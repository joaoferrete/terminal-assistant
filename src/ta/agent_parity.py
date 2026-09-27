"""Parity: what a Member can do on the board, in the CLI or by command, the agent
can do for them (F10, D47).

Each Tool here wraps something that already exists — a store function, a daemon
collaborator, a bot command — so there is one way to do each thing, and the agent
is one more door to it, with the same Grant. Two lines it does not cross: it
never edits `config.toml` or `.env` (it explains how, from the guide), and nothing
it does is permanent (deleting goes to the trash, with [Undo]).

The Tools that hand out something personal — a board link, a consent link, a
Satellite code — **send it themselves** through the bot, instead of returning it
for the model to copy: a model retyping a 200-character OAuth URL is how a link
gets one character wrong.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from . import store
from .builtin_tools import ToolContext, _quote
from .members import Viewer
from .tools import ToolResult, tool

log = logging.getLogger("ta.agent")

PRIORITIES = ("high", "medium", "low")
STATUSES = ("todo", "doing", "hold")


def _ids(raw: str) -> list[int]:
    return list(dict.fromkeys(
        int(x) for x in str(raw).replace("#", " ").replace(",", " ").split() if x.isdigit()))


def _own(ctx: ToolContext, note_id: int):
    """One of the asker's own Notes, trash included, or None. Editing someone
    else's words is not something the board allows either."""
    try:
        note = store.get_note(ctx.conn, note_id, viewer=Viewer(ctx.turn.member_id))
    except KeyError:
        return None
    return note if note.owner_id == ctx.turn.member_id else None


def _private(ctx: ToolContext, what: str) -> str | None:
    return f"{what} is private; ask in a private chat" if ctx.turn.in_group else None


# ── Notes ───────────────────────────────────────────────────────────────────
@tool(
    description="Change one of the asker's notes: its text, deadline or priority. Take "
    "the number from notes_search or notes_due first.",
    args={"id": "the note number",
          "text": "the new text, or empty to keep",
          "due": "YYYY-MM-DD, 'none' to remove the deadline, or empty to keep",
          "priority": "high, medium, low, 'none', or empty to keep"},
    changes_state=True,
)
async def notes_edit(ctx: ToolContext, id: str, text: str = "", due: str = "",  # noqa: A002
                     priority: str = "") -> ToolResult:
    ids = _ids(id)
    note = _own(ctx, ids[0]) if ids else None
    if note is None:
        return ToolResult(text=f"no note #{id} of yours")
    before = {"id": str(note.id), "text": note.text, "due": note.due or "none",
              "priority": note.priority or "none"}
    changed = []
    if text.strip() and text.strip() != note.text:
        ctx.conn.execute("UPDATE notes SET text = ? WHERE id = ?", (text.strip(), note.id))
        changed.append("text")
    if due.strip():
        if due.strip().lower() == "none":
            new_due = None
        else:
            try:
                new_due = date.fromisoformat(due.strip())
            except ValueError:
                return ToolResult(text=f"not a date: {due!r}; use YYYY-MM-DD")
        remind = datetime.fromisoformat(note.remind_at) if note.remind_at else None
        store.set_schedule(ctx.conn, note.id, due=new_due, remind_at=remind)
        changed.append("deadline")
    if priority.strip():
        p = priority.strip().lower()
        if p not in (*PRIORITIES, "none"):
            return ToolResult(text=f"priority is one of {', '.join(PRIORITIES)} or none")
        store.set_priority(ctx.conn, note.id, None if p == "none" else p)
        changed.append("priority")
    if not changed:
        return ToolResult(text="nothing to change")
    return ToolResult(text=f"#{note.id}: changed {', '.join(changed)}",
                      receipt={"summary": f"edited #{note.id} ({', '.join(changed)})",
                               "undo": {"tool": "notes_edit", "args": before}})


