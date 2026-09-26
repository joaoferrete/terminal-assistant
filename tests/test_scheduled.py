"""Scheduled actions (F9, D39): "liga a luz daqui 10 min", "todo dia às 7h".

What must hold: the time is computed by the code, a restart loses nothing, the
author's Grant is read again when it fires, a late run is skipped and said, and
only the author sees or cancels their own.
"""
import asyncio
import json
from datetime import datetime, timedelta

import pytest
from test_acting_tools import INVENTORY
from test_agent import ScriptedLLM, answer, call
from test_bot import FakeChannel, inbound
from test_daemon import StubHome

from ta import builtin_tools, db, scheduled, store
from ta.bot import AgentDeps, Bot
from ta.grants import OWNER_PERMISSIONS, Permissions
from ta.tools import NeedsConfirmation, Turn, registered
from ta.tools import run as run_tool

NOW = datetime(2026, 9, 25, 14, 0)          # a Friday


# ── The clock ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("kw", "first", "tod"), [
    ({"in_minutes": "10"}, datetime(2026, 9, 25, 14, 10), None),
    ({"at": "15:30"}, datetime(2026, 9, 25, 15, 30), None),
    ({"at": "7h"}, datetime(2026, 9, 26, 7, 0), None),                    # tomorrow
    ({"at": "2026-10-01 08:00"}, datetime(2026, 10, 1, 8, 0), None),
    ({"at": "07:00", "repeat": "daily"}, datetime(2026, 9, 26, 7, 0), "07:00"),
    ({"at": "07:00", "repeat": "weekdays"}, datetime(2026, 9, 28, 7, 0), "07:00"),  # Monday
    ({"at": "22:30", "repeat": "weekdays"}, datetime(2026, 9, 25, 22, 30), "22:30"),
    ({"at": "09:00", "repeat": "weekends"}, datetime(2026, 9, 26, 9, 0), "09:00"),
])
def test_when_is_computed_by_the_code(kw, first, tod):
    assert scheduled.when(now=NOW, **kw) == (first, tod)


@pytest.mark.parametrize("kw", [
    {}, {"in_minutes": "dez"}, {"in_minutes": "-5"}, {"at": "25:00"},
    {"at": "2026-09-01 08:00"},                   # the past
    {"in_minutes": "10", "repeat": "daily"},      # "every day in 10 minutes" means nothing
    {"at": "07:00", "repeat": "hourly"},
])
def test_what_cannot_be_understood_is_refused(kw):
    with pytest.raises(scheduled.BadSchedule):
        scheduled.when(now=NOW, **kw)


def test_a_recurring_row_does_not_drift_after_a_late_run():
    """Computed from the time of day, not by adding 24 h to when it ran."""
    assert scheduled.next_occurrence("daily", "07:00", datetime(2026, 9, 26, 7, 9)) == \
        datetime(2026, 9, 27, 7, 0)


@pytest.mark.parametrize(("delta", "text"), [
    (timedelta(minutes=10), "10 min"), (timedelta(seconds=30), "1 min"),
    (timedelta(hours=2, minutes=5), "2h05"), (timedelta(days=3, hours=4), "3d 4h"),
    (timedelta(minutes=59, seconds=30), "1h00"),
])
def test_remaining_is_readable(delta, text):
    assert scheduled.remaining(NOW + delta, NOW) == text


# ── The Tools ───────────────────────────────────────────────────────────────
@pytest.fixture
def ctx(tmp_path):
    def build(perms=OWNER_PERMISSIONS, member_id=1):
        conn = db.connect(tmp_path / "t.db")
        conn.execute("INSERT OR IGNORE INTO members (id, handle, is_owner, created_at)"
                     " VALUES (2, 'ana', 0, 'x')")
        turn = Turn(member_id=member_id, conversation_id="c1", in_group=False,
                    permissions=perms)
        return builtin_tools.ToolContext(
            conn=conn, turn=turn, channel="telegram",
            services={"home": StubHome(INVENTORY), "now": lambda: NOW})
    return build


