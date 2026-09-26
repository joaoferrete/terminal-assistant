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
    description="Turn on lights or plugs: a room, a name, a group like 'luz', or an entity_id",
    args={"target": "what to turn on", "brightness": "0-100 for lights, or empty for full"},
    grant="home",
    changes_state=True,
)
async def home_on(ctx: ToolContext, target: str, brightness: str = "") -> ToolResult:
    entities = await _targets(ctx, target)
    if not entities:
        return ToolResult(text=f"nothing you may switch matches {target!r}")
    level = int(brightness) if str(brightness).strip().isdigit() else 100
    for e in entities:
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
