"""The config page (F8, D38): two factors, comments kept, secrets never shown.

The property the page exists to keep: a value from `.env` marked secret never
leaves the server — not in a response, not in the page.
"""
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from test_daemon import FakeCalendar, FakeLighter

from ta import board_access, settings
from ta import config as cfg_mod
from ta.config import Config
from ta.daemon import create_app
from ta.settings import Invalid

TOKEN = "a-test-token-not-a-secret"
PASSWORD = "senha-longa-de-config"
SECRET_VALUE = "sk-this-must-never-appear"


# ── Password and session ────────────────────────────────────────────────────
def test_the_password_is_stored_as_a_salted_scrypt_hash():
    h = settings.hash_password(PASSWORD)
    assert h.startswith("scrypt$") and PASSWORD not in h
    assert settings.hash_password(PASSWORD) != h, "salted"
    assert settings.verify_password(PASSWORD, h)
    assert not settings.verify_password("quase", h)
    assert not settings.verify_password(PASSWORD, "garbage")


def test_the_config_session_expires_and_cannot_be_forged():
    key = settings.hash_password(PASSWORD)
    c = settings.config_cookie(key, 1, now=1000)
    assert settings.config_member(key, c, now=1000 + 60) == 1
    assert settings.config_member(key, c, now=1000 + settings.CONFIG_TTL + 1) is None
    assert settings.config_member(key, c.replace("c1.1.", "c1.2.", 1), now=1001) is None
    other = settings.hash_password(PASSWORD)
    assert settings.config_member(other, c, now=1001) is None, "a new password logs out"


# ── config.toml ─────────────────────────────────────────────────────────────
ORIGINAL = '''# my house, my rules
lang = "pt"

[aliases]
# the one in the bedroom
quarto = "light.lampada_do_quarto"

[chat]
house_rules = "sem diagnóstico"   # soft
'''