def run(c, name, **args):
    return asyncio.run(run_tool(registered()[name], c.turn, c, args))


def test_scheduling_stores_the_action_and_offers_to_cancel(ctx):
    c = ctx()
    out = run(c, "schedule_action", tool="home_on", tool_args='{"target": "sala"}',
              in_minutes="10")
    assert "14:10" in out.text and "10 min" in out.text
    [s] = scheduled.active(c.conn, 1)
    assert (s.tool, s.args) == ("home_on", {"target": "sala"})
    assert s.next_at == NOW + timedelta(minutes=10)
    assert c.turn.receipts[-1]["undo"] == {"kind": "cancel_scheduled", "id": s.id}


def test_the_arguments_may_come_as_an_object_too(ctx):
    c = ctx()
    run(c, "schedule_action", tool="home_off", tool_args={"target": "luz"}, at="23:00")
    assert scheduled.active(c.conn, 1)[0].args == {"target": "luz"}


@pytest.mark.parametrize(("tool", "args"), [
    ("web_search", '{"question": "x"}'),        # does not act
    ("schedule_cancel", '{"id": "1"}'),         # scheduling the scheduler
    ("home_on", '{"target": "sala", "evil": 1}'),
    ("nope", "{}"),
])
def test_only_acting_tools_with_their_own_arguments_can_be_scheduled(ctx, tool, args):
    c = ctx()
    run(c, "schedule_action", tool=tool, tool_args=args, in_minutes="5")
    assert scheduled.active(c.conn, 1) == []


def test_a_target_out_of_the_grant_is_refused_now_not_at_seven(ctx):
    c = ctx(Permissions(entity_patterns=("light.quarto",)), member_id=2)
    out = run(c, "schedule_action", tool="home_on", tool_args='{"target": "sala"}',
              at="07:00", repeat="daily")
    assert "nothing you may switch" in out.text
    assert scheduled.active(c.conn, 2) == []


def test_in_a_tainted_turn_scheduling_asks_first(ctx):
    """A web page must not be able to plant a 3 a.m. action (D27)."""
    c = ctx()
    c.turn.tainted = True
    with pytest.raises(NeedsConfirmation):
        run(c, "schedule_action", tool="home_off", tool_args='{"target": "tudo"}',
            in_minutes="1")


def test_list_says_how_long_is_left_and_shows_only_ones_own(ctx):
    c = ctx()
    run(c, "schedule_action", tool="home_on", tool_args='{"target": "sala"}',
        at="07:00", repeat="daily")
    other = ctx(member_id=2)
    assert run(other, "schedule_list").text == "nothing is scheduled"
    out = run(c, "schedule_list").text
    assert "17h00" in out and "daily at 07:00" in out


def test_cancel_works_only_for_the_author(ctx):
    c = ctx()
    run(c, "schedule_action", tool="home_on", tool_args='{"target": "sala"}', in_minutes="5")
    sid = scheduled.active(c.conn, 1)[0].id
    assert "no active" in run(ctx(member_id=2), "schedule_cancel", id=str(sid)).text
    assert scheduled.active(c.conn, 1)
    assert "cancelled" in run(c, "schedule_cancel", id=f"#{sid}").text
    assert scheduled.active(c.conn, 1) == []


# ── Firing ──────────────────────────────────────────────────────────────────
class SendingChannel(FakeChannel):
    async def send(self, conversation_id, text, buttons=None, **kw):
        self.sent.append(text)
        self.buttons.append(buttons or [])
        return f"bot{len(self.sent)}"


@pytest.fixture
def bot(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    home = StubHome(INVENTORY)
    perms = {"now": OWNER_PERMISSIONS}

    def build(*steps):
        b = Bot(conn, SendingChannel(), owner_username="dono",
                capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id),
                agent=AgentDeps(llm=ScriptedLLM(*steps), registry=registered,
                                permissions=lambda m: perms["now"],
                                services={"home": home}))
        asyncio.run(b.handle(inbound("/start")))
        b.home, b.perms = home, perms
        return b
    return build