@tool(
    description="Set the asker's notes to 'todo', 'doing' (in progress) or 'hold' (on "
    "hold). To finish one, use notes_done.",
    args={"ids": "note numbers, comma-separated", "status": "todo, doing or hold"},
    changes_state=True,
)
async def notes_status(ctx: ToolContext, ids: str, status: str) -> ToolResult:
    status = status.strip().lower()
    if status not in STATUSES:
        return ToolResult(text=f"status is one of {', '.join(STATUSES)}; to finish, notes_done")
    notes = [n for n in (_own(ctx, i) for i in _ids(ids)) if n is not None]
    if not notes:
        return ToolResult(text="no such note of yours")
    previous = {n.status for n in notes}
    for n in notes:
        store.set_status(ctx.conn, n.id, status)
    joined = ",".join(str(n.id) for n in notes)
    return ToolResult(
        text=f"{status}: " + ", ".join(f"#{n.id} {_quote(n.text)}" for n in notes),
        receipt={"summary": f"{joined} → {status}",
                 # One undo only when they all came from the same place.
                 "undo": {"tool": "notes_status", "args": {"ids": joined,
                                                           "status": previous.pop()}}
                 if len(previous) == 1 and previous <= set(STATUSES) else None},
    )


@tool(
    description="Delete the asker's notes. They go to the trash and can be restored; "
    "nothing is deleted for good.",
    args={"ids": "note numbers, comma-separated"},
    changes_state=True,
)
async def notes_delete(ctx: ToolContext, ids: str) -> ToolResult:
    notes = [n for n in (_own(ctx, i) for i in _ids(ids)) if n is not None and not n.deleted_at]
    if not notes:
        return ToolResult(text="no such note of yours")
    for n in notes:
        store.soft_delete(ctx.conn, n.id)
    joined = ",".join(str(n.id) for n in notes)
    return ToolResult(
        text="moved to the trash: " + ", ".join(f"#{n.id} {_quote(n.text)}" for n in notes),
        receipt={"summary": f"deleted {joined}",
                 "undo": {"tool": "notes_restore", "args": {"ids": joined}}},
    )


@tool(
    description="Bring notes back from the asker's trash",
    args={"ids": "note numbers, comma-separated"},
    changes_state=True,
)
async def notes_restore(ctx: ToolContext, ids: str) -> ToolResult:
    notes = [n for n in (_own(ctx, i) for i in _ids(ids)) if n is not None and n.deleted_at]
    if not notes:
        return ToolResult(text="none of those is in your trash")
    for n in notes:
        store.restore(ctx.conn, n.id)
    return ToolResult(text="restored: " + ", ".join(f"#{n.id}" for n in notes),
                      receipt={"summary": "restored " + ",".join(str(n.id) for n in notes)})


@tool(description="Show what is in the asker's trash")
async def trash_list(ctx: ToolContext) -> ToolResult:
    rows = ctx.conn.execute(
        "SELECT id, text, deleted_at FROM notes WHERE owner_id = ? AND deleted_at IS NOT NULL"
        " ORDER BY deleted_at DESC LIMIT 30", (ctx.turn.member_id,)).fetchall()
    if not rows:
        return ToolResult(text="the trash is empty")
    return ToolResult(text="\n".join(f"#{r['id']} (deleted {r['deleted_at'][:10]}): "
                                     f"{_quote(r['text'])}" for r in rows))


@tool(
    description="Create a personal List for the asker (household Lists are set up by the "
    "owner in the config; explain that with help if asked)",
    args={"name": "the List's name"},
    changes_state=True,
)
async def list_create(ctx: ToolContext, name: str) -> ToolResult:
    name = " ".join(name.split()).lower()
    if not name:
        return ToolResult(text="a List needs a name")
    if any(lv.name.lower() == name for lv in store.lists_for(ctx.conn, viewer=ctx.viewer)):
        return ToolResult(text=f"there is already a List named {name}")
    ctx.conn.execute("INSERT INTO lists (name, scope, owner_id, created_at)"
                     " VALUES (?, 'personal', ?, ?)",
                     (name, ctx.turn.member_id, datetime.now().isoformat(timespec="seconds")))
    return ToolResult(text=f"created the personal List {name}",
                      receipt={"summary": f"created List {name}"})


# ── Calendar ────────────────────────────────────────────────────────────────
def _calendar(ctx: ToolContext):
    build = ctx.services.get("calendar")
    cal = build(ctx.turn.member_id) if build else None
    return cal if cal is not None and cal.available else None


