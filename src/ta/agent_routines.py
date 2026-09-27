"""The Routine Tools (F10, D57, D58): create, run, list, delete.

A step is a Tool call the creator could make themselves: one that acts, is not
destructive (nobody may be there to press a confirmation when a Rule starts it),
and does not manage Routines or Members. Each step runs through `tools.run`, with
the Grant of whoever **starts** the Routine; a step outside it is skipped and said,
the others still run.
"""

from __future__ import annotations

import json
import logging

from . import routines
from .builtin_tools import ToolContext, _targets
from .i18n import t
from .tools import NotAllowed, ToolResult, allowed, registered, tool
from .tools import run as run_tool

log = logging.getLogger("ta.agent")

MAX_STEPS = 10
# Not inside a Routine: managing Routines, Members or schedules from a Routine
# would let one phrase rewrite the automations themselves.
_NOT_A_STEP = ("routine_", "member_", "schedule_")


def _step_tool(name: str):
    chosen = registered().get(name)
    if (chosen is None or not chosen.changes_state or chosen.destructive
            or chosen.name.startswith(_NOT_A_STEP)):
        return None
    return chosen


async def execute(ctx: ToolContext, routine: routines.Routine) -> ToolResult:
    """Run every step with the starter's Grant; collect what happened and the undos."""
    done, skipped, undos, lines = [], [], [], []
    for step in routine.steps:
        chosen = _step_tool(step.get("tool", ""))
        label = step.get("tool", "?")
        if chosen is None:
            skipped.append(label)
            continue
        try:
            # Already past the gate: the Routine itself was confirmed if the turn
            # needed it, and no step is destructive.
            result = await run_tool(chosen, ctx.turn, ctx, dict(step.get("args") or {}),
                                    confirmed=True)
        except NotAllowed:
            skipped.append(label)
            continue
        except Exception:
            log.exception("routine %s: step %s failed", routine.name, label)
            skipped.append(label)
            continue
        if result.receipt is None:          # "nothing you may switch matches"
            skipped.append(f"{label} ({result.text})")
            continue
        done.append(result.receipt.get("summary", label))
        lines.append(", ".join(result.receipt.get("entities") or []) or label)
        if result.receipt.get("undo"):
            undos.append(result.receipt["undo"])
    return ToolResult(
        text=f"routine {routine.name}: done: {'; '.join(done) or 'nothing'}"
             + (f"; skipped: {'; '.join(skipped)}" if skipped else ""),
        receipt={"summary": f"ran routine {routine.name}", "done": lines, "skipped": skipped,
                 "undo": {"kind": "undo_all", "undos": undos} if undos else None},
    )


def report(routine: routines.Routine, receipt: dict) -> str:
    """The one message a Routine answers with (D57)."""
    return "\n".join([t("bot.routine_ran", name=routine.name),
                      *(f"✓ {x}" for x in receipt.get("done", [])),
                      *(t("bot.routine_skipped", what=x) for x in receipt.get("skipped", []))])


@tool(
    description="Create a Routine: a named sequence of your acting tools, started by saying "
    "one of its phrases ('cheguei em casa') or by asking for it. Steps are a JSON list like "
    '[{"tool": "home_on", "args": {"target": "sala"}}, {"tool": "home_on", "args": '
    '{"target": "ventilador"}}]. household=yes shares it with the house.',
    args={"name": "a short name, e.g. chegada",
          "steps": "the steps, as a JSON list of {tool, args}",
          "phrases": "phrases that start it, separated by |, e.g. cheguei em casa|cheguei",
          "household": "yes to share it with the household, else empty"},
    changes_state=True,
)
async def routine_create(ctx: ToolContext, name: str, steps: object = "[]", phrases: str = "",
                         household: str = "") -> ToolResult:
    try:
        parsed = json.loads(steps) if isinstance(steps, str) else list(steps or [])
    except ValueError:
        return ToolResult(text="steps is not a JSON list")
    if not name.strip() or not parsed or len(parsed) > MAX_STEPS:
        return ToolResult(text=f"a Routine needs a name and 1 to {MAX_STEPS} steps")
    clean = []
    for step in parsed:
        chosen = _step_tool(str((step or {}).get("tool", ""))) if isinstance(step, dict) else None
        if chosen is None or not allowed(chosen, ctx.turn):
            return ToolResult(text=f"{step!r} cannot be a step: only your acting tools")
        args = step.get("args") or {}
        if not isinstance(args, dict) or set(args) - set(chosen.args):
            return ToolResult(text=f"{chosen.name} takes only: {', '.join(chosen.args)}")
        if chosen.grant == "home" and not await _targets(ctx, str(args.get("target", ""))):
            return ToolResult(text=f"nothing you may switch matches {args.get('target', '')!r}")
        clean.append({"tool": chosen.name, "args": {k: str(v) for k, v in args.items()}})
    said = [p.strip() for p in phrases.split("|") if routines.fold(p)]
    if routines.by_name(ctx.conn, ctx.turn.member_id, name) is not None:
        return ToolResult(text=f"there is already a Routine named {name}")
    shared = household.strip().lower() in ("yes", "sim", "true")
    routines.create(ctx.conn, owner_id=ctx.turn.member_id, name=name, steps=clean,
                    phrases=said, household=shared)
    return ToolResult(
        text=f"created Routine {name} ({len(clean)} steps"
             + (f"; started by: {', '.join(said)}" if said else "")
             + ("; shared with the household" if shared else "") + ")",
        receipt={"summary": f"created Routine {name}",
                 "undo": {"tool": "routine_delete", "args": {"name": name}}},
    )


@tool(
    description="Run a Routine by name (also when the member says it in other words: "
    "'tô chegando, prepara a casa' for the arrival Routine)",
    args={"name": "the Routine's name"},
    changes_state=True,
)
async def routine_run(ctx: ToolContext, name: str) -> ToolResult:
    routine = routines.by_name(ctx.conn, ctx.turn.member_id, name)
    if routine is None:
        return ToolResult(text=f"no Routine named {name!r}; see routine_list")
    return await execute(ctx, routine)


@tool(description="List the Routines the asker can run (their own and the household's)")
async def routine_list(ctx: ToolContext) -> ToolResult:
    found = routines.visible(ctx.conn, ctx.turn.member_id)
    if not found:
        return ToolResult(text="no Routine yet")
    def steps(r):
        return ", ".join(f"{s['tool']} {json.dumps(s['args'], ensure_ascii=False)}"
                         for s in r.steps)

    return ToolResult(text="\n".join(
        f"{r.name}" + (" (household)" if r.scope == "household" else "") + f": {steps(r)}"
        + (f"; phrases: {', '.join(r.phrases)}" if r.phrases else "") for r in found))


@tool(
    description="Delete a Routine (only its creator, or the owner)",
    args={"name": "the Routine's name"},
    changes_state=True,
)
async def routine_delete(ctx: ToolContext, name: str) -> ToolResult:
    from . import members as members_mod

    routine = routines.by_name(ctx.conn, ctx.turn.member_id, name)
    member = members_mod.get(ctx.conn, ctx.turn.member_id)
    if routine is None or not routines.delete(
            ctx.conn, routine.id, member_id=ctx.turn.member_id,
            is_owner=bool(member and member.is_owner)):
        return ToolResult(text=f"no Routine {name!r} you may delete")
    return ToolResult(text=f"deleted Routine {routine.name}",
                      receipt={"summary": f"deleted Routine {routine.name}"})