def plant(conn, *, tool="home_on", args=None, at=NOW, repeat="once", tod=None):
    return scheduled.add(conn, member_id=1, channel="telegram", conversation_id="c1",
                         in_group=False, tool=tool, args=args or {"target": "sala"},
                         summary="home_on target=sala", next_at=at, repeat=repeat,
                         time_of_day=tod)


def test_a_due_action_runs_and_says_so_with_an_undo(bot):
    b = bot()
    plant(b.conn)
    assert asyncio.run(b.run_scheduled(NOW + timedelta(seconds=20))) == 1
    assert b.home.calls == [("light.sala", 100)]
    assert "⏰" in b.channel.sent[-1] and "light.sala" in b.channel.sent[-1]
    assert b.channel.buttons[-1][0].data.startswith("undo:")
    assert scheduled.active(b.conn, 1) == [], "a one-off is finished"
    assert asyncio.run(b.run_scheduled(NOW + timedelta(minutes=1))) == 0, "never twice"


def test_a_recurring_action_moves_to_its_next_day(bot):
    b = bot()
    plant(b.conn, at=datetime(2026, 9, 26, 7, 0), repeat="daily", tod="07:00")
    asyncio.run(b.run_scheduled(datetime(2026, 9, 26, 7, 0, 10)))
    [s] = scheduled.active(b.conn, 1)
    assert s.next_at == datetime(2026, 9, 27, 7, 0)


def test_a_late_action_is_skipped_and_told_not_run(bot):
    b = bot()
    plant(b.conn, at=datetime(2026, 9, 26, 7, 0), repeat="daily", tod="07:00")
    asyncio.run(b.run_scheduled(datetime(2026, 9, 26, 7, 40)))
    assert b.home.calls == []
    assert "07:00" in b.channel.sent[-1]
    assert scheduled.active(b.conn, 1)[0].next_at == datetime(2026, 9, 27, 7, 0)


def test_a_grant_revoked_after_scheduling_wins(bot):
    b = bot()
    plant(b.conn)
    b.perms["now"] = Permissions()          # the Owner removed the Grant meanwhile
    asyncio.run(b.run_scheduled(NOW))
    assert b.home.calls == []
    assert "Feito" not in b.channel.sent[-1] and "Done" not in b.channel.sent[-1]


def test_the_cancel_button_cancels_and_says_so(bot):
    b = bot(answer("Agendado para 14:10."))
    b.agent.services["now"] = lambda: NOW
    b.agent.llm.steps.insert(0, call("schedule_action", json.dumps(
        {"tool": "home_on", "tool_args": '{"target": "sala"}', "in_minutes": "10"})))
    asyncio.run(b.handle(inbound("liga a luz do quarto daqui 10 min")))
    [button] = b.channel.buttons[-1]
    assert button.label in ("Cancelar agendamento", "Cancel schedule")
    asyncio.run(b.handle(_press(button.data)))
    assert scheduled.active(b.conn, 1) == []
    assert b.channel.acks[-1] in ("Agendamento cancelado.", "Schedule cancelled.")


def test_a_restart_loses_nothing(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    plant(conn)
    conn.close()
    assert len(scheduled.active(db.connect(tmp_path / "t.db"), 1)) == 1


def _press(data):
    from ta.channel import Inbound

    return Inbound(channel="telegram", conversation_id="c1", message_id="8",
                   sender_id="1001", sender_username="dono", private=True, text="",
                   callback=data)


def test_the_agent_is_told_what_time_it_is(bot):
    b = bot(answer("ok"))
    asyncio.run(b.handle(inbound("que horas são?")))
    assert datetime.now().strftime("%Y-%m-%d") in b.agent.llm.prompts[-1][0]