def _day(raw: str) -> date | None:
    text = raw.strip().lower()
    today = date.today()
    if text in ("", "today", "hoje"):
        return today
    if text in ("tomorrow", "amanhã", "amanha"):
        return today + timedelta(days=1)
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


@tool(
    slow=True,
    description="Read the asker's calendar for a day",
    args={"day": "today, tomorrow, or YYYY-MM-DD"},
)
async def calendar_day(ctx: ToolContext, day: str = "today") -> ToolResult:
    if (why := _private(ctx, "the calendar")) is not None:
        return ToolResult(text=why)
    cal = _calendar(ctx)
    if cal is None:
        return ToolResult(text="no calendar is connected; the member can send /conectar_agenda "
                               "or ask you to connect it")
    wanted = _day(day)
    if wanted is None:
        return ToolResult(text=f"not a day: {day!r}")
    events = await asyncio.to_thread(cal.today, wanted)
    if not events:
        return ToolResult(text=f"nothing on {wanted.isoformat()}")
    return ToolResult(text="\n".join(
        (f"{e.start:%H:%M}–{e.end:%H:%M}" if not e.all_day else "all day")
        + f" {_quote(e.summary)} ({e.calendar})" for e in events))


@tool(
    slow=True,
    description="Create an event in the asker's calendar (in its 'Terminal Assistant' "
    "calendar, never with guests)",
    args={"title": "the event's title", "start": "YYYY-MM-DD HH:MM",
          "end": "YYYY-MM-DD HH:MM, or empty for one hour"},
    changes_state=True,
)
async def calendar_create(ctx: ToolContext, title: str, start: str, end: str = "") -> ToolResult:
    if (why := _private(ctx, "the calendar")) is not None:
        return ToolResult(text=why)
    cal = _calendar(ctx)
    if cal is None:
        return ToolResult(text="no calendar is connected; the member can send /conectar_agenda")
    try:
        start_at = datetime.fromisoformat(start.strip().replace(" ", "T"))
        end_at = (datetime.fromisoformat(end.strip().replace(" ", "T")) if end.strip()
                  else start_at + timedelta(hours=1))
    except ValueError:
        return ToolResult(text="start and end are YYYY-MM-DD HH:MM")
    targets = await asyncio.to_thread(cal.write_targets)
    if not targets:
        return ToolResult(text="the account has no calendar named 'Terminal Assistant'; the "
                               "member creates it once in Google Calendar (see help)")
    uid = await asyncio.to_thread(cal.create_event, targets[0].uid, title.strip(), start_at, end_at)
    return ToolResult(
        text=f"created {title!r} on {start_at:%Y-%m-%d %H:%M}",
        receipt={"summary": f"created event {title!r}",
                 "undo": {"kind": "delete_calendar_event", "source_uid": targets[0].uid,
                          "uid": uid}} if uid else None,
    )


# ── Things the bot hands out (sent by the bot itself) ───────────────────────
async def _send(ctx: ToolContext, key: str, text: str) -> ToolResult:
    say = ctx.services.get("say")
    if say is None:
        return ToolResult(text="this chat cannot receive it")
    await say(text)
    return ToolResult(text=f"sent the {key} to the member in this chat; do not repeat it, "
                           "just say it is above")


@tool(description="Send the asker the link to their own board (their notes on a page)")
async def board_link(ctx: ToolContext) -> ToolResult:
    from .i18n import t

    if (why := _private(ctx, "a board link")) is not None:
        return ToolResult(text=why)
    make = ctx.services.get("board_link")
    url = make(ctx.turn.member_id) if make else None
    if not url:
        return ToolResult(text="the board cannot be reached from outside the server "
                               "(TA_PUBLIC_URL is not set; the owner sets it, see help)")
    return await _send(ctx, "board link", t("bot.board_link", url=url))


@tool(description="Give the asker a one-time code to pair one of their computers as a "
      "Satellite (ta satellite login <code>)")
