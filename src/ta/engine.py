"""The rule engine.

The app is the brain of the automations (ADR 0002): it evaluates the triggers and
treats Home Assistant as a dumb actuator. Rules are Python, kept under version
control, and this module is the dispatcher — not an interpreter.

Each Rule is loaded in isolation inside a try/except. A rule with a syntax error
takes **itself** off the air, never the daemon: that is the mitigation promised in
ADR 0002.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ta.engine")

# A Rule's signature: it takes the context and does what it has to do.
RuleFn = Callable[["Context"], Awaitable[None] | None]


@dataclass
class Trigger:
    """The condition that starts a Rule.

    `kind` identifies the source of the signal (`mic`, `time`, `reminder`,
    `state`), and `value` qualifies it. Deliberately poor: a rich trigger would
    become the template language ADR 0002 refused to write.
    """

    kind: str
    value: Any = None

    def __str__(self) -> str:
        return f"{self.kind}={self.value}" if self.value is not None else self.kind


@dataclass
class Rule:
    name: str
    fn: RuleFn
    on: list[Trigger]
    when: Callable[[Context], bool] | None = None
    source: str = "?"


@dataclass
class Context:
    """What a Rule receives. Actuators and sensors are injected by the daemon."""

    trigger: Trigger
    now: Any = None
    home: Any = None
    lighter: Any = None
    notify: Any = None
    calendar: Any = None
    note: Any = None  # present when the trigger is a Reminder
    extra: dict[str, Any] = field(default_factory=dict)


# ── Registry ────────────────────────────────────────────────────────────────
# A module-level registry: the files in the rules directory call @rule at import
# time, and the loader collects whatever showed up. Simple, and enough for a
# single-machine app.
_REGISTRY: list[Rule] = []


def rule(
    *,
    on: Trigger | list[Trigger],
    when: Callable[[Context], bool] | None = None,
    name: str | None = None,
) -> Callable[[RuleFn], RuleFn]:
    """Register a Rule.

        @rule(on=mic_active(), when=after("16:00"))
        async def late_meeting(ctx): ...
    """
    triggers = on if isinstance(on, list) else [on]

    def deco(fn: RuleFn) -> RuleFn:
        _REGISTRY.append(
            Rule(
                name=name or fn.__name__,
                fn=fn,
                on=triggers,
                when=when,
                source=getattr(fn, "__module__", "?"),
            )
        )
        return fn

    return deco


# ── Ready-made triggers and conditions ──────────────────────────────────────
def mic_active() -> Trigger:
    """The microphone started being used — the "I joined a call" signal (ADR 0008)."""
    return Trigger("mic", True)


def mic_inactive() -> Trigger:
    return Trigger("mic", False)


def at_time(hhmm: str) -> Trigger:
    return Trigger("time", hhmm)


def reminder_due() -> Trigger:
    return Trigger("reminder")


def entity_state(entity_id: str) -> Trigger:
    return Trigger("state", entity_id)


def after(hhmm: str) -> Callable[[Context], bool]:
    """A time condition: "and it is past 16:00". It is what Lighter cannot express."""
    h, m = (int(x) for x in hhmm.split(":"))
    return lambda ctx: ctx.now.time() >= time(h, m)


def before(hhmm: str) -> Callable[[Context], bool]:
    h, m = (int(x) for x in hhmm.split(":"))
    return lambda ctx: ctx.now.time() < time(h, m)


def all_of(*conds: Callable[[Context], bool]) -> Callable[[Context], bool]:
    return lambda ctx: all(c(ctx) for c in conds)


def any_of(*conds: Callable[[Context], bool]) -> Callable[[Context], bool]:
    return lambda ctx: any(c(ctx) for c in conds)


# ── Loading ─────────────────────────────────────────────────────────────────
@dataclass
class LoadReport:
    rules: list[Rule]
    errors: list[tuple[str, str]]  # (filename, traceback)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_rules(directory: Path) -> LoadReport:
    """Load the Rule files, one at a time, in isolation.

    A file that blows up goes into `errors` and the others keep loading. It is the
    difference between "one broken rule" and "the daemon is down".
    """
    _REGISTRY.clear()
    errors: list[tuple[str, str]] = []

    if not directory.is_dir():
        log.warning("rules directory does not exist: %s", directory)
        return LoadReport([], [])

    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        before_count = len(_REGISTRY)
        try:
            spec = importlib.util.spec_from_file_location(f"ta_rules.{path.stem}", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"could not load {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except BaseException:
            # BaseException on purpose: a stray `exit()` in a rule file must not
            # take the daemon down with it.
            errors.append((path.name, traceback.format_exc()))
            log.error("rule %s failed to load; the others carry on", path.name)
            continue
        log.info("rule %s loaded (%d)", path.name, len(_REGISTRY) - before_count)

    return LoadReport(list(_REGISTRY), errors)


# ── Dispatch ────────────────────────────────────────────────────────────────
def matching(rules: list[Rule], trigger: Trigger) -> list[Rule]:
    return [
        r
        for r in rules
        # A None value on a registered trigger means "any value".
        for t in r.on
        if t.kind == trigger.kind and (t.value is None or t.value == trigger.value)
    ]


async def dispatch(rules: list[Rule], ctx: Context) -> list[str]:
    """Run the Rules matching the trigger. Returns the names of those that ran.

    An exception inside a Rule is logged and swallowed: the next rule runs anyway,
    and the daemon stays up.
    """
    ran: list[str] = []
    for r in matching(rules, ctx.trigger):
        try:
            if r.when is not None and not r.when(ctx):
                continue
            result = r.fn(ctx)
            if inspect.isawaitable(result):
                await result
            ran.append(r.name)
            log.info("rule %s ran, triggered by %s", r.name, ctx.trigger)
        except Exception:
            log.exception("rule %s failed while running", r.name)
    return ran
