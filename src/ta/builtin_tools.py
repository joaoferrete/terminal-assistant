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