async def satellite_code(ctx: ToolContext) -> ToolResult:
    from .i18n import t

    if (why := _private(ctx, "a Satellite code")) is not None:
        return ToolResult(text=why)
    make = ctx.services.get("satellite_code")
    code = make(ctx.turn.member_id) if make else None
    if not code:
        return ToolResult(text="Satellites cannot pair with this server (it is not exposed "
                               "with a token; see help)")
    return await _send(ctx, "Satellite code", t("bot.satellite_code", code=code))


async def _connect(ctx: ToolContext, link_key: str, text_key: str, what: str) -> ToolResult:
    from .i18n import t

    if (why := _private(ctx, f"connecting {what}")) is not None:
        return ToolResult(text=why)
    link = ctx.services.get(link_key)
    if link is None or not link.configured:
        return ToolResult(text=f"{what} is not set up on the server yet (the owner needs a "
                               "Google OAuth client; see help)")
    return await _send(ctx, "consent link", t(text_key, url=link.consent(ctx.turn.member_id)))


@tool(description="Start connecting the asker's Google Calendar: sends them the consent link")
async def connect_calendar(ctx: ToolContext) -> ToolResult:
    return await _connect(ctx, "calendar_link", "bot.calendar_link", "the calendar")


@tool(description="Start connecting the asker's Gmail (read and drafts): sends the consent link")
async def connect_email(ctx: ToolContext) -> ToolResult:
    return await _connect(ctx, "mail_link", "bot.mail_link", "email")


# ── Reading the household's setup ───────────────────────────────────────────
@tool(
    description="List the automations: the house's file Rules (for admins), and the "
    "asker's scheduled actions and timers",
)
async def rules_list(ctx: ToolContext) -> ToolResult:
    lines = []
    if ctx.turn.permissions.is_admin:
        rules = ctx.services.get("file_rules")
        for r in (rules() if rules else []):
            lines.append(f"file Rule {r.name}: on {', '.join(str(t) for t in r.on)}")
    from .builtin_tools import schedule_list

    scheduled = await schedule_list(ctx)
    if scheduled.text != "nothing is scheduled":
        lines.append(scheduled.text)
    return ToolResult(text="\n".join(lines) or "no automation")


@tool(description="Show the asker's Priorities (what matters to them, used to order notes)")
async def priorities_show(ctx: ToolContext) -> ToolResult:
    from . import priorities

    content = priorities.current(ctx.conn, ctx.turn.member_id)
    return ToolResult(text=content or "no Priorities yet; they are set with `ta init` or on "
                                      "the board")


# ── Media and the ring light (admins, as on the board) ──────────────────────
@tool(
    description="Control music on the house speakers (Echo): play, pause, stop, next, previous",
    args={"action": "play, pause, stop, next or previous",
          "target": "a media_player entity or alias, or empty for the default speaker"},
    grant="admin",
    changes_state=True,
)
async def media_control(ctx: ToolContext, action: str, target: str = "") -> ToolResult:
    from .builtin_tools import _home
    from .config import resolve_entity

    app = ctx.services.get("app")
    echoes = app.state.config.echo_entities if app else ()
    entity = resolve_entity(target) if target.strip() else next(iter(echoes), "")
    if not entity.startswith("media_player."):
        return ToolResult(text="no speaker is configured (TA_ECHO_ENTITIES); see help")
    await _home(ctx).media(entity, action.strip().lower())
    return ToolResult(text=f"{action} on {entity}", receipt={"summary": f"{action} {entity}"})


@tool(
    description="The ring light on the owner's desk: on, off, toggle, or a profile name",
    args={"action": "on, off, toggle, or the name of a profile"},
    grant="admin",
    changes_state=True,
)
async def ringlight(ctx: ToolContext, action: str) -> ToolResult:
    app = ctx.services.get("app")
    lighter = app.state.lighter if app else ctx.services.get("lighter")
    if lighter is None or not lighter.available:
        return ToolResult(text="the ring light is not reachable (the owner's computer is off?)")
    a = action.strip().lower()
    ok = (await lighter.enable(a == "on") if a in ("on", "off")
          else await lighter.toggle() if a == "toggle" else await lighter.apply_profile(action))
    return ToolResult(text=f"ring light: {action} ({'ok' if ok else 'not applied'})",
                      receipt={"summary": f"ring light {action}"} if ok else None)

