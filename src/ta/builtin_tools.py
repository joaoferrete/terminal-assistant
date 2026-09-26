"""The Tools `ta` ships with. Household ones go in `~/.config/ta/tools/`.

Every read Tool filters by who is asking before anything reaches the model (D28,
invariant 9): the context it gets is already what the asker may see.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import memory, store
from .members import Viewer
from .tools import Source, ToolResult, Turn, tool


@dataclass
class ToolContext:
    conn: sqlite3.Connection
    turn: Turn
    channel: str
    # The daemon's collaborators, for the Tools that act (home, capture). Kept as
    # a plain bag so a Tool file from the household can use them too.
    services: dict[str, Any]

    @property
    def viewer(self) -> Viewer:
        return Viewer(self.turn.member_id, in_group=self.turn.in_group)


def _quote(text: str) -> str:
    # One line each, so a Note cannot fake the structure of the result around it.
    return " ".join(text.split())


@tool(
    description="Search the asker's notes (and the household's) for words. Use it "
    "for questions about their own data: tasks, what they wrote down, deadlines.",
    args={"query": "the words to look for"},
)
async def notes_search(ctx: ToolContext, query: str) -> ToolResult:
    found = store.search_notes(ctx.conn, query, viewer=ctx.viewer)
    if not found:
        return ToolResult(text="no note matches")
    lines = [
        f"#{n.id} [{n.status}]"
        + (f" due {n.due}" if n.due else "")
        + f": {_quote(n.text)}"
        for n in found
    ]
    return ToolResult(
        text="\n".join(lines),
        sources=[Source("note", str(n.id), _quote(n.text)[:60]) for n in found],
        # Somebody else's Note (a household item another Member wrote) is text the
        # asker did not write: it taints the turn (D27).
        tainted=any(n.owner_id != ctx.turn.member_id for n in found),
    )


@tool(
    description="List the asker's open tasks by deadline: overdue, due today, due this "
    "week. Use it for 'what is overdue', 'what do I have today', 'what is due this week'.",
    args={"which": "overdue, today, week, or all"},
)
async def notes_due(ctx: ToolContext, which: str = "all") -> ToolResult:
    # The first real question the bot failed was "quais são minhas notas vencidas?":
    # with only a word search, "vencidas" matched nothing and the model spent every
    # step looking. The Horizon is derived by the store, never by the model (ADR 0010).
    wanted = {"overdue": ("overdue",), "today": ("today",), "week": ("week",)}.get(
        which.strip().lower(), ("overdue", "today", "week"))
    open_ = [n for n in store.by_urgency(store.list_notes(ctx.conn, viewer=ctx.viewer))
             if n.due and store.horizon(n.due) in wanted]
    if not open_:
        return ToolResult(text=f"no open task in {', '.join(wanted)}")
    lines = [f"#{n.id} [{store.horizon(n.due)}] due {n.due}: {_quote(n.text)}" for n in open_]
    return ToolResult(
        text="\n".join(lines),
        sources=[Source("note", str(n.id), _quote(n.text)[:60]) for n in open_],
        tainted=any(n.owner_id != ctx.turn.member_id for n in open_),
    )


@tool(
    description="Search what was said earlier in THIS conversation. Use it when "
    "the asker refers to something from before that is not in the recent messages.",
    args={"query": "the words to look for"},
)
async def memory_search(ctx: ToolContext, query: str) -> ToolResult:
    found = memory.search(ctx.conn, channel=ctx.channel,
                          conversation_id=ctx.turn.conversation_id, query=query)
    if not found:
        return ToolResult(text="nothing said here matches")
    lines = [f"{m.at} {'bot' if m.member_id is None else 'member'}: {_quote(m.text)}"
             for m in found]
    # In a group, what comes back was written by other people.
    return ToolResult(text="\n".join(lines), tainted=ctx.turn.in_group)


@tool(
    description="Show what is on a List (shopping and the like), or name the Lists.",
    args={"list": "the List's name, or empty to see which Lists exist"},
)
async def list_show(ctx: ToolContext, list: str = "") -> ToolResult:  # noqa: A002
    lists = store.lists_for(ctx.conn, viewer=ctx.viewer)
    if not list:
        return ToolResult(text="Lists: " + (", ".join(lv.name for lv in lists) or "none"))
    lv = next((x for x in lists if x.name.lower() == list.strip().lower()), None)
    if lv is None:
        return ToolResult(text=f"no List named {list!r}; there are: "
                               + ", ".join(x.name for x in lists))
    items = [f"#{n.id}: {_quote(n.text)}" for n in lv.items] or ["(empty)"]
    return ToolResult(
        text=f"{lv.name}:\n" + "\n".join(items),
        sources=[Source("note", str(n.id), _quote(n.text)[:60]) for n in lv.items],
        tainted=any(n.owner_id != ctx.turn.member_id for n in lv.items),
    )


# ── Acting Tools (F4, T4.2) ─────────────────────────────────────────────────
# Each one checks the asker's Grant per argument, because only it knows which
# Entity or List an argument names, and each returns the Receipt with its undo.

def _home(ctx: ToolContext):
    app = ctx.services.get("app")
    return app.state.home if app is not None else ctx.services.get("home")


async def _targets(ctx: ToolContext, target: str) -> list[str]:
    from .config import resolve_targets

    found = resolve_targets(target.strip() or "luz", await _home(ctx).entities("light.", "switch."))
    return [e for e in found if ctx.turn.permissions.entity(e)]


@tool(
    description="Turn on lights or plugs: a room, a name, a group like 'luz', or an "
    "entity_id. For a colour, give it in English CSS names (blue, red, orange, purple), "
    "as #rrggbb, or as a white temperature like 2700K (warm) or 6500K (cold).",
    args={"target": "what to turn on", "brightness": "0-100 for lights, or empty for full",
          "color": "a colour for lights, or empty"},
    grant="home",
    changes_state=True,
)
async def home_on(ctx: ToolContext, target: str, brightness: str = "",
                  color: str = "") -> ToolResult:
    entities = await _targets(ctx, target)
    if not entities:
        return ToolResult(text=f"nothing you may switch matches {target!r}")
    level = int(brightness) if str(brightness).strip().isdigit() else 100
    for e in entities:
        if color.strip() and e.startswith("light."):
            # A plug in the same room just turns on; only lights take the colour.
            await _home(ctx).set_color(e, color, level if brightness.strip() else None)
        else:
            await _home(ctx).switch_on(e, level)
    return ToolResult(
        text="turned on: " + ", ".join(entities),
        receipt={"summary": "turned on " + ", ".join(entities), "entities": entities,
                 "undo": {"tool": "home_off", "args": {"target": ",".join(entities)}}},
    )


@tool(
    description="Turn off lights or plugs: a room, a name, a group like 'luz', or an entity_id",
    args={"target": "what to turn off; 'tudo' for everything the asker may switch"},
    grant="home",
    changes_state=True,
)
async def home_off(ctx: ToolContext, target: str) -> ToolResult:
    # An undo of home_on hands back a comma-separated list of entity_ids.
    if "," in target:
        entities = [e for e in target.split(",") if ctx.turn.permissions.entity(e)]
    else:
        entities = await _targets(ctx, target)
    if not entities:
        return ToolResult(text=f"nothing you may switch matches {target!r}")
    for e in entities:
        await _home(ctx).turn_off(e)
    return ToolResult(
        text="turned off: " + ", ".join(entities),
        receipt={"summary": "turned off " + ", ".join(entities), "entities": entities,
                 "undo": {"tool": "home_on", "args": {"target": ",".join(entities)}}
                 if len(entities) == 1 else None},
    )


@tool(
    description="Add an item to a List (shopping and the like)",
    args={"list": "the List's name", "item": "what to add"},
    grant="lists",
    changes_state=True,
)
async def list_add(ctx: ToolContext, list: str, item: str) -> ToolResult:  # noqa: A002
    lv = next((x for x in store.lists_for(ctx.conn, viewer=ctx.viewer)
               if x.name.lower() == list.strip().lower()), None)
    if lv is None:
        return ToolResult(text=f"no List named {list!r}")
    if lv.scope == "household" and not ctx.turn.permissions.list_(lv.name):
        return ToolResult(text=f"you may not add to {lv.name}")
    capture = ctx.services.get("capture")
    note = (capture(item.strip(), owner_id=ctx.turn.member_id, list_id=lv.id) if capture
            else store.add_note(ctx.conn, item.strip(), owner_id=ctx.turn.member_id,
                                list_id=lv.id))
    return ToolResult(
        text=f"added to {lv.name}: {item.strip()} (#{note.id})",
        receipt={"summary": f"added #{note.id} to {lv.name}",
                 "undo": {"kind": "delete_note", "note_id": note.id}},
    )


@tool(
    description="Remember how to address this member: a name to call them, and a tone",
    args={"name": "what to call them, or empty to keep", "tone": "how to talk, or empty"},
    changes_state=True,
)
async def persona_set(ctx: ToolContext, name: str = "", tone: str = "") -> ToolResult:
    import json

    row = ctx.conn.execute("SELECT persona FROM members WHERE id = ?",
                           (ctx.turn.member_id,)).fetchone()
    persona = json.loads(row[0]) if row and row[0] else {}
    if name.strip():
        persona["name"] = name.strip()
    if tone.strip():
        persona["tone"] = tone.strip()
    ctx.conn.execute("UPDATE members SET persona = ? WHERE id = ?",
                     (json.dumps(persona, ensure_ascii=False), ctx.turn.member_id))
    return ToolResult(text=f"persona now: {persona}",
                      receipt={"summary": f"persona set to {persona}"})


def persona_line(conn, member_id: int) -> str:
    """The Persona as a line for the agent's system prompt (D13): it belongs to
    the Member and applies in every conversation."""
    import json

    row = conn.execute("SELECT persona FROM members WHERE id = ?", (member_id,)).fetchone()
    p = json.loads(row[0]) if row and row[0] else {}
    bits = []
    if p.get("name"):
        bits.append(f"call them {p['name']}")
    if p.get("tone"):
        bits.append(f"tone: {p['tone']}")
    return "; ".join(bits)


@tool(
    description="Search the web for current or factual information the notes do not "
    "have: news, weather, opening hours, prices, anything recent",
    args={"question": "what to find out, as a full question"},
    third_party=True,
)
async def web_search(ctx: ToolContext, question: str) -> ToolResult:
    """A summary with links, never raw pages (D26). Its text was written by
    strangers, so it taints the turn (D27) — through `third_party`, which the
    code honours whatever the page says."""
    app = ctx.services.get("app")
    llm = ctx.services.get("llm") or (app.state.llm if app is not None else None)
    if llm is None:
        return ToolResult(text="web search is not available here")
    from .providers import LLMUnavailable

    try:
        text, links = await llm.search(question)
    except LLMUnavailable as e:
        return ToolResult(text=f"web search failed: {e}")
    return ToolResult(text=text, sources=[Source("web", uri, title) for uri, title in links])


@tool(
    description="Set up the member's daily summary: turn it on or off, the hour, or "
    "which sections it has (calendar, notes, lists, weather)",
    args={"enabled": "yes or no, or empty to keep", "time": "HH:MM, or empty to keep",
          "sections": "comma-separated section names, or empty to keep"},
    changes_state=True,
)
async def digest_set(ctx: ToolContext, enabled: str = "", time: str = "",
                     sections: str = "") -> ToolResult:
    from . import digest

    s = digest.Settings.load(ctx.conn, ctx.turn.member_id)
    if enabled.strip().lower() in ("yes", "sim", "true", "on"):
        s.enabled = True
    elif enabled.strip().lower() in ("no", "não", "nao", "false", "off"):
        s.enabled = False
    if time.strip():
        parsed = digest.parse_time(time)
        if parsed is None:
            return ToolResult(text=f"not a time: {time!r}; use HH:MM")
        s.time, s.enabled = parsed, True
    if sections.strip():
        chosen = tuple(x.strip() for x in sections.split(",") if x.strip() in digest.SECTIONS)
        if chosen:
            s.sections = chosen
    s.save(ctx.conn, ctx.turn.member_id)
    state = f"on at {s.time}" if s.enabled else "off"
    return ToolResult(text=f"daily summary {state}; sections: {', '.join(s.sections)}",
                      receipt={"summary": f"digest {state}"})


@tool(
    description="Build the member's daily summary right now and return it",
)
async def digest_now(ctx: ToolContext) -> ToolResult:
    build = ctx.services.get("digest")
    if build is None:
        return ToolResult(text="the summary is not available here")
    return ToolResult(text=await build(ctx.turn.member_id))


@tool(
    description="Search the member's own documents and code — the folders their "
    "computer indexes — by meaning. Use it for 'how did I solve X', 'where did I "
    "write about Y', or anything that sounds like their files rather than their notes.",
    args={"query": "what to look for, in plain words"},
    # Files hold text from many hands (a repository's dependencies, pasted docs):
    # third-party, so it taints the turn (D27).
    third_party=True,
)
async def docs_search(ctx: ToolContext, query: str) -> ToolResult:
    from . import members as members_mod
    from . import rag

    # Never from a group: these are one person's files (decided 2026-09-26).
    if ctx.turn.in_group:
        return ToolResult(text="document search is private; ask in a private chat")
    app = ctx.services.get("app")
    embedder = ctx.services.get("embedder") or (app.state.embedder if app else None)
    if embedder is None or not (ctx.services.get("embedder") or rag.available()):
        return ToolResult(text="document search is not set up on the server")
    member = members_mod.get(ctx.conn, ctx.turn.member_id)
    hits = rag.search(ctx.conn, embedder, query, member_id=ctx.turn.member_id,
                      is_owner=bool(member and member.is_owner))
    if not hits:
        return ToolResult(text="nothing in the indexed folders matches")
    return ToolResult(
        text="\n\n".join(f"{h.path}:\n{h.text}" for h in hits),
        sources=[Source("doc", h.path, h.path.rsplit("/", 1)[-1]) for h in hits],
    )


# ── Scheduled actions (F9, D39) ─────────────────────────────────────────────
def _now(ctx: ToolContext):
    from datetime import datetime

    return ctx.services.get("now", datetime.now)()


def _schedulable(ctx: ToolContext, name: str):
    """A Tool that may run later: one that acts, is not destructive (nobody is
    there to press a confirmation at 07:00), is not scheduling itself, and is in
    the asker's Grant now. The Grant is checked again when it fires."""
    from .tools import allowed, registered

    chosen = registered().get(name)
    if (chosen is None or not chosen.changes_state or chosen.destructive
            or chosen.name.startswith("schedule_") or not allowed(chosen, ctx.turn)):
        return None
    return chosen


