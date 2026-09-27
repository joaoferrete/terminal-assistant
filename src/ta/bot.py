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
import json
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from . import agent as agent_mod
from . import builtin_tools, i18n, memory, prerouter, receipts, scheduled, store
from . import members as members_mod
from .builtin_tools import ToolContext
from .channel import Button, Channel, ChannelError, Inbound
from .i18n import t
from .llm import for_member, for_task
from .members import OWNER_ID
from .speech import Transcriber, TranscriptionFailed
from .tools import NotAllowed, Tool, Turn, allowed
from .tools import run as run_tool

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


def _looks_pasted(text: str) -> bool:
    from .sensors.google_calendar import Connector

    return Connector.looks_pasted(text)


def _identity_of(conn: sqlite3.Connection, channel: str, member_id: int) -> str | None:
    row = conn.execute(
        "SELECT external_id FROM channel_identities WHERE channel = ? AND member_id = ?",
        (channel, member_id),
    ).fetchone()
    return row[0] if row else None


class GroupCapture(BaseModel):
    actionable: bool = Field(description="true only if the message asks for something to be "
                             "bought or added to one of the lists")
    list: str = Field(default="", description="which of the offered lists")
    item: str = Field(default="", description="the item, short: 'detergente', 'leite'")
    confidence: float = Field(default=0.0, description="0 to 1")


_CLASSIFY = """Lists this person may add to: {lists}.
Group message: "{text}"

Is this message asking for something to go on one of those lists — something that ran \
out, or needs buying? A question about whether someone bought it, a joke, or news is \
not. If it is, name the list and the item."""

# Below this the classifier's guess is not acted on: a wrong item in the shared
# shopping List, unasked for, is worse than a missed one somebody can still add.
PROACTIVE_MIN_CONFIDENCE = 0.7


