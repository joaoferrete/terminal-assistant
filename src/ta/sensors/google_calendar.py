"""Google Calendar over its REST API, per Member (D14, T5.1).

The server has no GNOME session, and the other Members have no Online Accounts on
the Owner's laptop, so on the server the calendar is Google's API with our own
OAuth client, one authorisation per account (amendment to ADR 0004).

Connecting happens through the Channel. Google's device flow does not allow
Calendar scopes, so it is the installed-app flow with a `localhost` redirect: the
Member opens the consent link on their phone, the redirect fails to load — as it
should, nothing listens there — and they paste the address back into the chat.
PKCE binds the pasted code to the link this bot issued.

Least privilege: `calendar.events` (read and create events) and
`calendar.calendarlist.readonly` (find the calendars). Not the full `calendar`
scope, which could delete whole calendars.

Refresh tokens live in files with mode 0600, one per account, outside the
database. The database is 0644, and these tokens read somebody's agenda.

The class keeps the interface of `sensors.calendar.Calendar` — `available`,
`today`, `now`, `write_targets`, `create_event` — so Rules, the review and the
Digest do not know which one they have.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time as time_mod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from .calendar import WRITE_CALENDAR, CalendarSource, Event

log = logging.getLogger("ta.calendar")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
REDIRECT = "http://localhost"
SCOPES = ("https://www.googleapis.com/auth/calendar.events",
          "https://www.googleapis.com/auth/calendar.calendarlist.readonly")
# A consent link that sits unused is a pending PKCE secret; ten minutes is long
# enough to switch apps on a phone and come back.
PENDING_TTL = 600


class CalendarError(RuntimeError):
    """Google refused or could not be reached. The message never holds a token."""


# ── The OAuth dance ─────────────────────────────────────────────────────────
@dataclass
class Pending:
    member_id: int
    verifier: str
    created: float


class Connector:
    """Consent links waiting to be pasted back, keyed by their `state`."""

    def __init__(self, client_id: str | None, client_secret: str | None, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.client_id, self.client_secret = client_id, client_secret
        self._pending: dict[str, Pending] = {}
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def consent_url(self, member_id: int) -> str:
        now = time_mod.time()
        self._pending = {k: v for k, v in self._pending.items()
                         if now - v.created < PENDING_TTL}
        state = secrets.token_urlsafe(16)
        verifier = secrets.token_urlsafe(48)
        self._pending[state] = Pending(member_id, verifier, now)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTH_URL + "?" + urlencode({
            "client_id": self.client_id, "redirect_uri": REDIRECT, "response_type": "code",
            "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
        })

    @staticmethod
    def looks_pasted(text: str) -> bool:
        return text.strip().startswith(REDIRECT) and "code=" in text and "state=" in text

    def exchange(self, pasted: str, member_id: int) -> dict:
        """The pasted redirect → the account's tokens. Only for the Member the link
        was issued to, only once, and only within its lifetime."""
        q = parse_qs(urlparse(pasted.strip()).query)
        state, code = (q.get("state") or [""])[0], (q.get("code") or [""])[0]
        pending = self._pending.pop(state, None)
        if (pending is None or pending.member_id != member_id or not code
                or time_mod.time() - pending.created >= PENDING_TTL):
            raise CalendarError("this link is not one I issued to you, or it expired")
        with httpx.Client(timeout=15, transport=self._transport) as c:
            r = c.post(TOKEN_URL, data={
                "client_id": self.client_id, "client_secret": self.client_secret,
                "code": code, "code_verifier": pending.verifier,
                "grant_type": "authorization_code", "redirect_uri": REDIRECT,
            })
        if r.status_code != 200 or "refresh_token" not in r.json():
            raise CalendarError(f"Google refused the code (HTTP {r.status_code})")
        return r.json()


class Link:
    """Connecting an account, end to end: the link out, the paste back."""

    def __init__(self, connector: Connector, store: TokenStore, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.connector, self.store, self._transport = connector, store, transport

    @property
    def configured(self) -> bool:
        return self.connector.configured

    def consent(self, member_id: int) -> str:
        return self.connector.consent_url(member_id)

    def finish(self, member_id: int, pasted: str) -> str:
        """Exchange the pasted code and keep the refresh token. Returns the account."""
        tokens = self.connector.exchange(pasted, member_id)
        account = account_of(tokens, transport=self._transport)
        self.store.save(member_id, account, tokens["refresh_token"])
        return account


# ── Tokens at rest ──────────────────────────────────────────────────────────
class TokenStore:
    def __init__(self, directory: Path) -> None:
        self.dir = directory

    def _path(self, member_id: int, account: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "@._-" else "_" for c in account)
        return self.dir / f"member-{member_id}-{safe}.json"

    def save(self, member_id: int, account: str, refresh_token: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        path = self._path(member_id, account)
        # Created 0600 from the first byte, not chmod'ed after: there is no moment
        # at which another user of the machine could read it.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"account": account, "refresh_token": refresh_token}, f)

    def accounts(self, member_id: int) -> list[tuple[str, str]]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.glob(f"member-{member_id}-*.json")):
            data = json.loads(p.read_text())
            out.append((data["account"], data["refresh_token"]))
        return out


# ── The calendar itself ─────────────────────────────────────────────────────
class GoogleCalendar:
    """One Member's Google calendars, behind the interface of `Calendar`."""

    def __init__(self, member_id: int, connector: Connector, store: TokenStore, *,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.member_id, self.connector, self.store = member_id, connector, store
        self._transport = transport
        self._access: dict[str, tuple[str, float]] = {}
        self._error: str | None = None

    # The interface of sensors.calendar.Calendar ──────────────────────────────
    @property
    def available(self) -> bool:
        return self.connector.configured and bool(self.store.accounts(self.member_id))

    @property
    def error(self) -> str | None:
        return self._error

    def warm(self) -> int:
        return len(self.store.accounts(self.member_id))

    def sources(self) -> list[CalendarSource]:
        out = []
        for account, _ in self.store.accounts(self.member_id):
            for cal in self._get(account, "/users/me/calendarList").get("items", []):
                out.append(CalendarSource(uid=f"{account}|{cal['id']}",
                                          name=cal.get("summary", ""), parent=account,
                                          local=False, account=account))
        return out

    def write_targets(self) -> list[CalendarSource]:
        """The dedicated `Terminal Assistant` calendar of each account (ADR 0007)."""
        return [s for s in self.sources() if s.name == WRITE_CALENDAR]

    def events_between(self, start_at: datetime, end_at: datetime) -> list[Event]:
        events: list[Event] = []
        for account, _ in self.store.accounts(self.member_id):
            cals = self._get(account, "/users/me/calendarList").get("items", [])
            for cal in cals:
                if not cal.get("selected", cal.get("primary", False)):
                    continue
                data = self._get(account, f"/calendars/{_q(cal['id'])}/events", {
                    # singleEvents expands the recurring ones: the EDS reader once
                    # showed a daily meeting on the date its series began.
                    "timeMin": _rfc3339(start_at), "timeMax": _rfc3339(end_at),
                    "singleEvents": "true", "orderBy": "startTime",
                })
                for item in data.get("items", []):
                    ev = _event(item, cal, account)
                    if ev is not None:
                        events.append(ev)
        return sorted(events, key=lambda e: e.start)

    def today(self, day: date | None = None) -> list[Event]:
        day = day or date.today()
        return self.events_between(datetime.combine(day, dtime.min),
                                   datetime.combine(day, dtime.max))

    def now(self, at: datetime | None = None) -> Event | None:
        at = at or datetime.now()
        return next((e for e in self.today(at.date())
                     if not e.all_day and e.start <= at <= e.end), None)

    def create_event(self, source_uid: str, summary: str, start: datetime,
                     end: datetime) -> str | None:
        """ADR 0007's invariant holds here too: **never** any attendees."""
        account, _, cal_id = source_uid.partition("|")
        body = {"summary": summary, "start": {"dateTime": _rfc3339(start)},
                "end": {"dateTime": _rfc3339(end)}}
        created = self._request(account, "POST", f"/calendars/{_q(cal_id)}/events", json=body)
        return created.get("id")

    # Plumbing ────────────────────────────────────────────────────────────────
    def _token(self, account: str) -> str:
        cached = self._access.get(account)
        if cached and cached[1] > time_mod.time() + 60:
            return cached[0]
        refresh = dict(self.store.accounts(self.member_id)).get(account)
        if refresh is None:
            raise CalendarError(f"{account} is not connected")
        with httpx.Client(timeout=15, transport=self._transport) as c:
            r = c.post(TOKEN_URL, data={
                "client_id": self.connector.client_id,
                "client_secret": self.connector.client_secret,
                "refresh_token": refresh, "grant_type": "refresh_token"})
        if r.status_code != 200:
            # The usual cause: the OAuth app still in Testing, whose refresh
            # tokens expire after seven days (D14). Say it, do not just fail.
            self._error = f"Google refused the refresh token (HTTP {r.status_code})"
            raise CalendarError(self._error)
        data = r.json()
        self._access[account] = (data["access_token"], time_mod.time() + data.get("expires_in", 0))
        return data["access_token"]

    def _request(self, account: str, method: str, path: str, **kw) -> dict:
        with httpx.Client(timeout=15, transport=self._transport, base_url=API) as c:
            try:
                r = c.request(method, path, headers={
                    "Authorization": f"Bearer {self._token(account)}"}, **kw)
            except httpx.HTTPError as e:
                raise CalendarError(f"calendar {method}: {type(e).__name__}") from None
        if r.status_code >= 400:
            raise CalendarError(f"calendar {method} {path.split('?')[0]}: HTTP {r.status_code}")
        return r.json() if r.content else {}

    def _get(self, account: str, path: str, params: dict | None = None) -> dict:
        return self._request(account, "GET", path, params=params or {})


def account_of(tokens: dict, *, transport: httpx.BaseTransport | None = None) -> str:
    """The email of a freshly connected account: its primary calendar's id."""
    with httpx.Client(timeout=15, transport=transport, base_url=API) as c:
        r = c.get("/users/me/calendarList/primary",
                  headers={"Authorization": f"Bearer {tokens['access_token']}"})
    if r.status_code != 200:
        raise CalendarError(f"could not read the connected account (HTTP {r.status_code})")
    return r.json()["id"]


def _q(cal_id: str) -> str:
    from urllib.parse import quote

    return quote(cal_id, safe="")


def _rfc3339(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.astimezone()).isoformat(timespec="seconds")


def _event(item: dict, cal: dict, account: str) -> Event | None:
    s, e = item.get("start", {}), item.get("end", {})
    all_day = "date" in s and "dateTime" not in s
    try:
        if all_day:
            start = datetime.combine(date.fromisoformat(s["date"]), dtime.min)
            end = datetime.combine(date.fromisoformat(e["date"]), dtime.min) - timedelta(seconds=1)
        else:
            start = datetime.fromisoformat(s["dateTime"]).astimezone().replace(tzinfo=None)
            end = datetime.fromisoformat(e["dateTime"]).astimezone().replace(tzinfo=None)
    except (KeyError, ValueError):
        return None
    return Event(uid=item.get("id", ""), summary=item.get("summary", ""), start=start, end=end,
                 all_day=all_day, calendar=cal.get("summary", ""),
                 source_uid=f"{account}|{cal['id']}")