@tool(
    description="Run one of your acting tools LATER: 'turn the light on in 10 minutes', "
    "'every day at 7 turn on the bedroom light', 'on weekdays at 22:30 turn everything "
    "off'. Give in_minutes for a delay, or at for a clock time. Changing the daily "
    "summary's hour is digest_set, not this.",
    args={"tool": "the acting tool to run, e.g. home_on",
          "tool_args": "that tool's arguments, as a JSON object",
          "in_minutes": "minutes from now, or empty",
          "at": "HH:MM (the next one), or YYYY-MM-DD HH:MM, or empty",
          "repeat": "once, daily, weekdays or weekends (a repeat needs at=HH:MM)"},
    changes_state=True,
)
async def schedule_action(ctx: ToolContext, tool: str, tool_args: Any = "{}",
                          in_minutes: str = "", at: str = "",
                          repeat: str = "once") -> ToolResult:
    import json

    from . import scheduled

    chosen = _schedulable(ctx, tool.strip())
    if chosen is None:
        return ToolResult(text=f"{tool!r} cannot be scheduled: it is not one of your "
                               "acting tools, or it needs a confirmation")
    try:
        args = json.loads(tool_args) if isinstance(tool_args, str) else dict(tool_args or {})
    except ValueError:
        return ToolResult(text="tool_args is not a JSON object")
    if not isinstance(args, dict) or set(args) - set(chosen.args):
        return ToolResult(text=f"{chosen.name} takes only: {', '.join(chosen.args)}")
    # A target nobody may switch should fail now, while the asker is here to read
    # why — not at 07:00 in a message they may never see.
    if chosen.grant == "home" and not await _targets(ctx, str(args.get("target", ""))):
        return ToolResult(text=f"nothing you may switch matches {args.get('target', '')!r}")
    now = _now(ctx)
    try:
        first, time_of_day = scheduled.when(in_minutes=in_minutes, at=at, repeat=repeat, now=now)
    except scheduled.BadSchedule as e:
        return ToolResult(text=f"not scheduled: {e}")
    repeat = repeat.strip().lower() or "once"
    summary = f"{chosen.name} " + ", ".join(f"{k}={v}" for k, v in args.items())
    sid = scheduled.add(ctx.conn, member_id=ctx.turn.member_id, channel=ctx.channel,
                        conversation_id=ctx.turn.conversation_id, in_group=ctx.turn.in_group,
                        tool=chosen.name, args=args, summary=summary, next_at=first,
                        repeat=repeat, time_of_day=time_of_day, now=now)
    every = f", repeating {repeat} at {time_of_day}" if time_of_day else ""
    return ToolResult(
        text=f"scheduled #{sid}: {summary}, first at {first:%Y-%m-%d %H:%M} "
             f"(in {scheduled.remaining(first, now)}){every}",
        receipt={"summary": f"scheduled #{sid}: {summary}",
                 "undo": {"kind": "cancel_scheduled", "id": sid}},
    )


