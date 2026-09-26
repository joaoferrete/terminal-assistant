"""Tools: the only things the agent can do (D16, ADR 0017, ADR 0019).

A Tool is a Python function with `@tool`. The built-in ones live in `builtin_tools.py`;
a household's own go in `~/.config/ta/tools/`, loaded one file at a time exactly
like the rules, so a broken file costs that file and nothing else.

    @tool(description="Tell how full the disk is", admin=True)
    async def disk(ctx):
        ...
        return ToolResult(text=...)

Every Tool declares what it needs and what it does, and the **code** enforces it,
never the model:

- `grant`: the permission it takes — `admin`, `home` (the Entities of its
  arguments), `lists`, or nothing. It runs with the asker's Grant (D12).
- `changes_state`: whether it writes anything. In a **tainted** turn (below) such a
  Tool does not run; it asks the asker to confirm first (D27, invariant 8).
- `destructive`: always asks, tainted or not, and is never reachable from the
  group's proactive capture (invariant 7).
- `third_party`: whether what it returns can hold text the asker did not write —
  a web page, another Member's Note, a group's history. Returning it **taints**
  the turn.

There is no shell Tool. The model chooses between functions somebody wrote.
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("ta.tools")


@dataclass
class Source:
    """Where a piece of an answer came from (D31). Built by the code, from what
    the Tool actually returned, so the model cannot invent one."""

    kind: str          # "note" | "web"
    ref: str           # the note id, or the URL
    title: str = ""


@dataclass
class ToolResult:
    text: str
    sources: list[Source] = field(default_factory=list)
    # Set by the Tool when THIS result holds third-party text, even if the Tool
    # is not always third-party: `notes_search` only taints when it returns
    # somebody else's Note.
    tainted: bool = False
    # For a Receipt: what the change was, and how to undo it (T4.4).
    receipt: dict | None = None


@dataclass
class Tool:
    name: str
    description: str
    fn: Callable[..., Awaitable[ToolResult]]
    args: dict[str, str]                     # argument name → what it is, for the model
    grant: str | None = None                 # "admin" | "home" | "lists" | None
    changes_state: bool = False
    destructive: bool = False
    third_party: bool = False

    def spec(self) -> str:
        args = ", ".join(f"{k}: {v}" for k, v in self.args.items()) or "no arguments"
        flags = " (changes things)" if self.changes_state else ""
        return f"- {self.name}({args}){flags}: {self.description}"


_REGISTRY: dict[str, Tool] = {}


def tool(
    *,
    description: str,
    args: dict[str, str] | None = None,
    name: str | None = None,
    grant: str | None = None,
    changes_state: bool = False,
    destructive: bool = False,
    third_party: bool = False,
    registry: dict[str, Tool] | None = None,
):
    """Declare a Tool. `destructive` implies `changes_state`: nothing that deletes
    is read-only, and forgetting the second flag must not skip a confirmation."""

    def deco(fn):
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"tool {fn.__name__} must be `async def`")
        t = Tool(
            name=name or fn.__name__,
            description=description,
            fn=fn,
            args=dict(args or {}),
            grant=grant,
            changes_state=changes_state or destructive,
            destructive=destructive,
            third_party=third_party,
        )
        (registry if registry is not None else _REGISTRY)[t.name] = t
        return fn

    return deco


def registered() -> dict[str, Tool]:
    return dict(_REGISTRY)


@dataclass
class LoadReport:
    tools: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


def load_tools(directory: Path, registry: dict[str, Tool] | None = None) -> LoadReport:
    """Load `*.py` from `directory`, each in isolation (the same shape as rules).

    A file that fails to import is reported and skipped; the others still load.
    A household Tool cannot replace a built-in one: the built-ins carry the
    guardrails, and a same-named file silently shadowing `home_off` would be a way
    around them.
    """
    target = registry if registry is not None else _REGISTRY
    report = LoadReport()
    if not directory.is_dir():
        return report
    builtin = set(target)
    for path in sorted(directory.glob("*.py")):
        local: dict[str, Tool] = {}
        try:
            spec = importlib.util.spec_from_file_location(f"ta_user_tool_{path.stem}", path)
            module = importlib.util.module_from_spec(spec)
            module.tool = functools.partial(tool, registry=local)
            module.ToolResult = ToolResult
            module.Source = Source
            spec.loader.exec_module(module)
        except Exception as e:  # a broken file must not take the others down
            report.errors.append((path.name, f"{type(e).__name__}: {e}"))
            log.error("tool file %s did not load: %s", path.name, e)
            continue
        for name, t in local.items():
            if name in builtin:
                report.errors.append((path.name, f"{name} would replace a built-in Tool"))
                continue
            target[name] = t
            report.tools.append(name)
    return report


# ── The turn and its taint (D27) ────────────────────────────────────────────
@dataclass
class Turn:
    """One conversation turn: who asked, where, and whether it is tainted yet.

    The taint is set here, by the code, when a Tool returns third-party content.
    Nothing the model writes can clear it.
    """

    member_id: int
    conversation_id: str
    in_group: bool
    permissions: Any                     # grants.Permissions
    tainted: bool = False
    sources: list[Source] = field(default_factory=list)
    receipts: list[dict] = field(default_factory=list)
    proactive: bool = False              # the group's classifier, not a request


class NeedsConfirmation(Exception):
    """The Tool may run only after the asker presses a button (D27)."""

    def __init__(self, tool: Tool, args: dict) -> None:
        super().__init__(tool.name)
        # Not `self.args`: that is Exception's own attribute, and assigning a
        # dict to it silently turns it into a tuple of the keys.
        self.tool, self.tool_args = tool, args


class NotAllowed(Exception):
    """The asker's Grant does not cover this. Said to the model as a result."""


def allowed(t: Tool, turn: Turn) -> bool:
    p = turn.permissions
    if t.grant == "admin":
        return p.is_admin
    if t.grant == "tools":
        return p.tool(t.name)
    # "home" and "lists" are checked per argument by the Tool itself, because
    # only it knows which Entity or List an argument names.
    return True


async def run(t: Tool, turn: Turn, ctx, args: dict, *, confirmed: bool = False) -> ToolResult:
    """Run a Tool under every rule it declared. The one gate all calls pass."""
    if turn.proactive and t.destructive:
        raise NotAllowed("destructive tools never run from proactive capture")
    if not allowed(t, turn):
        raise NotAllowed(f"{t.name} is not in your Grant")
    if t.changes_state and not confirmed and (t.destructive or turn.tainted):
        raise NeedsConfirmation(t, args)
    result = await t.fn(ctx, **args)
    if t.third_party or result.tainted:
        turn.tainted = True
    turn.sources += result.sources
    if result.receipt is not None:
        turn.receipts.append({"tool": t.name, **result.receipt})
    return result
