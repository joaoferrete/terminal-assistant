"""Grants (D12, ADR 0016) and inviting Members (D21, T3.4).

Deny by default, the Owner holds everything, and an action is judged by the
Grant of whoever asked for it.
"""
import asyncio

import pytest
from starlette.testclient import TestClient
from test_bot import FakeChannel, inbound
from test_daemon import StubHome
from test_security import TOKEN, build

from ta import board_access, db, grants
from ta import config as cfg_mod
from ta import members as members_mod
from ta.bot import Bot
from ta.grants import Grant, Permissions

GROUPS = {"luz": ("light.",), "tudo": ("light.", "switch.")}
DEFINED = {
    "morador": Grant("morador", entities=("light.sala", "tomada"), lists=("Compras",)),
    "adulto": Grant("adulto", entities=("luz",), admin=True),
}


def perms(handle, *, owner=False, members=None):
    return grants.permissions(handle, is_owner=owner,
                              members=members or {"ana": ("morador",)},
                              grants=DEFINED, groups=GROUPS)


# ── Permissions ─────────────────────────────────────────────────────────────
def test_the_owner_may_do_everything():
    p = perms("dono", owner=True)
    assert p.entity("light.quarto") and p.list_("qualquer") and p.tool("x") and p.is_admin


def test_a_member_gets_exactly_what_the_grant_says():
    p = perms("ana")
    assert p.entity("light.sala")
    assert not p.entity("light.quarto"), "the Owner's bedroom is not in her Grant"
    assert p.list_("compras"), "list names are case-insensitive"
    assert not p.is_admin


def test_an_entity_can_be_named_by_domain_or_by_group():
    assert perms("bia", members={"bia": ("adulto",)}).entity("light.quarto")   # group `luz`
    assert Permissions(entity_patterns=("switch.",)).entity("switch.cafeteira")


def test_deny_by_default():
    p = perms("estranho")
    assert not p.entity("light.sala") and not p.list_("compras") and not p.is_admin


def test_an_undefined_grant_denies_and_says_so(caplog):
    p = perms("ana", members={"ana": ("nao_existe",)})
    assert not p.entity("light.sala")
    assert "nao_existe" in caplog.text


def test_malformed_grants_are_dropped_not_widened(caplog):
    loaded = grants.load({"x": {"entities": "light.sala", "admin": "yes"}, "y": 3})
    assert loaded["x"].entities == () and loaded["x"].admin is False
    assert "y" not in loaded
    assert "grants.x.entities" in caplog.text


# ── config.toml ─────────────────────────────────────────────────────────────
@pytest.fixture
def user_config(tmp_path, monkeypatch):
    def write(text):
        (tmp_path / "ta").mkdir(exist_ok=True)
        (tmp_path / "ta" / "config.toml").write_text(text)
        cfg_mod._user_config.cache_clear()

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_mod._user_config.cache_clear()
    return write


def test_members_and_grants_come_from_config(user_config):
    user_config('[members."@Ana"]\ngrants = ["morador"]\n'
                '[grants.morador]\nentities = ["light.sala"]\n')
    invited, defined = cfg_mod.grants_config()
    assert invited == {"ana": ("morador",)}
    assert defined["morador"].entities == ("light.sala",)


def test_compras_exists_unless_the_file_says_otherwise(user_config):
    user_config("")
    assert cfg_mod.lists_config() == {"compras": "household"}
    user_config('[lists]\nmercado = "household"\nfilmes = "personal"\n')
    assert cfg_mod.lists_config() == {"mercado": "household"}


def test_groups_are_allowlisted_by_id(user_config):
    user_config("[channel.telegram]\ngroups = [-100123, -100456]\n")
    assert cfg_mod.telegram_groups() == {"-100123", "-100456"}


# ── Through the daemon ──────────────────────────────────────────────────────
INVENTORY = [
    {"entity_id": "light.sala", "state": "on", "attributes": {"friendly_name": "Sala"}},
    {"entity_id": "light.quarto", "state": "on", "attributes": {"friendly_name": "Quarto"}},
]


