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

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import i18n
from . import members as members_mod
from .channel import Channel, ChannelError, Inbound
from .i18n import t
from .members import OWNER_ID
from .speech import Transcriber, TranscriptionFailed

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


def pair(
    conn: sqlite3.Connection, msg: Inbound, member_id: int, *, now: datetime | None = None
) -> None:
    conn.execute(
        "INSERT INTO channel_identities"
        " (channel, external_id, username, role, paired_at, member_id)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (msg.channel, msg.sender_id, msg.sender_username,
         "owner" if member_id == OWNER_ID else "member",
         (now or datetime.now()).isoformat(timespec="seconds"), member_id),
    )


def pair_owner(conn: sqlite3.Connection, msg: Inbound, *, now: datetime | None = None) -> None:
    pair(conn, msg, OWNER_ID, now=now)


def _identity_of(conn: sqlite3.Connection, channel: str, member_id: int) -> str | None:
    row = conn.execute(
        "SELECT external_id FROM channel_identities WHERE channel = ? AND member_id = ?",
        (channel, member_id),
    ).fetchone()
    return row[0] if row else None


class Bot:
    def __init__(
        self,
        conn: sqlite3.Connection,
        channel: Channel,
        *,
        capture: Callable[..., object],
        owner_username: str | None,
        board_link: Callable[[int], str | None] = lambda member_id: None,
        invited: Callable[[], set[str]] = set,
        allowed_groups: Callable[[], set[str]] = set,
        transcriber: Transcriber | None = None,
        audio_dir: Path | None = None,
    ) -> None:
        self.conn = conn
        self.channel = channel
        # The same function `POST /notes` uses, so a Note from the phone is born
        # exactly like one from the terminal, review and all.
        self.capture = capture
        self.owner_username = owner_username
        # Read from config.toml on every message, not once at boot: removing a
        # line there must revoke that person on the next message they send.
        self.invited = invited
        self.allowed_groups = allowed_groups
        # Issues a one-time code each call, so it is a function, not a string.
        self.board_link = board_link
        self.transcriber = transcriber
        # Where audio that could not be transcribed is kept, so nothing said is
        # lost (invariant 1). None keeps nothing, which is what tests want.
        self.audio_dir = audio_dir
        # Voice notes are handled off the Channel's loop: a minute of audio takes
        # seconds to transcribe, and every text behind it would wait. The set
        # holds the tasks so the GC does not collect one mid-flight.
        self._voice_tasks: set[asyncio.Task] = set()

    def _recognise(self, msg: Inbound) -> tuple[int | None, bool]:
        """(member id, just paired). None is a stranger, answered with silence (D8).

        Identity is the numeric id (D21). A username only pairs, once: the Owner's
        from `[channel.telegram] owner`, anybody else's from `[members]`. After
        that the id is what counts, so a changed username keeps its person and a
        taken-over one gets nobody.
        """
        row = self.conn.execute(
            "SELECT member_id, role FROM channel_identities WHERE channel = ? AND external_id = ?",
            (msg.channel, msg.sender_id),
        ).fetchone()
        if row is not None:
            member_id = row[0] if row[0] is not None else (OWNER_ID if row[1] == "owner" else None)
            if member_id is None or member_id == OWNER_ID:
                return member_id, False
            member = members_mod.get(self.conn, member_id)
            # Paired once, but no longer invited: revoked (ADR 0016). Their Notes
            # stay theirs; they just stop reaching the bot.
            if member is None or member.handle not in self.invited():
                return None, False
            return member_id, False

        username = (msg.sender_username or "").lower()
        if not username:
            return None, False
        if username == self.owner_username and not owner_paired(self.conn, msg.channel):
            pair(self.conn, msg, OWNER_ID)
            log.warning("paired the owner on %s", msg.channel)
            return OWNER_ID, True
        if username in self.invited():
            member = members_mod.by_handle(self.conn, username)
            if (
                member is not None
                and not member.is_owner
                and _identity_of(self.conn, msg.channel, member.id) is None
            ):
                pair(self.conn, msg, member.id)
                log.warning("paired member %s on %s", member.id, msg.channel)
                return member.id, True
        # Nobody we know, or a known username on an account that is not the one
        # that paired — the takeover D21 exists to refuse.
        return None, False

    async def _tell_owner(self, msg: Inbound, text: str) -> None:
        """A private note to the Owner. Their private chat id is their user id."""
        owner = _identity_of(self.conn, msg.channel, OWNER_ID)
        if owner is not None and hasattr(self.channel, "send"):
            try:
                await self.channel.send(owner, text)
            except ChannelError as e:
                log.warning("could not tell the owner: %s", e)

    async def handle(self, msg: Inbound) -> None:
        if not msg.private:
            # Only allowlisted groups are listened to at all (D8). What the bot
            # does in them — proactive capture, answering a mention — is F4's;
            # until then an allowlisted group is heard and left alone.
            if msg.conversation_id not in self.allowed_groups():
                return
            return
        member_id, just_paired = self._recognise(msg)
        if member_id is None:
            return
        if just_paired:
            await self.channel.reply(msg, t("bot.paired"))
            # The first thing a new Member needs is the board, and typing a
            # 43-character token on a phone is where the user actually got stuck.
            if (url := self.board_link(member_id)) is not None:
                await self.channel.reply(msg, t("bot.board_link", url=url))
            if member_id != OWNER_ID:
                who = f"@{msg.sender_username}" if msg.sender_username else msg.sender_id
                await self._tell_owner(msg, t("bot.member_paired", who=who))

        if msg.voice_file_id:
            task = asyncio.create_task(self._voice(msg, member_id))
            self._voice_tasks.add(task)
            task.add_done_callback(self._voice_tasks.discard)
            return

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
                url = self.board_link(member_id)
                await self.channel.reply(
                    msg, t("bot.board_link", url=url) if url else t("bot.board_unreachable")
                )
            else:
                await self.channel.reply(msg, t("bot.unknown_command"))
            return

        # The sender owns what they wrote (D6).
        note = self.capture(text, owner_id=member_id)
        due = getattr(note, "due", None)
        await self.channel.reply(
            msg,
            t("bot.captured_due", id=note.id, due=due) if due else t("bot.captured", id=note.id),
        )

    # ── Voice (F2) ──────────────────────────────────────────────────────────
    async def _voice(self, msg: Inbound, member_id: int) -> None:
        """Download, transcribe, capture — and when any step fails, still capture.

        Invariant 1: a voice note is handled or becomes a Note. A failure keeps
        the audio on disk and captures a placeholder that says where it is, so
        what was said is in the queue even when nobody could read it.
        """
        try:
            audio = await self.channel.download(msg.voice_file_id)
        except ChannelError as e:
            log.warning("could not download a voice note: %s", e)
            await self._capture_placeholder(msg, None, "download", member_id)
            return

        transcriber = self.transcriber
        if transcriber is None or not transcriber.available:
            reason = "unavailable"
        elif msg.voice_seconds > transcriber.max_seconds:
            reason = "too_long"
        else:
            await self.channel.reply(msg, t("bot.voice_listening"))
            started = time.monotonic()
            try:
                text = await transcriber.transcribe(audio, language=i18n.lang())
                # The one number that says whether this CPU keeps up. It was
                # missing on the first real run, and nobody could measure it.
                log.info("voice note of %ds transcribed in %.1fs",
                         msg.voice_seconds, time.monotonic() - started)
            except TranscriptionFailed as e:
                log.warning("transcription failed: %s", e)
                reason = "failed"
            else:
                note = self.capture(text, owner_id=member_id)
                await self.channel.reply(msg, t("bot.voice_captured", id=note.id, text=text))
                return

        await self._capture_placeholder(msg, self._keep(msg, audio), reason, member_id)

    def _keep(self, msg: Inbound, audio: bytes) -> Path | None:
        if self.audio_dir is None:
            return None
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        path = self.audio_dir / f"{msg.channel}-{msg.conversation_id}-{msg.message_id}.ogg"
        path.write_bytes(audio)
        return path

    async def _capture_placeholder(
        self, msg: Inbound, path: Path | None, reason: str, member_id: int
    ) -> None:
        why = t(f"bot.voice_{reason}")
        note = self.capture(
            t("bot.voice_placeholder", reason=why, path=str(path) if path else "—"),
            owner_id=member_id,
        )
        await self.channel.reply(msg, t("bot.voice_kept", id=note.id, reason=why))