@tool(
    description="List the asker's scheduled actions with when each runs next and how "
    "long until then. Use it for 'what is scheduled', 'how long until the light "
    "turns on', 'quanto falta'.",
)
async def schedule_list(ctx: ToolContext) -> ToolResult:
    from . import scheduled

    now = _now(ctx)
    rows = scheduled.active(ctx.conn, ctx.turn.member_id)
    if not rows:
        return ToolResult(text="nothing is scheduled")
    lines = [f"#{s.id} {s.summary}: next {s.next_at:%Y-%m-%d %H:%M} "
             f"(in {scheduled.remaining(s.next_at, now)})"
             + (f", repeats {s.repeat} at {s.time_of_day}" if s.time_of_day else "")
             for s in rows]
    return ToolResult(text="\n".join(lines))


@tool(
    description="Cancel one of the asker's scheduled actions, by its number from schedule_list",
    args={"id": "the scheduled action's number"},
    changes_state=True,
)
async def schedule_cancel(ctx: ToolContext, id: str) -> ToolResult:  # noqa: A002
    from . import scheduled

    raw = str(id).strip().lstrip("#")
    if not raw.isdigit() or not scheduled.cancel(ctx.conn, int(raw), ctx.turn.member_id):
        return ToolResult(text=f"no active scheduled action #{raw} of yours")
    return ToolResult(text=f"cancelled #{raw}", receipt={"summary": f"cancelled scheduled #{raw}"})


