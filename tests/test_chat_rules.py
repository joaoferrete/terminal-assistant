"""Chat Rules (F10, D45, D46, D54) and the house's state (D52c)."""
import asyncio
import json
from datetime import datetime, timedelta

import pytest
from test_bot import inbound
from test_daemon import StubHome
from test_reminders_chat import SendingChannel

from ta import builtin_tools, chat_rules, db, store
from ta.bot import AgentDeps, Bot
from ta.channel import Inbound
from ta.grants import OWNER_PERMISSIONS, Permissions
from ta.tools import NeedsConfirmation, Turn, registered
from ta.tools import run as run_tool

HOUSE = [
    {"entity_id": "light.corredor", "state": "off", "attributes": {"friendly_name": "Corredor"}},
    {"entity_id": "light.sala", "state": "on", "attributes": {"friendly_name": "Sala"}},
    {"entity_id": "binary_sensor.porta", "state": "off",
     "attributes": {"friendly_name": "Porta da frente"}},
]
NIGHT = datetime(2026, 9, 26, 23, 0)


class House(StubHome):
    async def states(self):
        return self.inventory

    async def state(self, entity_id):
        return next(e for e in self.inventory if e["entity_id"] == entity_id)


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")

    def build(member_id=1, perms=OWNER_PERMISSIONS, tainted=False):
        turn = Turn(member_id=member_id, conversation_id="c", in_group=False, permissions=perms,
                    tainted=tainted)
        return builtin_tools.ToolContext(conn=conn, turn=turn, channel="telegram",
                                         services={"home": House(HOUSE)})
    return build


def run(c, tool_name, **args):
    return asyncio.run(run_tool(registered()[tool_name], c.turn, c, args))


def door_rule(**extra):
    return {"name": "corredor", "when_entity": "binary_sensor.porta", "when_state": "on",
            "after": "22:00", "tool": "home_on", "tool_args": '{"target": "corredor"}', **extra}


# ── The house's state ───────────────────────────────────────────────────────
def test_what_is_on_and_a_device_by_name(ctx):
    c = ctx()
    assert run(c, "home_status").text == "Sala (light.sala): on"
    assert "binary_sensor.porta" in run(c, "home_status", target="porta").text


def test_a_member_reads_only_what_their_grant_covers(ctx):
    c = ctx(member_id=2, perms=Permissions(entity_patterns=("light.sala",)))
    assert "nothing you may see" in run(c, "home_status", target="porta").text


# ── Creating ────────────────────────────────────────────────────────────────
def test_a_rule_is_stored_as_data_and_described(ctx):
    c = ctx()
    out = run(c, "rule_create", **door_rule())
    assert "quando binary_sensor.porta ficar on entre 22:00" in out.text
    [rule] = chat_rules.visible(c.conn, 1)
    assert (rule.action_tool, rule.action_args) == ("home_on", {"target": "corredor"})


@pytest.mark.parametrize(("change", "why"), [
    ({"when_entity": ""}, "exactly one trigger"),
    ({"when_entity": "porta"}, "not an entity_id"),
    ({"tool": "rule_delete", "tool_args": '{"name": "x"}'}, "cannot be a Rule's action"),
    ({"tool": "member_add", "tool_args": "{}"}, "cannot be a Rule's action"),
    ({"after": "25:00"}, "not a time"),
])
def test_what_a_rule_cannot_be(ctx, change, why):
    assert why in run(ctx(), "rule_create", **door_rule(**change)).text


def test_only_the_owner_makes_rules_on_the_microphone(ctx):
    c = ctx(member_id=2, perms=Permissions(entity_patterns=("light.",)))
    out = run(c, "rule_create", **door_rule(when_entity="", when_mic="on"))
    assert "only the owner" in out.text


def test_a_rule_asked_for_in_a_tainted_turn_waits_for_the_button(ctx):
    with pytest.raises(NeedsConfirmation):
        run(ctx(tainted=True), "rule_create", **door_rule())


