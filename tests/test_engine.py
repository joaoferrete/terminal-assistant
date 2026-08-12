from datetime import datetime

import pytest

from ta import engine
from ta.engine import Context, Trigger


@pytest.fixture(autouse=True)
def clean_registry():
    engine._REGISTRY.clear()
    yield
    engine._REGISTRY.clear()


def ctx(kind, value=None, hour="10:00"):
    h, m = (int(x) for x in hour.split(":"))
    return Context(trigger=Trigger(kind, value), now=datetime(2026, 8, 10, h, m))


# ── Isolated loading ────────────────────────────────────────────────────────
def write(dir, name, content):
    (dir / name).write_text(content)


def test_a_rule_with_a_syntax_error_does_not_take_the_others_down(tmp_path):
    """The mitigation promised in ADR 0002."""
    write(tmp_path, "good.py", """
from ta.engine import rule, mic_active
@rule(on=mic_active())
def good(ctx): pass
""")
    write(tmp_path, "broken.py", "this is not valid python (((")

    rep = engine.load_rules(tmp_path)
    assert [r.name for r in rep.rules] == ["good"]
    assert len(rep.errors) == 1
    assert rep.errors[0][0] == "broken.py"
    assert not rep.ok


def test_a_rule_that_calls_exit_does_not_kill_the_process(tmp_path):
    """This is why loading catches BaseException, not Exception."""
    write(tmp_path, "suicidal.py", "raise SystemExit(1)")
    write(tmp_path, "good.py", """
from ta.engine import rule, reminder_due
@rule(on=reminder_due())
def good(ctx): pass
""")
    rep = engine.load_rules(tmp_path)
    assert [r.name for r in rep.rules] == ["good"]
    assert len(rep.errors) == 1


def test_a_file_starting_with_an_underscore_is_ignored(tmp_path):
    write(tmp_path, "_util.py", "raise RuntimeError('should not have loaded')")
    rep = engine.load_rules(tmp_path)
    assert rep.ok and rep.rules == []


def test_a_missing_directory_is_not_an_error(tmp_path):
    rep = engine.load_rules(tmp_path / "does-not-exist")
    assert rep.ok and rep.rules == []


# ── Trigger matching ────────────────────────────────────────────────────────
def test_matching_by_kind_and_value():
    @engine.rule(on=engine.mic_active())
    def active_only(c): ...

    @engine.rule(on=engine.mic_inactive())
    def inactive_only(c): ...

    rules = list(engine._REGISTRY)
    assert [r.name for r in engine.matching(rules, Trigger("mic", True))] == ["active_only"]
    assert [r.name for r in engine.matching(rules, Trigger("mic", False))] == ["inactive_only"]


def test_value_none_casa_anything_valor():
    @engine.rule(on=engine.reminder_due())
    def anything(c): ...

    rules = list(engine._REGISTRY)
    assert len(engine.matching(rules, Trigger("reminder", 7))) == 1
    assert len(engine.matching(rules, Trigger("reminder"))) == 1


# ── Conditions ──────────────────────────────────────────────────────────────
def test_after_and_before():
    assert engine.after("16:00")(ctx("mic", True, hour="16:30"))
    assert not engine.after("16:00")(ctx("mic", True, hour="15:59"))
    assert engine.before("16:00")(ctx("mic", True, hour="15:59"))


def test_all_of_and_any_of():
    after16, before18 = engine.after("16:00"), engine.before("18:00")
    c = ctx("mic", True, hour="17:00")
    assert engine.all_of(after16, before18)(c)
    assert not engine.all_of(after16, engine.before("16:30"))(c)
    assert engine.any_of(engine.before("16:30"), after16)(c)


# ── Dispatch ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_false_when_does_not_run():
    ran = []

    @engine.rule(on=engine.mic_active(), when=engine.after("16:00"))
    def late(c):
        ran.append(True)

    rules = list(engine._REGISTRY)
    assert await engine.dispatch(rules, ctx("mic", True, hour="15:00")) == []
    assert ran == []
    assert await engine.dispatch(rules, ctx("mic", True, hour="16:01")) == ["late"]


@pytest.mark.asyncio
async def test_both_async_and_sync_rules_work():
    calls = []

    @engine.rule(on=engine.mic_active(), name="synchronous")
    def s(c):
        calls.append("s")

    @engine.rule(on=engine.mic_active(), name="assynchronous")
    async def a(c):
        calls.append("a")

    await engine.dispatch(list(engine._REGISTRY), ctx("mic", True))
    assert sorted(calls) == ["a", "s"]


@pytest.mark.asyncio
async def test_an_exception_in_one_rule_does_not_stop_the_next():
    ok = []

    @engine.rule(on=engine.mic_active(), name="explode")
    def explode(c):
        raise RuntimeError("boom")

    @engine.rule(on=engine.mic_active(), name="carries_on")
    def carries_on(c):
        ok.append(True)

    ran = await engine.dispatch(list(engine._REGISTRY), ctx("mic", True))
    assert ran == ["carries_on"]     # the one that blew up does not count as run
    assert ok == [True]              # but the next one did run
