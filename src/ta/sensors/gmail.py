"""Gmail, read-only, on demand, per Member (F9, D40).

The same OAuth client and paste-back flow as the calendar (D14), with a
Connector of its own that asks for one scope: `gmail.readonly`. Nothing here
can send, delete, label or mark as read — the scope itself forbids it, so a
tainted turn has nothing to trick the agent into even if a Tool had a bug.

It also writes **drafts** (D42), which the Member reviews and sends from Gmail
themselves: the bot never sends.

Nothing is stored. Each question is answered from Gmail at that moment, and the
only thing kept is the refresh token (0600, as the calendar's). Mail is text
strangers wrote, so the Tools that return it taint the turn (D27).

`gmail.readonly` is a *restricted* scope: an unverified app in Production shows
Google's warning screen, and a Workspace admin may block it outright (the same
risk D14 records for the work account).
"""

from __future__ import annotations

import base64
import html
import re
import time as time_mod

import httpx

from .google_calendar import CalendarError, Connector, TokenStore, refresh_access

API = "https://gmail.googleapis.com/gmail/v1/users/me"
# `gmail.compose` is for drafts (D42). Google has no drafts-only scope: compose
# could also send, so "never sends" is kept by the code — there is no send call
# anywhere in this module, and the tests fail on any request to `/send`.
SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/gmail.compose")
MAX_RESULTS = 10
# What the model gets of one message. A newsletter can be 200 KB of markup.
MAX_BODY_CHARS = 8000


class MailError(CalendarError):
    """Gmail refused or could not be reached. The message never holds a token."""


def account_of(tokens: dict, *, transport: httpx.BaseTransport | None = None) -> str:
    """The address of a freshly connected mailbox, from Gmail's own profile."""
    with httpx.Client(timeout=15, transport=transport) as c:
        r = c.get(f"{API}/profile", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    if r.status_code != 200:
        raise MailError(f"could not read the connected mailbox (HTTP {r.status_code})")
    return r.json()["emailAddress"]


class Gmail:
    def __init__(self, member_id: int, connector: Connector, store: TokenStore, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.member_id, self.connector, self.store = member_id, connector, store
        self._transport = transport
        self._access: dict[str, tuple[str, float]] = {}

    @property
    def available(self) -> bool:
        return self.connector.configured and bool(self.store.accounts(self.member_id))

    def search(self, query: str, limit: int = MAX_RESULTS) -> list[dict]:
        """Messages matching a Gmail query (`from:banco newer_than:7d`), newest
        first, across the Member's connected mailboxes: headers and snippet only."""
        found = []
        for account, _ in self.store.accounts(self.member_id):
            listed = self._get(account, "/messages", {"q": query, "maxResults": limit})
            for m in listed.get("messages", [])[:limit]:
                meta = self._get(account, f"/messages/{m['id']}", {
                    "format": "metadata", "metadataHeaders": ["From", "Subject", "Date"]})
                h = _headers(meta)
                found.append({"id": f"{account}|{m['id']}", "account": account,
                              "from": h.get("from", ""), "subject": h.get("subject", ""),
                              "date": h.get("date", ""),
                              "snippet": html.unescape(meta.get("snippet", ""))})
        return found

    def read(self, ref: str) -> dict:
        """One message's headers and text, by the id `search` gave."""
        account, _, mid = ref.partition("|")
        if account not in dict(self.store.accounts(self.member_id)) or not mid:
            # Another Member's mailbox, or a made-up id: the same answer for both.
            raise MailError("no such message in your mailboxes")
        full = self._get(account, f"/messages/{mid}", {"format": "full"})
        h = _headers(full)
        body = _text(full.get("payload", {}))
        return {"from": h.get("from", ""), "subject": h.get("subject", ""),
                "date": h.get("date", ""), "text": body[:MAX_BODY_CHARS],
                "truncated": len(body) > MAX_BODY_CHARS}

    def draft(self, *, to: str, subject: str, body: str, reply_to: str = "") -> str:
        """Save a draft; returns the mailbox it went to. A reply (`reply_to` is an
        id from `search`) stays in its thread, with the headers mail clients use
        to thread it. Never sent: the Member sends it from Gmail."""
        from email.message import EmailMessage

        accounts = dict(self.store.accounts(self.member_id))
        msg = EmailMessage()
        thread = None
        if reply_to:
            account, _, mid = reply_to.partition("|")
            if account not in accounts or not mid:
                raise MailError("no such message in your mailboxes")
            original = self._get(account, f"/messages/{mid}", {
                "format": "metadata", "metadataHeaders": ["Message-ID", "Subject", "From"]})
            h = _headers(original)
            thread = original.get("threadId")
            if h.get("message-id"):
                msg["In-Reply-To"] = msg["References"] = h["message-id"]
            to = to or h.get("from", "")
            subject = subject or ("Re: " + h.get("subject", ""))
        else:
            account = next(iter(accounts), "")
            if not account:
                raise MailError("no mailbox is connected")
        msg["To"], msg["Subject"] = to, subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = {"message": {"raw": raw, **({"threadId": thread} if thread else {})}}
        self._post(account, "/drafts", payload)
        return account

    # Plumbing ────────────────────────────────────────────────────────────────
    def _post(self, account: str, path: str, payload: dict) -> dict:
        # Only ever `/drafts`: a send is not something this class can do. Not an
        # `assert`, which `python -O` would strip.
        if path != "/drafts":
            raise MailError(f"refusing to POST {path}")
        with httpx.Client(timeout=15, transport=self._transport) as c:
            try:
                r = c.post(f"{API}{path}", json=payload,
                           headers={"Authorization": f"Bearer {self._token(account)}"})
            except httpx.HTTPError as e:
                raise MailError(f"gmail: {type(e).__name__}") from None
        if r.status_code >= 400:
            # 403 here is usually a mailbox connected before drafts existed,
            # without the compose scope: reconnecting grants it.
            raise MailError(f"gmail drafts: HTTP {r.status_code}")
        return r.json()

    def _token(self, account: str) -> str:
        cached = self._access.get(account)
        if cached and cached[1] > time_mod.time() + 60:
            return cached[0]
        refresh = dict(self.store.accounts(self.member_id)).get(account)
        if refresh is None:
            raise MailError(f"{account} is not connected")
        data = refresh_access(self.connector, refresh, self._transport)
        self._access[account] = (data["access_token"], time_mod.time() + data.get("expires_in", 0))
        return data["access_token"]

    def _get(self, account: str, path: str, params: dict) -> dict:
        with httpx.Client(timeout=15, transport=self._transport) as c:
            try:
                r = c.get(f"{API}{path}", params=params,
                          headers={"Authorization": f"Bearer {self._token(account)}"})
            except httpx.HTTPError as e:
                raise MailError(f"gmail: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise MailError(f"gmail {path.split('/')[1]}: HTTP {r.status_code}")
        return r.json()


def _headers(message: dict) -> dict[str, str]:
    return {h["name"].lower(): h["value"]
            for h in message.get("payload", {}).get("headers", [])}


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _parts(payload: dict):
    yield payload
    for p in payload.get("parts", []) or []:
        yield from _parts(p)


def _text(payload: dict) -> str:
    """The plain-text body, or the HTML one with the tags stripped."""
    parts = list(_parts(payload))
    for mime in ("text/plain", "text/html"):
        for p in parts:
            data = (p.get("body") or {}).get("data")
            if p.get("mimeType") == mime and data:
                text = _decode(data)
                if mime == "text/html":
                    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
                    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
                return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
    return ""