# ── Files on the Member's own computer (F9, D41) ────────────────────────────
def _state(ctx: ToolContext, name: str):
    app = ctx.services.get("app")
    return ctx.services.get(name) or (getattr(app.state, name, None) if app else None)


async def _ask_computer(ctx: ToolContext, op: str, path: str) -> dict | str:
    """Ask the asker's OWN Satellite. Returns its answer, or why there is none.

    Only the asker's: D37 lets the Owner search every Satellite's index, but live
    access to somebody's laptop is another thing, and was decided separately.
    """
    if ctx.turn.in_group:
        return "files are private; ask in a private chat"
    hub = _state(ctx, "hub")
    if hub is None or not hub.connected(ctx.turn.member_id):
        return "your computer is not connected right now (its Satellite is off or asleep)"
    try:
        answer = await hub.ask(ctx.turn.member_id, {"kind": "files", "op": op, "path": path})
    except TimeoutError:
        return "your computer did not answer; its Satellite may need updating"
    return answer.get("error") or answer


@tool(
    description="List a folder on the asker's own computer, among the folders it shares. "
    "Empty path lists the shared folders themselves.",
    args={"path": "a folder as a previous listing showed it, or empty"},
    # A file name is text somebody else may have chosen (a download, a clone).
    third_party=True,
)
async def files_list(ctx: ToolContext, path: str = "") -> ToolResult:
    answer = await _ask_computer(ctx, "list", path)
    if isinstance(answer, str):
        return ToolResult(text=answer)
    entries = answer.get("entries", [])
    lines = [f"{e['path']}{'/' if e.get('dir') else ''}"
             + ("" if e.get("dir") else f" ({e.get('size', 0)} bytes)") for e in entries]
    return ToolResult(text="\n".join(lines) or "(empty)")


