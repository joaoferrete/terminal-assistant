"""Routines (F10, D57, D58): "cheguei em casa" runs a named sequence, with no
model, with the starter's Grant, and [Undo all] puts the house back."""
import asyncio
import json

import pytest
from test_acting_tools import INVENTORY
from test_agent import ScriptedLLM, answer, call
from test_bot import inbound
from test_daemon import StubHome
from test_reminders_chat import SendingChannel

from ta import builtin_tools, db, routines, store
from ta.bot import AgentDeps, Bot
from ta.channel import Inbound
from ta.grants import OWNER_PERMISSIONS, Permissions
from ta.tools import NeedsConfirmation, Turn, registered
from ta.tools import run as run_tool

INVENTORY2 = [*INVENTORY, {"entity_id": "switch.ventilador", "state": "off",
                           "attributes": {"friendly_name": "Ventilador",
                                                          "device_class": "outlet"}}]
ARRIVAL = [{"tool": "home_on", "args": {"target": "sala"}},
           {"tool": "home_on", "args": {"target": "ventilador"}}]


def test_a_phrase_matches_whatever_the_case_and_punctuation(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    routines.create(conn, owner_id=1, name="chegada", steps=ARRIVAL,
                    phrases=["Cheguei em casa", "cheguei"])
    assert routines.by_phrase(conn, 1, "cheguei em casa!!").name == "chegada"
    assert routines.by_phrase(conn, 1, "CHEGUEI") is not None
    assert routines.by_phrase(conn, 1, "cheguei atrasado") is None, "a phrase, not a prefix"


def test_a_private_routine_is_nobody_elses(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    routines.create(conn, owner_id=2, name="cinema", steps=ARRIVAL, phrases=["modo cinema"])
    assert routines.by_phrase(conn, 1, "modo cinema") is None
    routines.create(conn, owner_id=2, name="casa", steps=ARRIVAL, phrases=["boa noite"],
                    household=True)
    assert routines.by_phrase(conn, 1, "boa noite").name == "casa"


@pytest.fixture
def bot(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    home = StubHome(INVENTORY2)
    perms = {"now": OWNER_PERMISSIONS}

    def build(*steps):
        b = Bot(conn, SendingChannel(), owner_username="dono",
                capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id),
                agent=AgentDeps(llm=ScriptedLLM(*steps), registry=registered,
                                permissions=lambda m: perms["now"], services={"home": home}))
        asyncio.run(b.handle(inbound("/start")))
        b.home, b.perms = home, perms
        return b
    return build


def press(data):
    return Inbound(channel="telegram", conversation_id="c1", message_id="9", sender_id="1001",
                   sender_username="dono", private=True, text="", callback=data)


def test_created_by_chat_then_the_phrase_runs_it_with_no_model(bot):
    b = bot(call("routine_create", json.dumps({
        "name": "chegada", "steps": json.dumps(ARRIVAL), "phrases": "cheguei em casa"})),
        answer("Criei a rotina chegada."))
    asyncio.run(b.handle(inbound("cria uma rotina chegada: luz da sala e ventilador")))
    prompts = len(b.agent.llm.prompts)
    asyncio.run(b.handle(inbound("Cheguei em casa")))
    assert len(b.agent.llm.prompts) == prompts, "the phrase needs no model"
    assert [c[0] for c in b.home.calls] == ["light.sala", "switch.ventilador"]
    assert "chegada" in b.channel.sent[-1]
    [undo] = b.channel.buttons[-1]
    assert undo.label == "Desfazer tudo"
    asyncio.run(b.handle(press(undo.data)))
    assert b.home.turned_off == ["switch.ventilador", "light.sala"], "last first"


def test_a_step_outside_the_starters_grant_is_skipped_and_said(bot):
    b = bot()
    routines.create(b.conn, owner_id=1, name="chegada", steps=ARRIVAL, phrases=["cheguei"])
    b.perms["now"] = Permissions(entity_patterns=("light.sala",))
    asyncio.run(b.handle(inbound("cheguei")))
    assert [c[0] for c in b.home.calls] == ["light.sala"]
    assert "pulei" in b.channel.sent[-1]


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    turn = Turn(member_id=1, conversation_id="c", in_group=False, permissions=OWNER_PERMISSIONS)
    return builtin_tools.ToolContext(conn=conn, turn=turn, channel="telegram",
                                     services={"home": StubHome(INVENTORY2)})


@pytest.mark.parametrize("step", [
    {"tool": "notes_delete", "args": {"ids": "1"}} and {"tool": "member_add", "args": {}},
    {"tool": "routine_delete", "args": {"name": "x"}},
    {"tool": "web_search", "args": {"question": "x"}},
    {"tool": "home_on", "args": {"target": "sala", "evil": "1"}},
])
def test_only_acting_tools_can_be_steps(ctx, step):
    out = asyncio.run(run_tool(registered()["routine_create"], ctx.turn, ctx,
                               {"name": "x", "steps": json.dumps([step])}))
    assert "cannot be a step" in out.text or "takes only" in out.text
    assert routines.visible(ctx.conn, 1) == []


def test_running_a_routine_in_a_tainted_turn_asks_first(ctx):
    routines.create(ctx.conn, owner_id=1, name="chegada", steps=ARRIVAL, phrases=[])
    ctx.turn.tainted = True
    with pytest.raises(NeedsConfirmation):
        asyncio.run(run_tool(registered()["routine_run"], ctx.turn, ctx, {"name": "chegada"}))
