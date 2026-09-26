"""Telegram, by long polling (D4).

Long polling is the reason Telegram came first: the server asks Telegram for
updates, so no port opens on the router, there is no public URL or certificate,
and the threat model of ADR 0012 does not grow. A webhook would need all three.

The bot token travels **in the URL path** (`/bot<token>/getUpdates`). So an
exception from `httpx` — whose message includes the URL — is never logged or
re-raised as is: `ChannelError` carries the method name and the status, nothing
else.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from . import Button, ChannelError, Handler, Inbound

log = logging.getLogger("ta.channel")

# httpx logs every request at INFO, **with the full URL** — and ours carries the
# bot token. The daemon logs at INFO, so each 50-second poll wrote the token to
# the journal. It was caught on the server's first deploy, not by the test that
# checked our own log lines, which never saw httpx's. WARNING keeps its errors.
logging.getLogger("httpx").setLevel(logging.WARNING)

API = "https://api.telegram.org"


class TelegramChannel:
    name = "telegram"

    # Telegram holds a long poll open for up to this many seconds when there is
    # nothing to deliver; the HTTP timeout must outlast it or every idle minute
    # would look like a failure.
    POLL_SECONDS = 50
    # Waits after consecutive failures: a network blip retries fast, an outage
    # settles at a minute instead of hammering the API.
    BACKOFF = (1, 2, 5, 15, 60)

    def __init__(
        self,
        token: str | None,
        *,
        base_url: str = API,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ) -> None:
        self.token = token
        self.base_url = base_url
        self._transport = transport
        self._sleep = sleep
        self._client: httpx.AsyncClient | None = None
        # The bot's own account (getMe), to tell when a group message mentions it.
        self.me: dict = {}

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=f"{self.base_url}/bot{self.token}",
                timeout=self.POLL_SECONDS + 15,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _call(self, method: str, payload: dict):
        try:
            r = await self._http().post(f"/{method}", json=payload)
        except httpx.HTTPError as e:
            raise ChannelError(f"telegram {method}: {type(e).__name__}") from None
        try:
            body = r.json()
        except ValueError:
            raise ChannelError(f"telegram {method}: HTTP {r.status_code}") from None
        if not body.get("ok"):
            raise ChannelError(
                f"telegram {method}: HTTP {r.status_code} {body.get('description', '')}"
            )
        return body.get("result")

    @staticmethod
    def parse(update: dict, me: dict | None = None) -> Inbound | None:
        """One update → one `Inbound`, or None for what is not a message to us."""
        if cb := update.get("callback_query"):
            sender, msg = cb.get("from") or {}, cb.get("message") or {}
            chat = msg.get("chat") or {}
            return Inbound(
                channel="telegram",
                conversation_id=str(chat.get("id", "")),
                message_id=str(msg.get("message_id", "")),
                sender_id=str(sender.get("id", "")),
                sender_username=sender.get("username"),
                private=chat.get("type") == "private",
                callback=cb.get("data") or "",
                callback_id=cb.get("id"),
            )
        msg = update.get("message")
        if not msg:
            return None   # edits and channel posts are not messages to us
        sender = msg.get("from") or {}
        if sender.get("is_bot"):
            return None
        chat = msg.get("chat") or {}
        text = msg.get("text") or msg.get("caption") or ""
        me = me or {}
        replied = msg.get("reply_to_message") or {}
        mentioned = bool(
            (me.get("username") and f"@{me['username']}".lower() in text.lower())
            or (me.get("id") and (replied.get("from") or {}).get("id") == me["id"])
        )
        # `voice` is a note recorded in Telegram; `audio` is a file someone sent.
        # Both are speech to transcribe as far as capture is concerned.
        sound = msg.get("voice") or msg.get("audio") or {}
        return Inbound(
            channel="telegram",
            conversation_id=str(chat.get("id", "")),
            message_id=str(msg.get("message_id", "")),
            sender_id=str(sender.get("id", "")),
            sender_username=sender.get("username"),
            private=chat.get("type") == "private",
            text=text,
            unsupported=not text and not sound,
            voice_file_id=sound.get("file_id"),
            voice_seconds=int(sound.get("duration") or 0),
            reply_to_message_id=(str(msg["reply_to_message"]["message_id"])
                                 if msg.get("reply_to_message") else None),
            mentioned=mentioned,
        )

    async def run(self, handler: Handler) -> None:
        offset: int | None = None
        failures = 0
        while not self.me:
            try:
                self.me = await self._call("getMe", {}) or {}
            except ChannelError as e:
                wait = self.BACKOFF[min(failures, len(self.BACKOFF) - 1)]
                failures += 1
                log.warning("%s; retrying in %ss", e, wait)
                await self._sleep(wait)
        failures = 0
        while True:
            try:
                updates = await self._call(
                    "getUpdates",
                    {"offset": offset, "timeout": self.POLL_SECONDS,
                     "allowed_updates": ["message", "callback_query"]},
                )
                failures = 0
            except ChannelError as e:
                wait = self.BACKOFF[min(failures, len(self.BACKOFF) - 1)]
                failures += 1
                log.warning("%s; retrying in %ss", e, wait)
                await self._sleep(wait)
                continue

            for update in updates or []:
                # Advance past it before handling: a message that crashes the
                # handler must not come back on every poll forever.
                offset = update["update_id"] + 1
                inbound = self.parse(update, self.me)
                if inbound is None:
                    continue
                try:
                    await handler(inbound)
                except Exception:
                    log.exception("the handler failed on a telegram message")

    async def download(self, file_id: str) -> bytes:
        info = await self._call("getFile", {"file_id": file_id})
        path = (info or {}).get("file_path")
        if not path:
            raise ChannelError("telegram getFile: no file_path")
        # Files live under /file/bot<token>/, outside the method base URL — and
        # the token is in this URL too, so errors are scrubbed the same way.
        try:
            r = await self._http().get(f"{self.base_url}/file/bot{self.token}/{path}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise ChannelError(f"telegram download: {type(e).__name__}") from None
        return r.content

    async def send(self, conversation_id: str, text: str,
                   buttons: list[Button] | None = None) -> str | None:
        payload: dict = {"chat_id": conversation_id, "text": text}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [[
                {"text": b.label, "callback_data": b.data} for b in buttons]]}
        sent = await self._call("sendMessage", payload)
        return str(sent["message_id"]) if isinstance(sent, dict) and "message_id" in sent else None

    async def reply(self, to: Inbound, text: str, buttons: list[Button] | None = None,
                    *, quiet: bool = False) -> str | None:
        payload: dict = {"chat_id": to.conversation_id, "text": text}
        if quiet:
            payload["disable_notification"] = True
        if to.message_id and not to.callback:
            payload["reply_parameters"] = {
                "message_id": int(to.message_id),
                "allow_sending_without_reply": True,
            }
        if buttons:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": b.label, "callback_data": b.data}
                                     for b in buttons]]
            }
        sent = await self._call("sendMessage", payload)
        return str(sent["message_id"]) if isinstance(sent, dict) and "message_id" in sent else None

    async def answered(self, to: Inbound, text: str | None = None) -> None:
        # Stop the button's spinner first: Telegram shows it until this call.
        payload = {"callback_query_id": to.callback_id}
        if text:
            payload["text"] = text
        await self._call("answerCallbackQuery", payload)
        # Then take the buttons off, so the same action cannot be pressed twice.
        try:
            await self._call("editMessageReplyMarkup", {
                "chat_id": to.conversation_id, "message_id": int(to.message_id),
                "reply_markup": {"inline_keyboard": []},
            })
        except ChannelError as e:
            log.info("could not remove the buttons: %s", e)
