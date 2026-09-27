"""Parity (F10, D47): the agent does what the board, the CLI and the commands do,
with the asker's Grant, and nothing permanent."""
import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from ta import builtin_tools, db, store
from ta.grants import OWNER_PERMISSIONS, Permissions
from ta.members import Viewer
from ta.tools import NotAllowed, Turn, registered
from ta.tools import run as run_tool


class FakeCal:
    available = True

    def __init__(self):
        self.created, self.deleted = [], []

    def today(self, day):
        return [SimpleNamespace(start=datetime(2026, 9, 27, 14), end=datetime(2026, 9, 27, 15),
                                all_day=False, summary="Dentista", calendar="eu")]

    def write_targets(self):
        return [SimpleNamespace(uid="eu|ta")]

    def create_event(self, uid, title, start, end):
        self.created.append((uid, title, start, end))
        return "ev1"


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    said = []

    async def say(text):
        said.append(text)

    link = SimpleNamespace(configured=True, consent=lambda m: f"https://accounts/consent?m={m}")
    cal = FakeCal()

    def build(member_id=1, perms=OWNER_PERMISSIONS, in_group=False):
        turn = Turn(member_id=member_id, conversation_id="c", in_group=in_group, permissions=perms)
        c = builtin_tools.ToolContext(conn=conn, turn=turn, channel="telegram", services={
            "say": say, "board_link": lambda m: f"https://board/{m}", "mail_link": link,
            "calendar_link": link, "satellite_code": lambda m: "123456",
            "calendar": lambda m: cal})
        c.said, c.cal = said, cal
        return c
    return build


def run(c, tool_name, **args):
    return asyncio.run(run_tool(registered()[tool_name], c.turn, c, args))


def test_edit_changes_and_undo_puts_it_back(ctx):
    c = ctx()
    n = store.add_note(c.conn, "ligar pro dentista", owner_id=1)
    run(c, "notes_edit", id=str(n.id), text="ligar pra clínica", due="2026-10-01", priority="high")
    got = store.get_note(c.conn, n.id, viewer=Viewer(1))
    assert (got.text, got.due, got.priority) == ("ligar pra clínica", "2026-10-01", "high")
    undo = c.turn.receipts[-1]["undo"]
    run(c, undo["tool"], **undo["args"])
    got = store.get_note(c.conn, n.id, viewer=Viewer(1))
    assert (got.text, got.due, got.priority) == ("ligar pro dentista", None, None)


def test_nobody_edits_or_deletes_someone_elses_note(ctx):
    c = ctx()
    theirs = store.add_note(c.conn, "da ana", owner_id=2)
    assert "no note" in run(c, "notes_edit", id=str(theirs.id), text="x").text
    assert "no such" in run(c, "notes_delete", ids=str(theirs.id)).text
    assert store.get_note(c.conn, theirs.id, viewer=Viewer(2)).text == "da ana"


def test_delete_goes_to_the_trash_and_comes_back(ctx):
    c = ctx()
    n = store.add_note(c.conn, "rascunho", owner_id=1)
    run(c, "notes_delete", ids=f"#{n.id}")
    assert "rascunho" in run(c, "trash_list").text
    assert c.turn.receipts[-1]["undo"] == {"tool": "notes_restore", "args": {"ids": str(n.id)}}
    run(c, "notes_restore", ids=str(n.id))
    assert store.get_note(c.conn, n.id, viewer=Viewer(1)).deleted_at is None


def test_status_in_progress_with_an_undo(ctx):
    c = ctx()
    n = store.add_note(c.conn, "relatório", owner_id=1)
    run(c, "notes_status", ids=str(n.id), status="doing")
    assert store.get_note(c.conn, n.id, viewer=Viewer(1)).status == "doing"
    assert c.turn.receipts[-1]["undo"]["args"]["status"] == "todo"
    assert "notes_done" in run(c, "notes_status", ids=str(n.id), status="done").text


def test_calendar_is_read_and_written_in_private_only(ctx):
    c = ctx()
    assert "Dentista" in run(c, "calendar_day", day="amanhã").text
    run(c, "calendar_create", title="Janta", start="2026-09-30 20:00")
    assert c.cal.created[-1][1:3] == ("Janta", datetime(2026, 9, 30, 20))
    assert c.turn.receipts[-1]["undo"]["kind"] == "delete_calendar_event"
    assert "private" in run(ctx(in_group=True), "calendar_day").text


def test_links_are_sent_by_the_bot_not_retyped_by_the_model(ctx):
    c = ctx()
    out = run(c, "connect_email")
    assert "https://accounts/consent?m=1" in c.said[-1]
    assert "https://" not in out.text, "the model never holds the URL to copy it"
    run(c, "board_link")
    assert "https://board/1" in c.said[-1]
    assert "private" in run(ctx(in_group=True), "board_link").text


def test_media_and_the_ring_light_are_for_admins(ctx):
    c = ctx(member_id=2, perms=Permissions())
    for tool_name, args in (("media_control", {"action": "pause"}),
                            ("ringlight", {"action": "on"})):
        with pytest.raises(NotAllowed):
            run(c, tool_name, **args)


def test_a_personal_list_is_created_but_not_a_household_one(ctx):
    c = ctx(member_id=2, perms=Permissions())
    run(c, "list_create", name="Filmes")
    [lv] = [x for x in store.lists_for(c.conn, viewer=Viewer(2)) if x.name == "filmes"]
    assert lv.scope == "personal"
