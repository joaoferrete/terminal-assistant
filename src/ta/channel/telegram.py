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

from . import ChannelError, Handler, Inbound

log = logging.getLogger("ta.channel")

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
    def parse(update: dict) -> Inbound | None:
        """One update → one `Inbound`, or None for what is not a message to us."""
        msg = update.get("message")
        if not msg:
            return None   # edits, channel posts, callbacks (F4) — not captures
        sender = msg.get("from") or {}
        if sender.get("is_bot"):
            return None
        chat = msg.get("chat") or {}
        text = msg.get("text") or msg.get("caption") or ""
        return Inbound(
            channel="telegram",
            conversation_id=str(chat.get("id", "")),
            message_id=str(msg.get("message_id", "")),
            sender_id=str(sender.get("id", "")),
            sender_username=sender.get("username"),
            private=chat.get("type") == "private",
            text=text,
            unsupported=not text,
        )

    async def run(self, handler: Handler) -> None:
        offset: int | None = None
        failures = 0
        while True:
            try:
                updates = await self._call(
                    "getUpdates",
                    {"offset": offset, "timeout": self.POLL_SECONDS,
                     "allowed_updates": ["message"]},
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
                inbound = self.parse(update)
                if inbound is None:
                    continue
                try:
                    await handler(inbound)
                except Exception:
                    log.exception("the handler failed on a telegram message")

    async def reply(self, to: Inbound, text: str) -> None:
        payload = {"chat_id": to.conversation_id, "text": text}
        if to.message_id:
            payload["reply_parameters"] = {
                "message_id": int(to.message_id),
                "allow_sending_without_reply": True,
            }
        await self._call("sendMessage", payload)