@tool(
    description="Read a text file on the asker's own computer (notes, code, markdown, "
    "config). For a PDF, an image or anything to keep, use files_send.",
    args={"path": "the file, as files_list showed it"},
    third_party=True,
)
async def files_read(ctx: ToolContext, path: str) -> ToolResult:
    answer = await _ask_computer(ctx, "read", path)
    if isinstance(answer, str):
        return ToolResult(text=answer)
    more = "\n[truncated]" if answer.get("truncated") else ""
    return ToolResult(text=f"{answer['path']}:\n{answer['text']}{more}",
                      sources=[Source("doc", answer["path"], answer["path"].rsplit("/", 1)[-1])])


@tool(
    description="Send a file from the asker's own computer to them, here in this chat",
    args={"path": "the file, as files_list showed it"},
)
async def files_send(ctx: ToolContext, path: str) -> ToolResult:
    import base64

    channel = _state(ctx, "channel")
    if channel is None or not hasattr(channel, "send_document"):
        return ToolResult(text="this chat cannot receive files")
    answer = await _ask_computer(ctx, "send", path)
    if isinstance(answer, str):
        return ToolResult(text=answer)
    # To the conversation the asker is in, which in a private chat is theirs
    # alone: `_ask_computer` already refused a group.
    await channel.send_document(ctx.turn.conversation_id, answer["name"],
                                base64.b64decode(answer["data"]))
    return ToolResult(text=f"sent {answer['name']} to the member; it is in the chat now")


