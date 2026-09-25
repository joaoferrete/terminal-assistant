"""The bot: what happens to a message that arrived on a Channel.

F1 is the Owner-only slice (D23). Only the Owner is recognised, only in a private
chat, and every text becomes a Note through exactly the path `ta note` takes.
Groups, other Members, the agent and its Tools come in later phases behind this
same `handle`.

Two rules from the plan already hold here:

- **Handled or captured** (invariant 1): a text from the Owner either is a
  command the bot answers, or it becomes a Note. Nothing is dropped.
- **Identity is the numeric id** (D21, invariant 4): the configured username only
  *pairs* the first time. After that, a different account using the same
  username is a stranger.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime

from .channel import Channel, Inbound
from .i18n import t

log = logging.getLogger("ta.bot")


def role_of(conn: sqlite3.Connection, channel: str, external_id: str) -> str | None:
    row = conn.execute(
        "SELECT role FROM channel_identities WHERE channel = ? AND external_id = ?",
        (channel, external_id),
    ).fetchone()
    return row[0] if row else None


def owner_paired(conn: sqlite3.Connection, channel: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM channel_identities WHERE channel = ? AND role = 'owner'", (channel,)
    ).fetchone() is not None


def pair_owner(conn: sqlite3.Connection, msg: Inbound, *, now: datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO channel_identities (channel, external_id, username, role, paired_at)"
        " VALUES (?, ?, ?, 'owner', ?)",
        (msg.channel, msg.sender_id, msg.sender_username,
         (now or datetime.now()).isoformat(timespec="seconds")),
    )


class Bot:
    def __init__(
        self,
        conn: sqlite3.Connection,
        channel: Channel,
        *,
        capture: Callable[[str], object],
        owner_username: str | None,
        board_link: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.conn = conn
        self.channel = channel
        # The same function `POST /notes` uses, so a Note from the phone is born
        # exactly like one from the terminal, review and all.
        self.capture = capture
        self.owner_username = owner_username
        # Issues a one-time code each call, so it is a function, not a string.
        self.board_link = board_link

    def _recognise(self, msg: Inbound) -> tuple[str | None, bool]:
        """(role, just paired). None is a stranger, answered with silence (D8)."""
        role = role_of(self.conn, msg.channel, msg.sender_id)
        if role is not None:
            return role, False
        wanted = self.owner_username
        if (
            wanted
            and (msg.sender_username or "").lower() == wanted
            and not owner_paired(self.conn, msg.channel)
        ):
            pair_owner(self.conn, msg)
            log.warning("paired the owner on %s", msg.channel)
            return "owner", True
        # Either nobody we know, or the Owner's username on an account that is
        # not the one that paired — the takeover D21 exists to refuse.
        return None, False

    async def handle(self, msg: Inbound) -> None:
        if not msg.private:
            return   # groups are allowlisted by chat id, and arrive in F3
        role, just_paired = self._recognise(msg)
        if role is None:
            return
        if just_paired:
            await self.channel.reply(msg, t("bot.paired"))
            # The first thing a new Member needs is the board, and typing a
            # 43-character token on a phone is where the user actually got stuck.
            if (url := self.board_link()) is not None:
                await self.channel.reply(msg, t("bot.board_link", url=url))

        if msg.unsupported:
            await self.channel.reply(msg, t("bot.unsupported"))
            return

        text = msg.text.strip()
        if text.startswith("/"):
            # `/start@SomeBot` is how Telegram addresses a command in a group.
            command = text.split()[0].split("@")[0].lower()
            if command == "/start":
                if not just_paired:
                    await self.channel.reply(msg, t("bot.hello"))
            elif command == "/board":
                url = self.board_link()
                await self.channel.reply(
                    msg, t("bot.board_link", url=url) if url else t("bot.board_unreachable")
                )
            else:
                await self.channel.reply(msg, t("bot.unknown_command"))
            return

        note = self.capture(text)
        due = getattr(note, "due", None)
        await self.channel.reply(
            msg,
            t("bot.captured_due", id=note.id, due=due) if due else t("bot.captured", id=note.id),
        )