@dataclass
class AgentDeps:
    """What the bot needs to converse (F4). Without it, every text is captured —
    which is exactly F1's behaviour, and what tests of capture want."""

    llm: object
    registry: Callable[[], dict[str, Tool]]
    permissions: Callable[[int], object]
    services: dict = field(default_factory=dict)
    persona: Callable[[int], str] = lambda member_id: ""
    house_rules: Callable[[], str] = lambda: ""
    # (name, personality) of the bot itself, from `[chat]`.
    identity: Callable[[], tuple[str, str]] = lambda: ("", "")
    # True while the Member's day and the household's month are under the
    # ceilings (D30). Checked before every model call the chat makes.
    within_budget: Callable[[int], bool] = lambda member_id: True


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
        agent: AgentDeps | None = None,
        calendar_link=None,
        mail_link=None,
        satellite_code: Callable[[int], str | None] = lambda member_id: None,
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
        self.agent = agent
        # Connecting a Google account from the chat (D14): the link out, the
        # pasted redirect back. None when there is no OAuth client configured.
        self.calendar_link = calendar_link
        # The same flow for Gmail, read-only (D40), with its own consent link.
        self.mail_link = mail_link
        # A one-time code a Satellite trades for its token (F6). Only the code goes
        # through the Channel, never the token: a chat is stored on its servers.
        self.satellite_code = satellite_code
        # Where audio that could not be transcribed is kept, so nothing said is
        # lost (invariant 1). None keeps nothing, which is what tests want.
        self.audio_dir = audio_dir
        # Voice notes are handled off the Channel's loop: a minute of audio takes
        # seconds to transcribe, and every text behind it would wait. The set
        # holds the tasks so the GC does not collect one mid-flight.
        self._voice_tasks: set[asyncio.Task] = set()
        # (member, day) pairs the Owner was already told about, so a spent budget
        # sends one message a day, not one per message.
        self._budget_told: set[tuple[int, str]] = set()
        # (sender, day) pairs of strangers already answered and reported (D60).
        self._strangers_told: set[tuple[str, str]] = set()

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

    async def _stranger(self, msg: Inbound) -> None:
        """Somebody not allowed, in private (D60, amending D8's silence).

        One standard reply, which names neither the household nor the Owner, and
        one message to the Owner with [Allow] and [Ignore] — each at most once per
        person per day, so a stranger typing all day costs the Owner one ping.
        """
        if msg.callback is not None or not owner_paired(self.conn, msg.channel):
            return
        key = (msg.sender_id, datetime.now().date().isoformat())
        if key in self._strangers_told:
            return
        self._strangers_told.add(key)
        await self.channel.reply(msg, t("bot.stranger"))
        owner = _identity_of(self.conn, msg.channel, OWNER_ID)
        if owner is None or not hasattr(self.channel, "send"):
            return
        who = " ".join(x for x in (msg.sender_name, f"@{msg.sender_username}"
                                   if msg.sender_username else None) if x) or msg.sender_id
        rid = receipts.record(self.conn, channel=msg.channel, conversation_id=owner,
                              message_id=None, member_id=OWNER_ID, tool="stranger",
                              args={"username": msg.sender_username or ""}, state="pending",
                              summary=f"stranger {who}")
        snippet = " ".join((msg.text or "").split())[:80]
        try:
            await self.channel.send(owner, t("bot.stranger_owner", who=who, text=snippet),
                                    [Button(t("bot.btn_allow"), f"allow:{rid}"),
                                     Button(t("bot.btn_ignore"), f"no:{rid}")])
        except ChannelError as e:
            log.warning("could not tell the owner about a stranger: %s", e)

    async def _allow_stranger(self, msg: Inbound, r) -> None:
        """[Allow] on a stranger: pick the Grant, then D59's confirmed add."""
        from .config import grants_config

        username = r.args.get("username") or ""
        receipts.set_state(self.conn, r.id, "done")
        if not username:
            await self.channel.answered(msg)
            await self._say(msg, t("bot.stranger_no_username"))
            return
        _, grants = grants_config()
        buttons = []
        for grant in [*sorted(grants), ""]:
            rid = self._receipt(msg, OWNER_ID, tool="member_add",
                                args={"username": username, "grant": grant}, state="pending",
                                summary=f"allow @{username} as {grant or 'notes only'}")
            buttons.append(Button(grant or t("bot.no_grant"), f"ok:{rid}"))
        await self.channel.answered(msg)
        await self._say(msg, t("bot.pick_grant", who=f"@{username}"), buttons)

    def last_talked(self) -> dict[str, str]:
        """handle → when that Member last wrote to the bot (from the chat memory)."""
        rows = self.conn.execute(
            "SELECT m.handle, MAX(x.at) FROM messages x JOIN members m ON m.id = x.member_id"
            " GROUP BY m.handle").fetchall()
        return {h: at for h, at in rows if h}

    def _members_text(self) -> str:
        """`/moradores`: who may use the bot, and when they last talked (D61)."""
        from .config import grants_config

        invited, _ = grants_config()
        if not invited:
            return t("bot.members_none")
        last = self.last_talked()

        def when(handle: str) -> str:
            at = last.get(handle)
            return (t("bot.last_talked", at=datetime.fromisoformat(at).strftime("%d/%m %H:%M"))
                    if at else t("bot.never_talked"))

        return "\n".join([t("bot.members_title"), *(
            f"• @{h} — {', '.join(g) or t('bot.no_grant')} · {when(h)}"
            for h, g in sorted(invited.items()))])

    def _hub(self):
        services = self.agent.services if self.agent else {}
        app = services.get("app")
        return services.get("hub") or (getattr(app.state, "hub", None) if app else None)

    def _satellites_text(self, member_id: int) -> str:
        """`/satellites`: the computers connected, and whether they are on now.
        The Owner sees every Member's; everyone else sees their own."""
        hub = self._hub()
        rows = hub.machines(None if member_id == OWNER_ID else member_id) if hub else []
        if not rows:
            return t("bot.satellites_none")
        names = {m.id: m.handle for m in members_mod.all_members(self.conn)}
        lines = [t("bot.satellites_title")]
        for r in rows:
            seen = datetime.fromtimestamp(r["last_seen"]).strftime("%d/%m %H:%M")
            state = t("bot.satellite_on") if r["online"] else t("bot.satellite_off", at=seen)
            owner = f" (@{names.get(r['member_id'], '?')})" if member_id == OWNER_ID else ""
            lines.append(f"• {r['machine']}{owner} — {state}")
        return "\n".join(lines)

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
            # Only allowlisted groups are listened to at all (D8).
            if msg.conversation_id in self.allowed_groups():
                await self._group(msg)
            return
        member_id, just_paired = self._recognise(msg)
        if member_id is None:
            await self._stranger(msg)
            return
        if msg.callback is not None:
            await self._button(msg, member_id)
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
            elif command == "/satellite":
                code = self.satellite_code(member_id)
                await self.channel.reply(
                    msg, t("bot.satellite_code", code=code) if code else t("bot.board_unreachable"))
            elif command in ("/conectar_agenda", "/connect_calendar"):
                await self._connect_calendar(msg, member_id)
            elif command in ("/conectar_email", "/connect_email"):
                await self._connect_mail(msg, member_id)
            elif command in ("/help", "/ajuda"):
                # From the catalogue, with no model: the cheapest answer there is.
                await self.channel.reply(msg, t("bot.help"))
            elif command in ("/moradores", "/members") and member_id == OWNER_ID:
                await self.channel.reply(msg, self._members_text())
            elif command in ("/satellites", "/computadores"):
                await self.channel.reply(msg, self._satellites_text(member_id))
            elif command in ("/agendado", "/scheduled"):
                await self.channel.reply(msg, self._scheduled_text(member_id))
            elif command == "/board":
                url = self.board_link(member_id)
                await self.channel.reply(
                    msg, t("bot.board_link", url=url) if url else t("bot.board_unreachable")
                )
            else:
                await self.channel.reply(msg, t("bot.unknown_command"))
            return

        # The pasted consent redirect is a credential, not a thought: it is never
        # captured, never remembered, never sent to a model.
        if _looks_pasted(text) and (self.calendar_link or self.mail_link):
            # The link's `state` says whether it was for the mail or the calendar.
            if self.mail_link is not None and self.mail_link.connector.owns(text):
                await self._finish_mail(msg, member_id, text)
            elif self.calendar_link is not None:
                await self._finish_calendar(msg, member_id, text)
            return

        await self._text(msg, member_id, text)

    async def _connect_calendar(self, msg: Inbound, member_id: int) -> None:
        if self.calendar_link is None or not self.calendar_link.configured:
            await self.channel.reply(msg, t("bot.calendar_unconfigured"))
            return
        if not msg.private:
            return      # a consent link is personal; it never goes to a group
        await self.channel.reply(
            msg, t("bot.calendar_link", url=self.calendar_link.consent(member_id)))

    async def _finish_calendar(self, msg: Inbound, member_id: int, text: str) -> None:
        from .sensors.google_calendar import CalendarError

        try:
            account = await asyncio.to_thread(self.calendar_link.finish, member_id, text)
        except CalendarError as e:
            log.warning("calendar connection failed: %s", e)
            await self.channel.reply(msg, t("bot.calendar_failed"))
            return
        await self.channel.reply(msg, t("bot.calendar_connected", account=account))

    async def _connect_mail(self, msg: Inbound, member_id: int) -> None:
        if self.mail_link is None or not self.mail_link.configured:
            await self.channel.reply(msg, t("bot.calendar_unconfigured"))
            return
        if not msg.private:
            return      # a consent link is personal; it never goes to a group
        await self.channel.reply(msg, t("bot.mail_link", url=self.mail_link.consent(member_id)))

    async def _finish_mail(self, msg: Inbound, member_id: int, text: str) -> None:
        from .sensors.google_calendar import CalendarError

        try:
            account = await asyncio.to_thread(self.mail_link.finish, member_id, text)
        except CalendarError as e:
            log.warning("mail connection failed: %s", e)
            await self.channel.reply(msg, t("bot.mail_failed"))
            return
        await self.channel.reply(msg, t("bot.mail_connected", account=account))

    # ── Groups (T4.6) ──────────────────────────────────────────────────────
    def _known(self, msg: Inbound) -> int | None:
        """A Member already paired — never pairing here. Pairing answers with the
        Member's own board link, and in a group everybody would see it."""
        row = self.conn.execute(
            "SELECT member_id, role FROM channel_identities WHERE channel = ? AND external_id = ?",
            (msg.channel, msg.sender_id),
        ).fetchone()
        if row is None:
            return None
        member_id = row[0] if row[0] is not None else (OWNER_ID if row[1] == "owner" else None)
        if member_id not in (None, OWNER_ID):
            member = members_mod.get(self.conn, member_id)
            if member is None or member.handle not in self.invited():
                return None
        return member_id

    async def _group(self, msg: Inbound) -> None:
        member_id = self._known(msg)
        # Somebody in the group who is not a Member: nothing of theirs is kept or
        # acted on. They never agreed to be remembered by this bot.
        if member_id is None:
            return
        if msg.callback is not None:
            await self._button(msg, member_id)
            return
        text = msg.text.strip()
        if not text:
            return
        if msg.mentioned or text.startswith("/"):
            # Addressed to the bot: the agent, seeing only the household's data
            # (invariant 3), with the group's history counting as tainted (D33).
            for mention in self._mentions():
                text = text.replace(mention, "").strip()
            await self._text(msg, member_id, text or msg.text.strip())
            return
        memory.record(self.conn, channel=msg.channel, conversation_id=msg.conversation_id,
                      message_id=msg.message_id, member_id=member_id, text=text)
        if self.agent is not None and self.agent.within_budget(member_id):
            await self._proactive(msg, member_id, text)

    def _mentions(self) -> list[str]:
        me = getattr(self.channel, "me", {}) or {}
        return [f"@{me['username']}"] if me.get("username") else []

    async def _proactive(self, msg: Inbound, member_id: int, text: str) -> None:
        """A group message nobody addressed to the bot, that may still be a List
        item: "acabou o detergente" (D10, D11).

        It goes straight into a household List the author may add to, acknowledged
        quietly with an undo button. Only `list_add` is reachable from here, and
        the Turn is marked proactive, so a destructive Tool could never run even if
        one were asked for (invariant 7). A failure is silent: the message was
        not addressed to the bot, so there is nobody waiting for an answer.
        """
        deps = self.agent
        perms = deps.permissions(member_id)
        writable = [lv for lv in store.lists_for(self.conn, viewer=members_mod.Viewer(
                    member_id, in_group=True)) if lv.scope == "household" and perms.list_(lv.name)]
        chosen = deps.registry().get("list_add")
        if not writable or chosen is None:
            return
        try:
            with for_member(member_id), for_task("classify"):
                found = await deps.llm._structured(
                    _CLASSIFY.format(lists=", ".join(lv.name for lv in writable), text=text),
                    GroupCapture,
                    system="You sort a household group chat's messages. Most are "
                    "conversation and are not items. Be conservative.",
                )
        except Exception as e:
            log.info("proactive classifier unavailable: %s", e)
            return
        lv = next((x for x in writable if x.name.lower() == found.list.strip().lower()), None)
        if not found.actionable or found.confidence < PROACTIVE_MIN_CONFIDENCE or lv is None \
                or not found.item.strip():
            return
        turn = Turn(member_id=member_id, conversation_id=msg.conversation_id, in_group=True,
                    permissions=perms, proactive=True)
        ctx = ToolContext(conn=self.conn, turn=turn, channel=msg.channel,
                          services={**deps.services, "message": msg})
        try:
            await run_tool(chosen, turn, ctx, {"list": lv.name, "item": found.item.strip()})
        except Exception as e:
            log.warning("proactive capture failed: %s", e)
            return
        rids, buttons = [], None
        for done in turn.receipts:
            rid = self._receipt(msg, member_id, tool=done["tool"], summary=done["summary"],
                                undo=done.get("undo"))
            rids.append(rid)
            buttons = [Button(t("bot.btn_undo"), f"undo:{rid}")]
        sent = await self.channel.reply(
            msg, t("bot.proactive", item=found.item.strip(), list=lv.name), buttons, quiet=True)
        if sent:
            receipts.answered_by(self.conn, rids, sent)

    def _captured_line(self, note) -> str:
        # A timer says when it will ring: "daqui 10 min" read wrong must show now,
        # not when the reminder fails to come.
        remind = getattr(note, "remind_at", None)
        if remind:
            at = datetime.fromisoformat(str(remind))
            when = f"{at:%H:%M}" if at.date() == datetime.now().date() else f"{at:%d/%m %H:%M}"
            return t("bot.captured_remind", id=note.id, at=when)
        due = getattr(note, "due", None)
        return t("bot.captured_due", id=note.id, due=due) if due else t("bot.captured", id=note.id)

    async def _say(self, msg: Inbound, text: str, buttons: list[Button] | None = None,
                   receipt_ids: list[int] | None = None) -> None:
        sent = await self.channel.reply(msg, text, buttons) if buttons else \
            await self.channel.reply(msg, text)
        memory.record(self.conn, channel=msg.channel, conversation_id=msg.conversation_id,
                      message_id=sent, member_id=None, text=text)
        if sent and receipt_ids:
            receipts.answered_by(self.conn, receipt_ids, sent)

    def _receipt(self, msg: Inbound, member_id: int, **kw) -> int:
        return receipts.record(self.conn, channel=msg.channel,
                               conversation_id=msg.conversation_id,
                               message_id=msg.message_id, member_id=member_id, **kw)

    def _chat_services(self, msg: Inbound) -> dict:
        """What only the bot has, for the Tools (D47, D48): the message, a way to
        send into this conversation, and the things the commands hand out."""
        async def say(text: str) -> None:
            await self._say(msg, text)

        async def progress(tool_name: str) -> None:
            key = f"progress.{tool_name}"
            text = t(key)
            await self.channel.reply(msg, text if text != key else t("progress.generic"),
                                     quiet=True)

        return {"message": msg, "say": say, "progress": progress,
                "board_link": self.board_link,
                "satellite_code": self.satellite_code, "calendar_link": self.calendar_link,
                "mail_link": self.mail_link}

    @staticmethod
    def _switched(tool: str, receipt: dict) -> str:
        """What a home Tool did, in the catalogue's words, honest about the
        Entities that did not confirm the new state."""
        key = "bot.home_on_done" if tool == "home_on" else "bot.home_off_done"
        # "Bedroom lamp", not light.bedroom_lamp_2: the Tool records each
        # Entity's friendly name, and the id is only the fallback.
        names = receipt.get("names") or {}

        def said(entities):
            return ", ".join(names.get(e, e) for e in entities or [])

        line = t(key, what=said(receipt.get("entities")))
        if receipt.get("unconfirmed"):
            line += "\n" + t("bot.home_unconfirmed", what=said(receipt["unconfirmed"]))
        return line

    @staticmethod
    def _undo_button(rid: int, undo: dict) -> Button:
        # Undoing a schedule is cancelling it, and the button should say so: a
        # plain "Undo" under "I'll turn it on at 7" reads as undoing the light.
        if undo.get("kind") == "cancel_scheduled":
            return Button(t("bot.btn_cancel_scheduled"), f"undo:{rid}")
        if undo.get("kind") == "undo_all":
            return Button(t("bot.btn_undo_all"), f"undo:{rid}")
        return Button(t("bot.btn_undo"), f"undo:{rid}")

    async def _text(self, msg: Inbound, member_id: int, text: str) -> None:
        """A text (or a transcript) from a recognised Member: converse, or capture.

        Invariant 1 holds on every path out of here: the message was answered, or it
        became a Note — and when the agent fails, it becomes a Note.
        """
        history = memory.recent(self.conn, channel=msg.channel,
                                conversation_id=msg.conversation_id)
        memory.record(self.conn, channel=msg.channel, conversation_id=msg.conversation_id,
                      message_id=msg.message_id, member_id=member_id, text=text)
        if self.agent is None:
            # The sender owns what they wrote (D6).
            await self._say(msg, self._captured_line(self.capture(text, owner_id=member_id)))
            return

        deps = self.agent
        if await self._routine_phrase(msg, member_id, text):
            return
        if await self._prerouted(msg, member_id, text):
            return
        if not deps.within_budget(member_id):
            # Chat and search stop; capture goes on (D30, invariant 1).
            note = self.capture(text, owner_id=member_id)
            await self._say(msg, t("bot.budget_spent", line=self._captured_line(note)))
            key = (member_id, datetime.now().date().isoformat())
            if key not in self._budget_told:
                self._budget_told.add(key)
                await self._tell_owner(msg, t("bot.budget_owner", who=member_id))
            return
        turn = Turn(member_id=member_id, conversation_id=msg.conversation_id,
                    in_group=not msg.private, permissions=deps.permissions(member_id))
        ctx = ToolContext(conn=self.conn, turn=turn, channel=msg.channel,
                          services={**deps.services, **self._chat_services(msg)})
        available = [t_ for t_ in deps.registry().values() if allowed(t_, turn)]
        about = None
        if msg.reply_to_message_id:
            # "What did you do here?" is answered from the Receipts, not from
            # whatever the model remembers (D11).
            about = [f"{r.summary} [{r.state}]" for r in receipts.for_message(
                self.conn, channel=msg.channel, conversation_id=msg.conversation_id,
                message_id=msg.reply_to_message_id)] or ["(nothing recorded)"]
        try:
            with for_member(member_id):
                reply = await agent_mod.respond(
                    deps.llm, turn=turn, ctx=ctx, text=text, history=history,
                    available=available, persona=deps.persona(member_id),
                    house_rules=deps.house_rules(), about=about,
                    identity=deps.identity(),
                )
        except agent_mod.AgentFailed as e:
            log.warning("agent failed, capturing instead: %s", e)
            note = self.capture(text, owner_id=member_id)
            key = "bot.captured_offline" if e.unavailable else "bot.captured_unanswered"
            await self._say(msg, t(key, line=self._captured_line(note)))
            return

        parts, buttons, rids = [], [], []
        # What the Tools did this turn, each with its Receipt and the last one's undo.
        for done in turn.receipts:
            rid = self._receipt(msg, member_id, tool=done["tool"], summary=done["summary"],
                                undo=done.get("undo"))
            rids.append(rid)
            if done.get("undo"):
                buttons = [self._undo_button(rid, done["undo"])]
        if reply.pending is not None:
            rid, line = self._propose(msg, member_id, reply.pending)
            rids.append(rid)
            parts.append(line)
            buttons = [Button(t("bot.btn_confirm"), f"ok:{rid}"),
                       Button(t("bot.btn_cancel"), f"no:{rid}")]
        if reply.text:
            parts.append(reply.text)
        # A reply with nothing to say and nothing captured would drop the message.
        if reply.capture or not parts:
            note = self.capture(text, owner_id=member_id)
            rid = self._receipt(msg, member_id, tool="capture", summary=f"captured #{note.id}",
                                undo={"kind": "delete_note", "note_id": note.id})
            rids.append(rid)
            parts.append(self._captured_line(note))
            # D32: erring towards capture costs one tap, and this is the tap.
            if not buttons:
                buttons = [Button(t("bot.btn_not_note"), f"undo:{rid}")]
        if cited := agent_mod.format_sources(reply.sources):
            parts.append(cited)
        await self._say(msg, "\n\n".join(parts), buttons or None, rids)

    async def _routine_phrase(self, msg: Inbound, member_id: int, text: str) -> bool:
        """"Cheguei em casa": one of a Routine's phrases runs it, with no model
        (D57). True if it was one."""
        from . import agent_routines, routines

        routine = routines.by_phrase(self.conn, member_id, text)
        if routine is None:
            return False
        turn, ctx = self._turn(msg, member_id)
        result = await agent_routines.execute(ctx, routine)
        done = result.receipt or {}
        rid = self._receipt(msg, member_id, tool="routine_run", summary=done.get(
            "summary", f"routine {routine.name}"), undo=done.get("undo"))
        await self._say(msg, agent_routines.report(routine, done),
                        [self._undo_button(rid, done["undo"])] if done.get("undo") else None,
                        [rid])
        return True

    async def _prerouted(self, msg: Inbound, member_id: int, text: str) -> bool:
        """Switching the house with no model (D9). True if it was handled."""
        route = prerouter.home(text)
        if route is None:
            return False
        name, target = route
        chosen = self.agent.registry().get(name)
        if chosen is None:
            return False
        turn, ctx = self._turn(msg, member_id)
        try:
            # Only a target that resolves to something this Member may switch;
            # otherwise it was not about the house, and the agent takes it.
            if not await builtin_tools._targets(ctx, target):
                return False
            await run_tool(chosen, turn, ctx, {"target": target})
        except Exception as e:   # Home Assistant down: let the agent say so, or capture
            log.warning("pre-routed %s failed: %s", name, e)
            return False
        rids, buttons = [], None
        for done in turn.receipts:
            rid = self._receipt(msg, member_id, tool=done["tool"], summary=done["summary"],
                                undo=done.get("undo"))
            rids.append(rid)
            if done.get("undo"):
                buttons = [Button(t("bot.btn_undo"), f"undo:{rid}")]
        # The Tool's own text is written for the model, in English. What the Member
        # reads comes from the catalogue (AGENTS §1): the first real run showed
        # "Feito · turned on: light.sala" on the phone.
        switched = [e for done in turn.receipts for e in done.get("entities", [])]
        if not switched:
            return False
        receipt = {"entities": switched, "unconfirmed": [
            e for done in turn.receipts for e in done.get("unconfirmed", [])],
            "names": {k: v for done in turn.receipts for k, v in (done.get("names") or {}).items()}}
        await self._say(msg, self._switched(name, receipt), buttons, rids)
        return True

    def _propose(self, msg: Inbound, member_id: int, pending) -> tuple[int, str]:
        """Store an action waiting for the asker's button (D27) and describe it."""
        rid = self._receipt(
            msg, member_id, tool=pending.tool.name, args=pending.tool_args, state="pending",
            summary=f"{pending.tool.name} {json.dumps(pending.tool_args, ensure_ascii=False)}",
        )
        return rid, t("bot.confirm_needed", action=pending.tool.description,
                      args=", ".join(f"{k}={v}" for k, v in pending.tool_args.items()))

    # ── Split proposal (D5, T4.7) ───────────────────────────────────────────
    async def propose_split(self, note, parts: list[str], channel: str = "telegram") -> bool:
        """Offer the writer, in private, to split one Note into several.

        Nothing is split until they press the button: the model proposes, the
        person decides (ADR 0007's pattern). Returns False when the writer has no
        private chat on the Channel to be asked in.
        """
        chat = _identity_of(self.conn, channel, note.owner_id)
        if chat is None or not hasattr(self.channel, "send"):
            return False
        rid = receipts.record(self.conn, channel=channel, conversation_id=chat, message_id=None,
                              member_id=note.owner_id, tool="split",
                              args={"note_id": note.id, "parts": parts}, state="pending",
                              summary=f"split #{note.id} into {len(parts)}")
        lines = "\n".join(f"• {p}" for p in parts)
        sent = await self.channel.send(
            chat, t("bot.split_offer", id=note.id, n=len(parts), parts=lines),
            [Button(t("bot.btn_split"), f"split:{rid}"), Button(t("bot.btn_keep"), f"no:{rid}")],
        )
        if sent:
            receipts.answered_by(self.conn, [rid], sent)
        return True

    def _split(self, member_id: int, r) -> list:
        note_id, parts = r.args["note_id"], r.args["parts"]
        original = store.get_note(self.conn, note_id, viewer=members_mod.Viewer(member_id))
        created = [self.capture(p, owner_id=member_id) for p in parts]
        # The original goes to the trash, not away: it can be restored as it was.
        store.soft_delete(self.conn, original.id)
        return created

    # ── Buttons (T4.4) ──────────────────────────────────────────────────────
    async def _button(self, msg: Inbound, member_id: int) -> None:
        """A pressed button. Only the Member the Receipt belongs to may press it:
        a confirmation is the ASKER's (D27), not anybody's in the chat."""
        kind, _, raw = (msg.callback or "").partition(":")
        r = receipts.get(self.conn, int(raw)) if raw.isdigit() else None
        if r is None or r.member_id != member_id:
            await self.channel.answered(msg, t("bot.not_yours"))
            return
        if kind == "ok" and r.state == "pending":
            await self.channel.answered(msg)
            await self._confirmed(msg, member_id, r)
        elif kind == "split" and r.state == "pending":
            try:
                created = self._split(member_id, r)
            except KeyError:          # the Note was deleted meanwhile
                receipts.set_state(self.conn, r.id, "refused")
                await self.channel.answered(msg, t("bot.stale"))
                return
            receipts.set_state(self.conn, r.id, "done")
            await self.channel.answered(msg, t("bot.split_done",
                                               ids=", ".join(f"#{n.id}" for n in created)))
        elif kind in ("done", "snooze") and r.state == "pending" and r.tool == "reminder":
            try:
                answer = self._reminder_button(kind, member_id, r)
            except KeyError:          # the Note was deleted meanwhile
                receipts.set_state(self.conn, r.id, "refused")
                await self.channel.answered(msg, t("bot.stale"))
                return
            await self.channel.answered(msg, answer)
        elif kind == "ruleoff" and r.tool == "chat_rule":
            from . import chat_rules

            chat_rules.set_enabled(self.conn, r.args["id"], False, member_id=member_id,
                                   is_owner=member_id == OWNER_ID)
            receipts.set_state(self.conn, r.id, "done")
            await self.channel.answered(msg, t("bot.rule_switched_off"))
        elif kind == "allow" and r.state == "pending" and r.tool == "stranger":
            await self._allow_stranger(msg, r)
        elif kind == "no" and r.state == "pending":
            receipts.set_state(self.conn, r.id, "refused")
            await self.channel.answered(msg, t("bot.cancelled"))
        elif kind == "undo" and r.state == "done" and r.undo:
            await self._undo(msg, member_id, r)
            receipts.set_state(self.conn, r.id, "undone")
            await self.channel.answered(msg, t(
                "bot.scheduled_cancelled" if r.undo.get("kind") == "cancel_scheduled"
                else "bot.undone"))
        else:
            await self.channel.answered(msg, t("bot.stale"))

    def _turn(self, msg: Inbound, member_id: int) -> tuple[Turn, ToolContext]:
        # Permissions are read AGAIN at press time: a Grant revoked between the
        # proposal and the button must not be honoured by an old button.
        turn = Turn(member_id=member_id, conversation_id=msg.conversation_id,
                    in_group=not msg.private,
                    permissions=self.agent.permissions(member_id) if self.agent else None)
        ctx = ToolContext(conn=self.conn, turn=turn, channel=msg.channel,
                          services={**(self.agent.services if self.agent else {}),
                                    **self._chat_services(msg)})
        return turn, ctx

    async def _confirmed(self, msg: Inbound, member_id: int, r) -> None:
        chosen = self.agent.registry().get(r.tool) if self.agent else None
        if chosen is None:
            receipts.set_state(self.conn, r.id, "refused")
            await self._say(msg, t("bot.stale"))
            return
        turn, ctx = self._turn(msg, member_id)
        try:
            result = await run_tool(chosen, turn, ctx, r.args, confirmed=True)
        except NotAllowed:
            receipts.set_state(self.conn, r.id, "refused")
            await self._say(msg, t("api.forbidden"))
            return
        receipts.set_state(self.conn, r.id, "done")
        undo = (result.receipt or {}).get("undo")
        receipts.set_undo(self.conn, r.id, undo)
        buttons = [self._undo_button(r.id, undo)] if undo else None
        # Not `result.text`: that is written for the model, in English.
        switched = (result.receipt or {}).get("entities")
        done = self._switched(r.tool, result.receipt) if switched else t("bot.done")
        await self._say(msg, done, buttons, [r.id])

    async def _undo(self, msg: Inbound, member_id: int, r) -> None:
        await self._apply_undo(msg, member_id, r.undo or {})

    async def _apply_undo(self, msg: Inbound, member_id: int, u: dict) -> None:
        if u.get("kind") == "undo_all":
            # A Routine's [Undo all] (D57): each step's own undo, last first, so
            # the house goes back the way it came.
            for step in reversed(u.get("undos", [])):
                try:
                    await self._apply_undo(msg, member_id, step)
                except Exception:
                    log.exception("undoing one step of a routine failed")
        elif u.get("kind") == "delete_calendar_event":
            build = (self.agent.services if self.agent else {}).get("calendar")
            cal = build(member_id) if build else None
            if cal is not None and hasattr(cal, "delete_event"):
                await asyncio.to_thread(cal.delete_event, u["source_uid"], u["uid"])
        elif u.get("kind") == "reopen_notes":
            viewer = members_mod.Viewer(member_id)
            for note_id in u.get("ids", []):
                try:
                    store.get_note(self.conn, note_id, viewer=viewer)   # still visible?
                except KeyError:
                    continue
                store.mark_undone(self.conn, note_id)
        elif u.get("kind") == "delete_event":
            remove = (self.agent.services if self.agent else {}).get("delete_event")
            if remove is not None:
                await remove(member_id, u["note_id"])
        elif u.get("kind") == "cancel_scheduled":
            # `cancel` checks the owner itself: only the author's rows move.
            scheduled.cancel(self.conn, u["id"], member_id)
        elif u.get("kind") == "delete_note":
            # Only a Note the presser owns: the Receipt is theirs, but the check is
            # cheap and a forged button must not delete somebody else's words.
            note = store.get_note(self.conn, u["note_id"], viewer=members_mod.Viewer(member_id))
            if note.owner_id == member_id:
                store.soft_delete(self.conn, note.id)
        elif u.get("tool") and self.agent is not None:
            chosen = self.agent.registry().get(u["tool"])
            if chosen is not None:
                turn, ctx = self._turn(msg, member_id)
                await run_tool(chosen, turn, ctx, u.get("args", {}), confirmed=True)

    def _scheduled_text(self, member_id: int) -> str:
        """`/agendado`: the Member's scheduled actions and timers, with no model."""
        now = datetime.now()
        lines = [t("bot.scheduled_item", id=s.id, what=s.summary, at=f"{s.next_at:%d/%m %H:%M}",
                   left=scheduled.remaining(s.next_at, now),
                   repeat=f" · {s.repeat} {s.time_of_day}" if s.time_of_day else "")
                 for s in scheduled.active(self.conn, member_id)]
        for n in store.upcoming_reminders(self.conn, member_id):
            at = datetime.fromisoformat(n.remind_at)
            lines.append(t("bot.reminder_item", text=n.text, at=f"{at:%d/%m %H:%M}",
                           left=scheduled.remaining(at, now)))
        from . import chat_rules, routines

        for rule in chat_rules.visible(self.conn, member_id):
            lines.append(f"• ⚙️ {rule.name}: {rule.describe()}")
        for routine in routines.visible(self.conn, member_id):
            lines.append(f"• 🏠 {routine.name}"
                         + (f" — “{'”, “'.join(routine.phrases)}”" if routine.phrases else ""))
        if not lines:
            return t("bot.nothing_scheduled")
        return "\n".join([t("bot.scheduled_title"), *lines])

    def menu(self) -> list[tuple[str, str]]:
        """The `/` menu Telegram shows (D61), in the installation's language."""
        return [(c, t(f"bot.menu_{c}")) for c in
                ("help", "board", "agendado", "conectar_agenda", "conectar_email", "satellite",
                 "satellites")]

    def owner_menu(self) -> list[tuple[str, str]]:
        """The Owner's own menu: everyone's, plus `/moradores`."""
        return [*self.menu(), ("moradores", t("bot.menu_moradores"))]

    def owner_chat(self) -> str | None:
        return _identity_of(self.conn, self.channel.name, OWNER_ID)

    # ── Chat Rules (F10, D45, D46) ──────────────────────────────────────────
    async def fire_chat_rules(self, kind: str, entity: str | None, new: str,
                              now: datetime | None = None) -> int:
        """Run the chat Rules a signal starts, each with its creator's Grant read
        now, and tell the creator every time, with [Undo] and [Switch it off].
        Returns how many acted."""
        from . import chat_rules

        if self.agent is None:
            return 0
        now = now or datetime.now()
        acted = 0
        for rule in chat_rules.triggered(self.conn, kind, entity, new, now):
            if not chat_rules.in_window(rule, now) or not await self._rule_condition(rule):
                continue
            chat_rules.mark_fired(self.conn, rule.id, now)
            chat = _identity_of(self.conn, self.channel.name, rule.owner_id)
            chosen = self.agent.registry().get(rule.action_tool)
            turn = Turn(member_id=rule.owner_id, conversation_id=chat or "", in_group=False,
                        permissions=self.agent.permissions(rule.owner_id))
            ctx = ToolContext(conn=self.conn, turn=turn, channel=self.channel.name,
                              services=dict(self.agent.services))
            try:
                if chosen is None:
                    raise NotAllowed(f"{rule.action_tool} is gone")
                result = await run_tool(chosen, turn, ctx, dict(rule.action_args))
            except NotAllowed:
                result = None
            except Exception:
                log.exception("chat rule %s failed", rule.name)
                result = None
            if chat is None:
                continue
            off = receipts.record(self.conn, channel=self.channel.name, conversation_id=chat,
                                  message_id=None, member_id=rule.owner_id, tool="chat_rule",
                                  args={"id": rule.id}, state="pending",
                                  summary=f"rule {rule.name} fired")
            buttons = [Button(t("bot.btn_rule_off"), f"ruleoff:{off}")]
            if result is None or result.receipt is None:
                await self._push(self.channel.name, chat,
                                 t("bot.rule_failed", name=rule.name), buttons, off)
                continue
            acted += 1
            done = result.receipt
            rid = receipts.record(self.conn, channel=self.channel.name, conversation_id=chat,
                                  message_id=None, member_id=rule.owner_id,
                                  tool=rule.action_tool, summary=done.get("summary", rule.name),
                                  args=rule.action_args, undo=done.get("undo"))
            what = (self._switched(rule.action_tool, done) if done.get("entities")
                    else t("bot.done"))
            if done.get("undo"):
                buttons.insert(0, self._undo_button(rid, done["undo"]))
            await self._push(self.channel.name, chat,
                             t("bot.rule_fired", name=rule.name, what=what), buttons, rid)
        return acted

    async def _rule_condition(self, rule) -> bool:
        if not rule.cond_entity:
            return True
        services = self.agent.services if self.agent else {}
        app = services.get("app")
        home = app.state.home if app is not None else services.get("home")
        try:
            state = (await home.state(rule.cond_entity)).get("state", "")
        except Exception:
            return False          # cannot tell: do not act on a guess
        return str(state).lower() == (rule.cond_state or "").lower()

    # ── Reminders on the chat (F9) ──────────────────────────────────────────
    async def remind(self, note, late: str = "") -> bool:
        """A due Reminder, to its writer's private chat, with [Done] and [+10 min].

        Only to the writer: their Reminder is as private as their Note (D6).
        False when they have no chat on this Channel yet.
        """
        if not getattr(self.channel, "configured", True) or not hasattr(self.channel, "send"):
            return False
        chat = _identity_of(self.conn, self.channel.name, note.owner_id)
        if chat is None:
            return False
        rid = receipts.record(self.conn, channel=self.channel.name, conversation_id=chat,
                              message_id=None, member_id=note.owner_id, tool="reminder",
                              args={"note_id": note.id}, state="pending",
                              summary=f"reminded of #{note.id}")
        await self._push(self.channel.name, chat, t("bot.reminder", text=note.text, late=late),
                         [Button(t("bot.btn_reminder_done"), f"done:{rid}"),
                          Button(t("bot.btn_snooze"), f"snooze:{rid}")], rid)
        return True

    def _reminder_button(self, kind: str, member_id: int, r) -> str:
        """[Done] or [+10 min] on a Reminder; returns what to answer."""
        note = store.get_note(self.conn, r.args["note_id"], viewer=members_mod.Viewer(member_id))
        if kind == "done":
            store.mark_done(self.conn, note.id)
            receipts.set_state(self.conn, r.id, "done")
            return t("bot.reminder_done")
        until = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=10)
        store.snooze(self.conn, note.id, until)
        # `done` for this button: the receipts table allows only four states, and
        # the next ring comes with a Receipt of its own.
        receipts.set_state(self.conn, r.id, "done")
        return t("bot.reminder_snoozed", at=f"{until:%H:%M}")

    async def event_created(self, note, title: str, start: datetime) -> bool:
        """The review put an event in the writer's calendar (T5.2): say so in
        their private chat, with [Undo]. No confirmation was asked — this message
        is what makes that autonomy visible."""
        chat = _identity_of(self.conn, self.channel.name, note.owner_id)
        if chat is None or not hasattr(self.channel, "send"):
            return False
        undo = {"kind": "delete_event", "note_id": note.id}
        rid = receipts.record(self.conn, channel=self.channel.name, conversation_id=chat,
                              message_id=None, member_id=note.owner_id, tool="calendar",
                              summary=f"created event {title!r} from #{note.id}", undo=undo)
        await self._push(self.channel.name, chat,
                         t("bot.event_created", title=title, at=f"{start:%d/%m %H:%M}"),
                         [self._undo_button(rid, undo)], rid)
        return True

    # ── Scheduled actions (F9, D39) ─────────────────────────────────────────
    async def run_scheduled(self, now: datetime | None = None) -> int:
        """Run what is due, each with its author's Grant as it is NOW.

        The permissions are read at fire time, not copied from when it was
        scheduled: a Grant revoked in between must win, as it does for a button
        (T4.4). Each outcome is told in the conversation it was asked in, so
        nothing happens in the house without a message saying so. Returns how
        many ran.
        """
        if self.agent is None:
            return 0
        now = now or datetime.now()
        ran = 0
        for s in scheduled.due(self.conn, now):
            late = scheduled.is_late(s, now)
            # Moved on first: a Tool that hangs must not fire twice.
            scheduled.advance(self.conn, s, now)
            if late:
                await self._tell_scheduled(s, t("bot.scheduled_missed", what=s.summary,
                                                at=f"{s.next_at:%H:%M}"))
                continue
            chosen = self.agent.registry().get(s.tool)
            turn = Turn(member_id=s.member_id, conversation_id=s.conversation_id,
                        in_group=s.in_group, permissions=self.agent.permissions(s.member_id))
            ctx = ToolContext(conn=self.conn, turn=turn, channel=s.channel,
                              services=dict(self.agent.services))
            try:
                if chosen is None:
                    raise NotAllowed(f"{s.tool} is gone")
                result = await run_tool(chosen, turn, ctx, s.args)
            except NotAllowed:
                await self._tell_scheduled(s, t("bot.scheduled_refused", what=s.summary))
                continue
            except Exception:
                log.exception("scheduled #%d (%s) failed", s.id, s.tool)
                await self._tell_scheduled(s, t("bot.scheduled_failed", what=s.summary))
                continue
            if result.receipt is None:
                # The acting Tools answer "nothing you may switch" instead of
                # raising, and a Grant narrowed since scheduling ends up here. A
                # "Done" for a light that stayed off would be a lie.
                await self._tell_scheduled(s, t("bot.scheduled_refused", what=s.summary))
                continue
            ran += 1
            done = result.receipt
            rid = receipts.record(self.conn, channel=s.channel, conversation_id=s.conversation_id,
                                  message_id=None, member_id=s.member_id, tool=s.tool,
                                  summary=done.get("summary", s.summary), args=s.args,
                                  undo=done.get("undo"))
            if done.get("entities"):
                line = self._switched(s.tool, done)
            else:
                line = t("bot.done") + f" ({s.summary})"
            undo = done.get("undo")
            await self._tell_scheduled(s, t("bot.scheduled_ran", done=line),
                                       [self._undo_button(rid, undo)] if undo else None, rid)
        return ran

    async def _tell_scheduled(self, s, text: str, buttons: list[Button] | None = None,
                              rid: int | None = None) -> None:
        await self._push(s.channel, s.conversation_id, text, buttons, rid)

    async def _push(self, channel: str, conversation_id: str, text: str,
                    buttons: list[Button] | None = None, rid: int | None = None) -> None:
        """A message nobody just asked for, kept in the conversation's memory and
        tied to its Receipt, so "what was that?" in reply to it has an answer."""
        try:
            sent = await self.channel.send(conversation_id, text, buttons)
        except ChannelError as e:
            log.warning("could not send to %s: %s", conversation_id, e)
            return
        memory.record(self.conn, channel=channel, conversation_id=conversation_id,
                      message_id=sent, member_id=None, text=text)
        if sent and rid is not None:
            receipts.answered_by(self.conn, [rid], sent)

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
                # A spoken message is handled like a typed one (D34): a question
                # gets an answer, a thought becomes a Note. What was heard is shown
                # first, so a wrong transcript is visible before anything else.
                await self.channel.reply(msg, t("bot.voice_heard", text=text))
                await self._text(msg, member_id, text)
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