# ── Matching ────────────────────────────────────────────────────────────────
def test_a_window_may_cross_midnight():
    r = chat_rules.ChatRule(1, 1, "x", "personal", "state", "e", None, "22:00", "06:00", None,
                            None, "home_on", {}, True, None)
    assert chat_rules.in_window(r, NIGHT)
    assert chat_rules.in_window(r, NIGHT.replace(hour=3))
    assert not chat_rules.in_window(r, NIGHT.replace(hour=12))


def test_it_fires_once_then_waits_out_the_debounce(ctx):
    c = ctx()
    run(c, "rule_create", **door_rule())
    assert chat_rules.triggered(c.conn, "state", "binary_sensor.porta", "on", NIGHT)
    assert not chat_rules.triggered(c.conn, "state", "binary_sensor.porta", "off", NIGHT)
    chat_rules.mark_fired(c.conn, 1, NIGHT)
    assert not chat_rules.triggered(c.conn, "state", "binary_sensor.porta", "on",
                                    NIGHT + timedelta(seconds=30))


# ── Firing ──────────────────────────────────────────────────────────────────
@pytest.fixture
def bot(tmp_path):
    conn = db.connect(tmp_path / "b.db")
    home = House(HOUSE)
    perms = {"now": OWNER_PERMISSIONS}
    b = Bot(conn, SendingChannel(), owner_username="dono",
            capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id),
            agent=AgentDeps(llm=None, registry=registered, permissions=lambda m: perms["now"],
                            services={"home": home}))
    asyncio.run(b.handle(inbound("/start")))
    b.home, b.perms = home, perms
    return b


def plant(conn, **kw):
    return chat_rules.create(conn, owner_id=1, name="corredor", trigger_kind="state",
                             trigger_entity="binary_sensor.porta", trigger_to="on",
                             action_tool="home_on", action_args={"target": "corredor"}, **kw)


def press(data):
    return Inbound(channel="telegram", conversation_id="1001", message_id="9", sender_id="1001",
                   sender_username="dono", private=True, text="", callback=data)


def test_the_door_turns_the_light_on_and_the_creator_is_told(bot):
    plant(bot.conn, cond_after="22:00")
    assert asyncio.run(bot.fire_chat_rules("state", "binary_sensor.porta", "on", NIGHT)) == 1
    assert bot.home.calls == [("light.corredor", 100)]
    assert "corredor" in bot.channel.sent[-1] and "Corredor" in bot.channel.sent[-1]
    labels = [b.label for b in bot.channel.buttons[-1]]
    assert labels == ["Desfazer", "Desligar esta regra"]
    asyncio.run(bot.handle(press(bot.channel.buttons[-1][1].data)))
    assert not chat_rules.visible(bot.conn, 1)[0].enabled


def test_outside_the_window_or_the_condition_nothing_happens(bot):
    plant(bot.conn, cond_after="22:00", cond_entity="light.sala", cond_state="off")
    asyncio.run(bot.fire_chat_rules("state", "binary_sensor.porta", "on", NIGHT))
    assert bot.home.calls == [], "the living room light is on: the condition fails"
    asyncio.run(bot.fire_chat_rules("state", "binary_sensor.porta", "on",
                                    NIGHT.replace(hour=12)))
    assert bot.home.calls == []


def test_a_grant_narrowed_since_creation_wins(bot):
    plant(bot.conn)
    bot.perms["now"] = Permissions()
    asyncio.run(bot.fire_chat_rules("state", "binary_sensor.porta", "on", NIGHT))
    assert bot.home.calls == [] and "não consegui agir" in bot.channel.sent[-1]


def test_agendado_lists_rules_and_routines(bot):
    from ta import routines

    plant(bot.conn)
    routines.create(bot.conn, owner_id=1, name="chegada",
                    steps=[{"tool": "home_on", "args": {"target": "sala"}}],
                    phrases=["cheguei"])
    out = bot._scheduled_text(1)
    assert "corredor" in out and "chegada" in out and json.dumps("cheguei")[1:-1] in out
