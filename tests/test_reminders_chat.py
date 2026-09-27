"""Reminders on the chat (F9): "daqui 10 min tirar o bolo" rings on Telegram.

What must hold: a delay becomes a moment; a Reminder goes to its writer's private
chat and nobody else's; the Owner's desktop and speakers get only the Owner's;
[Done] and [+10 min] work only for the writer.
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from test_bot import FakeChannel, inbound

from ta import builtin_tools, daemon, db, i18n, notes, store
from ta.bot import Bot
from ta.channel import Inbound
from ta.grants import OWNER_PERMISSIONS
from ta.members import Viewer
from ta.tools import Turn, registered
from ta.tools import run as run_tool

NOW = datetime(2026, 9, 26, 14, 0)


# ── The parser ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("text", "at"), [
    ("me avisa daqui 10 min pra tirar o bolo", NOW + timedelta(minutes=10)),
    ("daqui a meia hora ver o forno", NOW + timedelta(minutes=30)),
    ("timer de 15 min", NOW + timedelta(minutes=15)),
    ("em 2 horas buscar a encomenda", NOW + timedelta(hours=2)),
])
def test_a_delay_becomes_a_reminder(text, at):
    assert notes.parse(text, now=NOW).remind_at == at


@pytest.mark.parametrize("text", ["leva 10 min pra ficar pronto", "reunião em 15 de outubro",
                                  "bateria dura 8h"])
def test_a_duration_is_not_a_moment(text):
    assert notes.parse(text, now=NOW).remind_at is None


def test_english_has_its_own_forms(monkeypatch):
    monkeypatch.setenv("TA_LANG", "en")
    i18n.reset_cache()
    assert notes.parse("take the cake out in 10 minutes", now=NOW).remind_at == \
        NOW + timedelta(minutes=10)
    assert notes.parse("check the oven in half an hour", now=NOW).remind_at == \
        NOW + timedelta(minutes=30)


# ── Delivery ────────────────────────────────────────────────────────────────
class SendingChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.to = []

    async def send(self, conversation_id, text, buttons=None, **kw):
        self.to.append(conversation_id)
        self.sent.append(text)
        self.buttons.append(buttons or [])
        return f"bot{len(self.sent)}"


@pytest.fixture
def bot(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    b = Bot(conn, SendingChannel(), owner_username="dono", invited=lambda: {"ana"},
            capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id))
    asyncio.run(b.handle(inbound("/start")))                                  # the Owner: 1001
    asyncio.run(b.handle(inbound("/start", sender_id="2002", username="ana")))  # Ana: 2002
    return b


def reminder(conn, owner_id, text="tirar o bolo"):
    note = store.add_note(conn, text, owner_id=owner_id)
    conn.execute("UPDATE notes SET remind_at = ? WHERE id = ?", (NOW.isoformat(), note.id))
    return store.get_note(conn, note.id, viewer=Viewer(owner_id))


def press(data, sender="1001"):
    return Inbound(channel="telegram", conversation_id=sender, message_id="9", sender_id=sender,
                   sender_username=None, private=True, text="", callback=data)


def test_a_reminder_rings_in_its_writers_chat_only(bot):
    note = reminder(bot.conn, 2)
    assert asyncio.run(bot.remind(note))
    assert bot.channel.to[-1] == "2002", "Ana's chat, not the Owner's"
    assert "tirar o bolo" in bot.channel.sent[-1]
    assert [b.label for b in bot.channel.buttons[-1]] == ["Concluir", "+10 min"]


def test_done_completes_it_and_only_for_the_writer(bot):
    note = reminder(bot.conn, 2)
    asyncio.run(bot.remind(note))
    done = bot.channel.buttons[-1][0].data
    asyncio.run(bot.handle(press(done, sender="1001")))
    assert bot.channel.acks[-1] != "Concluída ✓", "the Owner cannot press Ana's button"
    asyncio.run(bot.handle(press(done, sender="2002")))
    assert store.get_note(bot.conn, note.id, viewer=Viewer(2)).status == "done"


def test_snooze_brings_it_back_in_ten_minutes(bot):
    note = reminder(bot.conn, 1)
    store.mark_fired(bot.conn, note.id, now=NOW)
    asyncio.run(bot.remind(note))
    asyncio.run(bot.handle(press(bot.channel.buttons[-1][1].data)))
    again = store.get_note(bot.conn, note.id, viewer=Viewer(1))
    assert again.fired_at is None
    assert datetime.fromisoformat(again.remind_at) > datetime.now() + timedelta(minutes=9)


class FakeNotify:
    def __init__(self):
        self.sent = []

    async def send(self, title, body="", **kw):
        self.sent.append(body)


def test_the_owners_desktop_gets_only_the_owners_reminders(bot, monkeypatch):
    """Before F9 every Member's Reminder went to the Owner's laptop and speakers."""
    async def nothing(*a, **kw):
        return None

    monkeypatch.setattr(daemon.engine, "dispatch", nothing)
    monkeypatch.setattr(daemon, "_make_context", lambda *a, **kw: None)
    reminder(bot.conn, 2, "ligar pro médico")
    reminder(bot.conn, 1, "reunião")
    notify = FakeNotify()
    app = SimpleNamespace(state=SimpleNamespace(
        conn=bot.conn, notify=notify, bot=bot, rules=[],
        config=SimpleNamespace(echo_entities=[])))
    asyncio.run(daemon._fire_reminders(app, NOW + timedelta(seconds=5)))
    assert notify.sent == ["reunião"]
    assert sorted(bot.channel.to[-2:]) == ["1001", "2002"], "each in their own chat"


def test_quanto_falta_lists_timers_too(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    note = store.add_note(conn, "tirar o bolo", owner_id=1)
    conn.execute("UPDATE notes SET remind_at = ? WHERE id = ?",
                 ((NOW + timedelta(minutes=7)).isoformat(), note.id))
    turn = Turn(member_id=1, conversation_id="c", in_group=False, permissions=OWNER_PERMISSIONS)
    ctx = builtin_tools.ToolContext(conn=conn, turn=turn, channel="telegram",
                                    services={"now": lambda: NOW})
    out = asyncio.run(run_tool(registered()["schedule_list"], turn, ctx, {}))
    assert "tirar o bolo" in out.text and "7 min" in out.text


def test_the_capture_reply_says_when_it_will_ring(bot):
    asyncio.run(bot.handle(inbound("daqui 10 min tirar o bolo")))
    assert "te lembro às" in bot.channel.sent[-1]
