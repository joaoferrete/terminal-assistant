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
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from . import agent as agent_mod
from . import builtin_tools, i18n, memory, prerouter, receipts, store
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
            # Only allowlisted groups are listened to at all (D8).
            if msg.conversation_id in self.allowed_groups():
                await self._group(msg)
            return
        member_id, just_paired = self._recognise(msg)
        if member_id is None:
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
            elif command == "/board":
                url = self.board_link(member_id)
                await self.channel.reply(
                    msg, t("bot.board_link", url=url) if url else t("bot.board_unreachable")
                )
            else:
                await self.channel.reply(msg, t("bot.unknown_command"))
            return

        await self._text(msg, member_id, text)

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
                          services={**deps.services, "message": msg})
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
                )
        except agent_mod.AgentFailed as e:
            log.warning("agent failed, capturing instead: %s", e)
            note = self.capture(text, owner_id=member_id)
            await self._say(msg, t("bot.captured_offline", line=self._captured_line(note)))
            return

        parts, buttons, rids = [], [], []
        # What the Tools did this turn, each with its Receipt and the last one's undo.
        for done in turn.receipts:
            rid = self._receipt(msg, member_id, tool=done["tool"], summary=done["summary"],
                                undo=done.get("undo"))
            rids.append(rid)
            if done.get("undo"):
                buttons = [Button(t("bot.btn_undo"), f"undo:{rid}")]
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
            result = await run_tool(chosen, turn, ctx, {"target": target})
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
        await self._say(msg, t("bot.home_done", what=result.text), buttons, rids)
        return True

    def _propose(self, msg: Inbound, member_id: int, pending) -> tuple[int, str]:
        """Store an action waiting for the asker's button (D27) and describe it."""
        rid = self._receipt(
            msg, member_id, tool=pending.tool.name, args=pending.tool_args, state="pending",
            summary=f"{pending.tool.name} {json.dumps(pending.tool_args, ensure_ascii=False)}",
        )
        return rid, t("bot.confirm_needed", action=pending.tool.description,
                      args=", ".join(f"{k}={v}" for k, v in pending.tool_args.items()))

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
        elif kind == "no" and r.state == "pending":
            receipts.set_state(self.conn, r.id, "refused")
            await self.channel.answered(msg, t("bot.cancelled"))
        elif kind == "undo" and r.state == "done" and r.undo:
            await self._undo(msg, member_id, r)
            receipts.set_state(self.conn, r.id, "undone")
            await self.channel.answered(msg, t("bot.undone"))
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
                                    "message": msg})
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
        buttons = [Button(t("bot.btn_undo"), f"undo:{r.id}")] if undo else None
        await self._say(msg, result.text, buttons, [r.id])

    async def _undo(self, msg: Inbound, member_id: int, r) -> None:
        u = r.undo or {}
        if u.get("kind") == "delete_note":
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