@pytest.fixture
def toml(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(ORIGINAL)
    return p


def test_a_field_change_keeps_every_comment(toml):
    settings.write(toml, {"fields": {"chat.bot_name": "Rotombot"}})
    text = toml.read_text()
    assert "# my house, my rules" in text and "# the one in the bedroom" in text
    assert "# soft" in text and 'bot_name = "Rotombot"' in text


def test_a_table_is_replaced_with_the_comments_typed_in_it(toml):
    settings.write(toml, {"tables": {"aliases": '# lamps\nsala = "light.sala"\n'}})
    text = toml.read_text()
    assert "# lamps" in text and 'sala = "light.sala"' in text and "quarto" not in text
    assert "# my house, my rules" in text


def test_one_invalid_change_and_nothing_is_written(toml):
    with pytest.raises(Invalid):
        settings.write(toml, {"fields": {"chat.bot_name": "ok"},
                              "tables": {"llm.tasks": 'organize = "gpt"\n'}})
    assert toml.read_text() == ORIGINAL


@pytest.mark.parametrize(("table", "text"), [
    ("grants", '[morador]\nentities = "light.sala"\n'),
    ("grants", '[morador]\nsuperpowers = true\n'),
    ("members", '[ana]\ngrants = "morador"\n'),
    ("lists", 'filmes = "personal"\n'),
    ("llm.prices", '[m]\ninput = "barato"\noutput = 1\n'),
    ("aliases", "este não é toml ="),
])
def test_the_daemons_rules_refuse_on_the_page(toml, table, text):
    with pytest.raises(Invalid):
        settings.write(toml, {"tables": {table: text}})


def test_a_bad_ceiling_is_refused_not_silently_defaulted(toml):
    with pytest.raises(Invalid):
        settings.write(toml, {"fields": {"chat.daily_usd_per_member": "0"}})


def test_emptying_a_field_removes_the_setting(toml):
    settings.write(toml, {"fields": {"lang": ""}})
    assert "lang" not in toml.read_text().split("[aliases]")[0]


def test_every_write_leaves_a_backup(toml):
    backup = settings.write(toml, {"fields": {"chat.bot_name": "x"}})
    assert backup.read_text() == ORIGINAL


def test_reading_gives_fields_and_tables_as_text(toml):
    state = settings.read(toml)
    assert state["fields"]["lang"] == "pt"
    assert "light.lampada_do_quarto" in state["tables"]["aliases"]
    assert "lang" in state["restart"]


# ── .env ────────────────────────────────────────────────────────────────────
@pytest.fixture
def env(tmp_path):
    p = tmp_path / ".env"
    p.write_text(f"TA_HOST=0.0.0.0\nDEEPSEEK_API_KEY={SECRET_VALUE}\n")
    return p


def test_secrets_are_known_only_as_set_or_not(env):
    state = settings.read_env(env)
    assert state["secrets"]["DEEPSEEK_API_KEY"] is True
    assert state["secrets"]["GEMINI_API_KEY"] is False
    assert SECRET_VALUE not in repr(state)
    assert state["fields"]["TA_HOST"] == "0.0.0.0"


def test_env_writes_are_private_backed_up_and_one_line(env):
    import os
    import stat

    backup = settings.write_env(env, {"GEMINI_API_KEY": "new"})
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600 and backup.exists()
    assert "GEMINI_API_KEY=new" in env.read_text()
    with pytest.raises(Invalid):
        settings.write_env(env, {"HA_TOKEN": "a\nTA_TOKEN=injected"})
    with pytest.raises(Invalid):
        settings.write_env(env, {"PATH": "/tmp"})


# ── Through the daemon ──────────────────────────────────────────────────────
@pytest.fixture
def daemon(tmp_path, monkeypatch, env):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    (tmp_path / "xdg" / "ta").mkdir(parents=True)
    (tmp_path / "xdg" / "ta" / "config.toml").write_text(ORIGINAL)
    cfg_mod._user_config.cache_clear()
    app = create_app(Config(ha_token="fake", auto_review=False, host="0.0.0.0", token=TOKEN),
                     db_path=tmp_path / "t.db", rules_dir=tmp_path / "none",
                     calendar=FakeCalendar(), lighter=FakeLighter(), env_path=env,
                     background=False)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"      # the Owner's board session
        c.env = env
        yield c


def set_password(env):
    settings.write_env(env, {"TA_ADMIN_PASSWORD_HASH": settings.hash_password(PASSWORD)})


def test_without_a_password_the_page_says_how_to_set_one(daemon):
    r = daemon.post("/config/login", json={"password": "x"})
    assert r.status_code == 409 and "ta passwd" in r.json()["error"]


def test_the_board_session_alone_is_not_enough(daemon):
    set_password(daemon.env)
    assert daemon.get("/config/state").status_code == 401
    assert daemon.post("/config/login", json={"password": "errada"}).status_code == 401
    assert daemon.post("/config/login", json={"password": PASSWORD}).status_code == 200
    assert daemon.get("/config/state").status_code == 200


def test_a_housemate_never_gets_in_even_with_the_password(daemon, tmp_path):
    set_password(daemon.env)
    from ta import db

    db.connect(tmp_path / "t.db").execute(
        "INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    del daemon.headers["Authorization"]
    daemon.cookies.set(board_access.COOKIE, board_access.session_cookie(TOKEN, 2))
    assert daemon.get("/config").status_code == 403
    assert daemon.post("/config/login", json={"password": PASSWORD}).status_code == 403


def test_no_secret_value_ever_leaves_the_server(daemon):
    set_password(daemon.env)
    daemon.post("/config/login", json={"password": PASSWORD})
    r = daemon.get("/config/state")
    assert SECRET_VALUE not in r.text
    assert r.json()["env"]["secrets"]["DEEPSEEK_API_KEY"] is True
    assert SECRET_VALUE not in daemon.get("/config").text


def test_saving_writes_config_and_env_and_replacing_a_secret_works(daemon, tmp_path):
    set_password(daemon.env)
    daemon.post("/config/login", json={"password": PASSWORD})
    r = daemon.post("/config/save", json={"fields": {"chat.bot_name": "Rotombot"},
                                          "env": {"TA_PUBLIC_URL": "http://casa:7777"}})
    assert r.status_code == 200
    assert 'bot_name = "Rotombot"' in (tmp_path / "xdg" / "ta" / "config.toml").read_text()
    assert "TA_PUBLIC_URL=http://casa:7777" in daemon.env.read_text()
    assert daemon.post("/config/secret", json={"key": "GEMINI_API_KEY",
                                               "value": "novo"}).status_code == 200
    assert "GEMINI_API_KEY=novo" in daemon.env.read_text()
    assert daemon.post("/config/secret", json={"key": "PATH", "value": "/x"}).status_code == 400


def test_an_invalid_save_is_refused_with_the_reason(daemon, tmp_path):
    set_password(daemon.env)
    daemon.post("/config/login", json={"password": PASSWORD})
    r = daemon.post("/config/save", json={"tables": {"llm.tasks": 'organize = "gpt"'}})
    assert r.status_code == 400 and "providers" in r.json()["error"]
    assert (tmp_path / "xdg" / "ta" / "config.toml").read_text() == ORIGINAL


def test_restart_exits_so_systemd_brings_it_back(daemon, monkeypatch):
    """A non-zero exit is what `Restart=on-failure` restarts."""
    set_password(daemon.env)
    daemon.post("/config/login", json={"password": PASSWORD})
    codes = []
    monkeypatch.setattr("ta.daemon._exit_soon", codes.append)
    assert daemon.post("/config/restart").status_code == 200
    assert codes == [75]


def test_ta_passwd_stores_only_the_hash(tmp_path, monkeypatch):
    from ta import cli

    env = tmp_path / ".env"
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt="": next(answers))
    monkeypatch.setattr(cfg_mod, "env_file", lambda: env)
    assert cli.cmd_passwd(Config(), SimpleNamespace()) == 0
    text = env.read_text()
    assert PASSWORD not in text and text.startswith("TA_ADMIN_PASSWORD_HASH=scrypt$")


def test_ta_passwd_refuses_a_short_password(monkeypatch):
    from ta import cli

    monkeypatch.setattr("getpass.getpass", lambda prompt="": "curta")
    with pytest.raises(cli.Problem):
        cli.cmd_passwd(Config(), SimpleNamespace())
