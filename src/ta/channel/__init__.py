"""The Channel: where Members talk to the bot (CONTEXT.md, D4).

Only the shape lives here — what an inbound message is and what a Channel can
do. Telegram is the first implementation (`telegram.py`); WhatsApp or anything
else comes in behind the same `Channel`, and nothing above this layer changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Inbound:
    """One message that arrived on a Channel.

    `sender_id` is the identity (D21): the Channel's numeric user id, as a string
    so every Channel fits. `sender_username` is only a hint for pairing — it is
    optional and it changes, so nothing is ever *authorised* by it.
    """

    channel: str
    conversation_id: str
    message_id: str
    sender_id: str
    sender_username: str | None
    private: bool
    text: str = ""
    # Something we cannot read yet (voice before F2, a photo, a sticker). Kept so
    # the bot can say so instead of pretending nothing arrived.
    unsupported: bool = False


Handler = Callable[[Inbound], Awaitable[None]]


class ChannelError(RuntimeError):
    """The Channel could not be reached. The message never carries a secret."""


class Channel(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    async def run(self, handler: Handler) -> None:
        """Deliver inbound messages to `handler` until cancelled. Never returns
        on a transient failure: it waits and tries again."""

    async def reply(self, to: Inbound, text: str) -> None: ...