# ── Mail (F9, D40) ──────────────────────────────────────────────────────────
def _mailbox(ctx: ToolContext):
    """The asker's own Gmail, or why there is none. Private chats only."""
    if ctx.turn.in_group:
        return "mail is private; ask in a private chat"
    build = ctx.services.get("mail")
    box = build(ctx.turn.member_id) if build else None
    if box is None or not box.available:
        return "no mailbox is connected; the member can send /conectar_email"
    return box


@tool(
    description="Search the asker's own Gmail, only when they ask about their email. "
    "Takes Gmail search syntax: words, from:, subject:, newer_than:7d, is:unread.",
    args={"query": "a Gmail search, e.g. 'from:banco newer_than:7d'"},
    # Mail is written by strangers: it taints the turn, so no email can make the
    # bot act without the asker's confirmation (D27, D40).
    third_party=True,
)
async def mail_search(ctx: ToolContext, query: str) -> ToolResult:
    import asyncio

    from .sensors.gmail import MailError

    box = _mailbox(ctx)
    if isinstance(box, str):
        return ToolResult(text=box)
    try:
        found = await asyncio.to_thread(box.search, query)
    except MailError as e:
        return ToolResult(text=f"mail search failed: {e}")
    if not found:
        return ToolResult(text="no message matches")
    return ToolResult(text="\n".join(
        f"[{m['id']}] {m['date']} · {_quote(m['from'])} · {_quote(m['subject'])}: "
        f"{_quote(m['snippet'])}" for m in found))


@tool(
    description="Read one of the asker's emails in full, by the id mail_search gave",
    args={"id": "the message id, exactly as mail_search showed it"},
    third_party=True,
)
async def mail_read(ctx: ToolContext, id: str) -> ToolResult:  # noqa: A002
    import asyncio

    from .sensors.gmail import MailError

    box = _mailbox(ctx)
    if isinstance(box, str):
        return ToolResult(text=box)
    try:
        m = await asyncio.to_thread(box.read, id.strip().strip("[]"))
    except MailError as e:
        return ToolResult(text=f"could not read it: {e}")
    more = "\n[truncated]" if m["truncated"] else ""
    return ToolResult(text=f"From: {m['from']}\nDate: {m['date']}\nSubject: {m['subject']}"
                           f"\n\n{m['text']}{more}")
