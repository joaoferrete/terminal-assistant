"""Members from the Owner's chat (D59), strangers (D60), /moradores and
/satellites (F10)."""
import asyncio
import dataclasses
import types

import pytest
from test_bot import inbound
from test_reminders_chat import SendingChannel

from ta import builtin_tools, db, settings
from ta import config as cfg_mod
from ta.bot import AgentDeps, Bot
from ta.channel import Inbound
from ta.grants import OWNER_PERMISSIONS, Permissions
from ta.i18n import t
from ta.satellite import Hub
from ta.tools import NeedsConfirmation, NotAllowed, Turn, registered
from ta.tools import run as run_tool

CONFIG = """# a casa
[channel.telegram]
owner = "dono"

[grants.morador]   # quem mora aqui
entities = ["light.sala"]
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "ta").mkdir(parents=True)
    path = tmp_path / "xdg" / "ta" / "config.toml"
    path.write_text(CONFIG)
    cfg_mod._user_config.cache_clear()
    return path


def test_a_member_is_added_with_an_existing_grant_and_comments_stay(home):
    backup = settings.set_member(home, "@Ana_Silva", ["morador"])
    text = home.read_text()
    assert "[members.ana_silva]" in text and 'grants = ["morador"]' in text
    assert "# quem mora aqui" in text and backup.read_text() == CONFIG
    with pytest.raises(settings.Invalid, match="no such Grant"):
        settings.set_member(home, "beto", ["admin_total"])
    settings.set_member(home, "ana_silva", None)
    assert "ana_silva" not in home.read_text()


@pytest.fixture
def owner_ctx(home, tmp_path):
    conn = db.connect(tmp_path / "t.db")

    def build(member_id=1, perms=OWNER_PERMISSIONS):
        turn = Turn(member_id=member_id, conversation_id="c", in_group=False, permissions=perms)
        return builtin_tools.ToolContext(conn=conn, turn=turn, channel="telegram", services={})
    return build


def test_adding_always_asks_and_then_applies_with_no_restart(owner_ctx, home):
    c = owner_ctx()
    with pytest.raises(NeedsConfirmation):
        asyncio.run(run_tool(registered()["member_add"], c.turn, c,
                             {"username": "ana", "grant": "morador"}))
    asyncio.run(run_tool(registered()["member_add"], c.turn, c,
                         {"username": "ana", "grant": "morador"}, confirmed=True))
    assert "ana" in cfg_mod.grants_config()[0], "read again at once"
    assert c.conn.execute("SELECT 1 FROM members WHERE handle = 'ana'").fetchone()


def test_nobody_but_the_owner_manages_members(owner_ctx):
    c = owner_ctx(member_id=2, perms=Permissions())
    with pytest.raises(NotAllowed):
        asyncio.run(run_tool(registered()["member_add"], c.turn, c, {"username": "x"},
                             confirmed=True))
    c.conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'b', 0, 'x')")
    admin = owner_ctx(member_id=2, perms=Permissions(admin=True))
    out = asyncio.run(run_tool(registered()["member_add"], admin.turn, admin,
                               {"username": "x"}, confirmed=True))
    assert "only the owner" in out.text, "an admin Grant is not the Owner"


# ── Strangers ───────────────────────────────────────────────────────────────
@pytest.fixture
def bot(home, tmp_path):
    conn = db.connect(tmp_path / "b.db")
    b = Bot(conn, SendingChannel(), owner_username="dono",
            capture=lambda text, owner_id=1: types.SimpleNamespace(id=1, due=None),
            agent=AgentDeps(llm=None, registry=registered,
                            permissions=lambda m: OWNER_PERMISSIONS, services={}))
    asyncio.run(b.handle(inbound("/start")))
    return b


def knock(b, text="oi, é a Jo"):
    msg = dataclasses.replace(inbound(text, sender_id="7777", username="joana"),
                              sender_name="Joana Silva")
    asyncio.run(b.handle(msg))


def press(data):
    return Inbound(channel="telegram", conversation_id="1001", message_id="9", sender_id="1001",
                   sender_username="dono", private=True, text="", callback=data)


def test_a_stranger_gets_one_reply_and_the_owner_one_message_a_day(bot):
    knock(bot)
    knock(bot, "oi?? alguém?")
    assert bot.channel.sent.count(t("bot.stranger")) == 1
    told = [m for m in bot.channel.sent if "Joana Silva @joana" in m]
    assert len(told) == 1 and "oi, é a Jo" in told[0]
    assert [b.label for b in bot.channel.buttons[bot.channel.sent.index(told[0])]] == \
        ["Liberar", "Ignorar"]


def test_allow_asks_the_grant_and_the_choice_writes_the_config(bot, home):
    knock(bot)
    allow = next(b.data for bs in bot.channel.buttons for b in bs if b.data.startswith("allow:"))
    asyncio.run(bot.handle(press(allow)))
    choices = {b.label: b.data for b in bot.channel.buttons[-1]}
    assert set(choices) == {"morador", "só notas"}
    asyncio.run(bot.handle(press(choices["morador"])))
    assert "[members.joana]" in home.read_text()


def test_moradores_shows_when_each_last_talked(bot, home):
    settings.set_member(home, "ana", [])
    cfg_mod._user_config.cache_clear()
    out = bot._members_text()
    assert "@ana" in out and "ainda não falou" in out


def test_satellites_are_listed_by_computer_with_their_state(bot):
    hub = Hub()
    hub.seen(1, "notebook-do-joao")
    bot.agent.services["hub"] = hub
    out = bot._satellites_text(1)
    assert "notebook-do-joao" in out and "🟢" in out
    assert "Nenhum" in bot._satellites_text(2)