@pytest.fixture
def ana(tmp_path, user_config):
    user_config('[members.ana]\ngrants = ["morador"]\n'
                '[grants.morador]\nentities = ["light.sala"]\n')
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        home = StubHome(INVENTORY)
        c.app.state.home = home
        member = members_mod.by_handle(db.connect(tmp_path / "t.db"), "ana")
        c.cookies.set(board_access.COOKIE, board_access.session_cookie(TOKEN, member.id))
        yield c, home


def test_boot_creates_invited_members_and_the_declared_lists(tmp_path, user_config):
    user_config('[members.ana]\ngrants = []\n')
    with TestClient(build(tmp_path)):
        pass
    conn = db.connect(tmp_path / "t.db")
    assert [m.handle for m in members_mod.all_members(conn)][1:] == ["ana"]
    assert conn.execute("SELECT name, scope FROM lists").fetchall()[0][:] == ("compras",
                                                                            "household")


def test_a_housemate_switches_only_what_her_grant_covers(ana):
    c, home = ana
    assert c.post("/home/light", json={"entity": "sala"}).status_code == 200
    assert c.post("/home/light", json={"entity": "quarto"}).status_code == 403
    assert home.calls == [("light.sala", 100)]


def test_turn_everything_off_means_everything_she_may_touch(ana):
    """A housemate's "all off" must not reach the Owner's bedroom mid-call."""
    c, home = ana
    c.post("/home/off")
    assert home.turned_off == ["light.sala"]


def test_the_entity_list_shows_only_hers(ana):
    c, _ = ana
    ids = [e["entity_id"] for e in c.get("/home/entities").json()["entities"]]
    assert ids == ["light.sala"]


def test_the_ringlight_and_media_are_admin_only(ana):
    c, _ = ana
    assert c.post("/lighter", json={"on": True}).status_code == 403
    assert c.post("/media", json={"action": "pause"}).status_code == 403


# ── Pairing an invited Member ───────────────────────────────────────────────
class TellingChannel(FakeChannel):
    def __init__(self):
        super().__init__()
        self.told = []

    async def send(self, conversation_id, text):
        self.told.append((conversation_id, text))


@pytest.fixture
def house_bot(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    members_mod.sync(conn, owner="dono", invited=["ana"])
    invited = {"ana"}
    captured = []

    def capture(text, owner_id=1):
        captured.append((text, owner_id))
        return type("N", (), {"id": len(captured), "due": None})()

    b = Bot(conn, TellingChannel(), capture=capture, owner_username="dono",
            invited=lambda: invited)
    b.captured, b.invited_set = captured, invited
    asyncio.run(b.handle(inbound("/start")))          # the Owner pairs first
    return b, conn


def test_an_invited_member_pairs_and_the_owner_is_told(house_bot):
    b, conn = house_bot
    asyncio.run(b.handle(inbound("comprar pão", sender_id="2002", username="ana")))
    ana = members_mod.by_handle(conn, "ana")
    assert b.captured[-1] == ("comprar pão", ana.id), "the Note is hers, not the Owner's"
    assert b.channel.told and b.channel.told[0][0] == "1001", "told in the Owner's private chat"
    assert "@ana" in b.channel.told[0][1]


def test_removing_a_member_from_the_config_revokes_them(house_bot):
    b, _ = house_bot
    asyncio.run(b.handle(inbound("oi", sender_id="2002", username="ana")))
    b.invited_set.clear()
    sent = len(b.channel.sent)
    asyncio.run(b.handle(inbound("ainda aqui?", sender_id="2002", username="ana")))
    assert len(b.channel.sent) == sent and b.captured[-1][0] == "oi"


def test_an_invited_username_taken_over_after_pairing_gets_nobody(house_bot):
    """Invariant 4, for Members as well as the Owner."""
    b, _ = house_bot
    asyncio.run(b.handle(inbound("sou a ana", sender_id="2002", username="ana")))
    asyncio.run(b.handle(inbound("também sou", sender_id="6666", username="ana")))
    assert [t for t, _ in b.captured] == ["sou a ana"]


def test_a_group_outside_the_allowlist_is_ignored(house_bot):
    b, _ = house_bot
    sent = len(b.channel.sent)
    asyncio.run(b.handle(inbound("oi grupo", private=False)))
    assert len(b.channel.sent) == sent and b.captured == []
