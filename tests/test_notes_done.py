""""Comprei o leite", "terminei o relatório": the agent marks things done (F9).

What must hold: one's own Notes can always be finished; somebody else's item on a
household List only with that List in the Grant; another's private Note never;
and [Undo] reopens exactly what was closed.
"""
import asyncio

import pytest
from test_reminders_chat import bot, press  # noqa: F401 - the fixture

from ta import builtin_tools, db, store
from ta.bot import AgentDeps
from ta.grants import OWNER_PERMISSIONS, Permissions
from ta.members import Viewer
from ta.tools import Turn, registered
from ta.tools import run as run_tool


@pytest.fixture
def house(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    conn.execute("INSERT INTO lists (name, scope, owner_id, created_at)"
                 " VALUES ('compras', 'household', 1, 'x')")
    lid = conn.execute("SELECT id FROM lists WHERE name = 'compras'").fetchone()[0]
    milk = store.add_note(conn, "leite", owner_id=1, list_id=lid)
    report = store.add_note(conn, "relatório", owner_id=2)
    secret = store.add_note(conn, "presente surpresa", owner_id=1)
    return conn, milk, report, secret


def ctx_for(conn, member_id, perms):
    turn = Turn(member_id=member_id, conversation_id="c", in_group=False, permissions=perms)
    return builtin_tools.ToolContext(conn=conn, turn=turn, channel="telegram", services={})


def run(c, **args):
    return asyncio.run(run_tool(registered()["notes_done"], c.turn, c, args))


def status(conn, note, member_id):
    return store.get_note(conn, note.id, viewer=Viewer(member_id)).status


def test_ones_own_task_and_a_granted_list_item_are_marked(house):
    conn, milk, report, _ = house
    c = ctx_for(conn, 2, Permissions(lists=("compras",)))
    out = run(c, ids=f"#{report.id}, {milk.id}")
    assert status(conn, report, 2) == "done" and status(conn, milk, 2) == "done"
    assert "leite" in out.text
    assert [lv.items for lv in store.lists_for(conn, viewer=Viewer(2))] == [[]], "bought"


def test_without_the_list_in_the_grant_a_housemates_item_stays(house):
    conn, milk, _, _ = house
    out = run(ctx_for(conn, 2, Permissions()), ids=str(milk.id))
    assert status(conn, milk, 1) == "todo" and "not yours" in out.text


def test_another_members_private_note_is_not_even_found(house):
    conn, _, _, secret = house
    out = run(ctx_for(conn, 2, Permissions(lists=("compras",))), ids=str(secret.id))
    assert status(conn, secret, 1) == "todo" and "not found" in out.text


def test_undo_reopens_what_was_closed(bot):  # noqa: F811
    from test_agent import ScriptedLLM, answer, call

    note = store.add_note(bot.conn, "relatório", owner_id=1)
    bot.agent = AgentDeps(llm=ScriptedLLM(call("notes_done", f'{{"ids": "{note.id}"}}'),
                                          answer("Marquei como feito.")),
                          registry=registered, permissions=lambda m: OWNER_PERMISSIONS,
                          services={})
    from test_bot import inbound

    asyncio.run(bot.handle(inbound("terminei o relatório")))
    assert status(bot.conn, note, 1) == "done"
    [button] = bot.channel.buttons[-1]
    asyncio.run(bot.handle(press(button.data)))
    assert status(bot.conn, note, 1) == "todo"
