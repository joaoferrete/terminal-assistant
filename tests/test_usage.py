"""Usage accounting: one row per answered model call, and honest sums (ADR 0018)."""
import sqlite3
from datetime import datetime

import httpx
import pytest
from starlette.testclient import TestClient
from test_daemon import FakeCalendar, FakeLighter

from ta import config as cfg_mod
from ta import db, usage
from ta.config import Config
from ta.daemon import create_app
from ta.llm import LLM
from ta.providers import DeepSeekProvider, Usage
from ta.usage import Price

NOON = datetime(2026, 9, 25, 12, 0)


def test_cost_is_per_million_tokens():
    assert usage.cost(Price(input=1.0, output=2.0), Usage(500_000, 250_000)) == 1.0


def test_an_unknown_price_is_none_not_zero():
    """Zero would read as "free" in the Digest; the honest answer is "unknown"."""
    assert usage.cost(None, Usage(10, 10)) is None


def test_the_sum_counts_unpriced_calls_instead_of_hiding_them(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    prices = {"deepseek-flash": Price(input=1.0, output=1.0)}
    for model in ("deepseek-flash", "deepseek-flash"):
        usage.record(conn, provider="deepseek", model=model, task="review_capture",
                     usage=Usage(1_000_000, 0), prices=prices, now=NOON)
    usage.record(conn, provider="gemini", model="gemini-flash-latest", task="organize",
                 usage=Usage(5, 5), prices=prices, now=NOON)

    spend = {s.provider: s for s in usage.spend_since(conn, datetime(2026, 9, 25))}
    assert spend["deepseek"].calls == 2 and spend["deepseek"].cost_usd == 2.0
    assert spend["gemini"].cost_usd == 0 and spend["gemini"].unpriced_calls == 1


def test_the_period_starts_where_it_is_asked_to(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    usage.record(conn, provider="deepseek", model="m", task="t", usage=Usage(1, 1),
                 prices={}, now=datetime(2026, 9, 24, 23, 59))
    assert usage.spend_since(conn, datetime(2026, 9, 25)) == []


def test_prices_come_from_config_toml_on_top_of_the_defaults(tmp_path, monkeypatch, caplog):
    (tmp_path / "ta").mkdir()
    (tmp_path / "ta" / "config.toml").write_text(
        '[llm.prices.gemini-flash-latest]\ninput = 0.3\noutput = 2.5\n'
        '[llm.prices.broken]\ninput = "cheap"\n'
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_mod._user_config.cache_clear()

    prices = cfg_mod.llm_prices()
    assert prices["gemini-flash-latest"] == Price(input=0.3, output=2.5)
    assert "deepseek-flash" in prices, "the built-in defaults stay"
    assert "broken" not in prices and "broken" in caplog.text


@pytest.fixture
def client(tmp_path):
    # The fakes are not optional: without them the app talks to the real
    # Evolution Data Server and writes the real Lighter settings (test_daemon).
    app = create_app(
        Config(auto_review=False),
        db_path=tmp_path / "t.db",
        rules_dir=tmp_path / "no-rules",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        yield c


def fake_deepseek(content='{"text": "ok"}'):
    return DeepSeekProvider("a-fake-key", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        })
    ))


def test_the_daemon_records_each_answered_call(client, tmp_path):
    """End to end through a real route: the hook is wired, and it writes a row."""
    llm = LLM(providers={"deepseek": fake_deepseek()}, default="deepseek")
    llm.on_usage = client.app.state.llm.on_usage
    client.app.state.llm = llm

    assert client.post("/digest-prose").json() == {"text": "ok"}

    # A separate connection to the same file: the daemon's belongs to its loop.
    row = sqlite3.connect(tmp_path / "t.db").execute(
        "SELECT provider, model, task, tokens_in, tokens_out, cost_usd FROM llm_usage"
    ).fetchone()
    assert row[:5] == ("deepseek", "deepseek-flash", "digest_prose", 100, 50)
    assert row[5] > 0, "deepseek-flash has a built-in price"


def test_a_failure_to_record_never_fails_the_call(client, monkeypatch):
    """The review it describes already succeeded; the bookkeeping must not undo it."""
    def broken(*a, **k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(usage, "record", broken)
    llm = LLM(providers={"deepseek": fake_deepseek()}, default="deepseek")
    llm.on_usage = client.app.state.llm.on_usage
    client.app.state.llm = llm

    assert client.post("/digest-prose").json() == {"text": "ok"}


# ── Ceilings (D30) and house rules (D29) ────────────────────────────────────
def test_unpriced_calls_count_at_the_worst_known_price(tmp_path):
    """Otherwise a model with no price would slip under every ceiling."""
    conn = db.connect(tmp_path / "t.db")
    prices = {"cheap": Price(1.0, 1.0), "dear": Price(10.0, 10.0)}
    usage.record(conn, provider="x", model="nobody-priced-this", task="agent",
                 usage=Usage(1_000_000, 0), prices=prices, member_id=1, now=NOON)
    assert usage.spent_by_member(conn, 1, datetime(2026, 9, 25), prices) == 10.0


def test_a_search_costs_its_grounding_too(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    usage.record(conn, provider="gemini", model="m", task="web_search", usage=Usage(0, 0),
                 prices={"m": Price(0, 0)}, member_id=1, now=NOON)
    assert usage.spent_total(conn, datetime(2026, 9, 25), {"m": Price(0, 0)}) == \
        usage.SEARCH_PER_CALL


def test_a_bad_ceiling_falls_back_to_the_default_not_to_none(tmp_path, monkeypatch, caplog):
    (tmp_path / "ta").mkdir()
    (tmp_path / "ta" / "config.toml").write_text(
        '[chat]\ndaily_usd_per_member = 0\nmonthly_usd_household = "muito"\n'
        'house_rules = "Sem diagnóstico médico."\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg_mod._user_config.cache_clear()
    chat = cfg_mod.chat_config()
    assert chat["daily_usd_per_member"] == 0.50 and chat["monthly_usd_household"] == 10.0
    assert chat["house_rules"] == "Sem diagnóstico médico."
    assert "daily_usd_per_member" in caplog.text


def test_within_budget_checks_the_day_and_the_month(tmp_path, monkeypatch):
    from ta.daemon import _within_budget

    monkeypatch.setattr("ta.daemon.chat_config", lambda: {
        "house_rules": "", "daily_usd_per_member": 1.0, "monthly_usd_household": 5.0})
    monkeypatch.setattr("ta.daemon.llm_prices", lambda: {"m": Price(1.0, 0)})
    conn = db.connect(tmp_path / "t.db")
    for mid in (2, 3):
        conn.execute("INSERT INTO members (id, handle, is_owner, created_at)"
                     " VALUES (?, ?, 0, 'x')", (mid, f"m{mid}"))
    assert _within_budget(conn, 1, now=NOON)
    usage.record(conn, provider="p", model="m", task="agent", usage=Usage(1_000_000, 0),
                 prices={"m": Price(1.0, 0)}, member_id=1, now=NOON)
    assert not _within_budget(conn, 1, now=NOON), "a dollar spent today hits the daily 1.0"
    assert _within_budget(conn, 2, now=NOON), "another Member's day is untouched"
    for _ in range(5):
        usage.record(conn, provider="p", model="m", task="agent", usage=Usage(1_000_000, 0),
                     prices={"m": Price(1.0, 0)}, member_id=3,
                     now=datetime(2026, 9, 2, 12, 0))
    assert not _within_budget(conn, 2, now=NOON), "the household's month is spent"


def test_a_spent_budget_still_captures_and_calls_no_model(tmp_path):
    import asyncio

    from test_agent import ScriptedLLM
    from test_bot import inbound
    from test_grants import TellingChannel

    from ta import store
    from ta.bot import AgentDeps, Bot
    from ta.grants import OWNER_PERMISSIONS
    from ta.tools import registered

    conn = db.connect(tmp_path / "t.db")
    llm = ScriptedLLM()      # any call would fail: none must happen
    b = Bot(conn, TellingChannel(), owner_username="dono",
            capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id),
            agent=AgentDeps(llm=llm, registry=registered,
                            permissions=lambda m: OWNER_PERMISSIONS,
                            within_budget=lambda m: False))
    asyncio.run(b.handle(inbound("/start")))
    asyncio.run(b.handle(inbound("qual a capital da Mongólia?")))
    asyncio.run(b.handle(inbound("e da Bolívia?")))
    assert llm.prompts == []
    assert len(store.list_notes(conn, viewer=__import__("ta.members").members.SYSTEM)) == 2
    assert len(b.channel.told) == 1, "the Owner hears about it once a day, not per message"


def test_house_rules_reach_the_agents_system_prompt(tmp_path):
    import asyncio

    from test_agent import ScriptedLLM, answer
    from test_bot import FakeChannel, inbound

    from ta import store
    from ta.bot import AgentDeps, Bot
    from ta.grants import OWNER_PERMISSIONS
    from ta.tools import registered

    conn = db.connect(tmp_path / "t.db")
    llm = ScriptedLLM(answer("ok"))
    b = Bot(conn, FakeChannel(), owner_username="dono",
            capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id),
            agent=AgentDeps(llm=llm, registry=registered,
                            permissions=lambda m: OWNER_PERMISSIONS,
                            house_rules=lambda: "Nunca dê diagnóstico médico."))
    asyncio.run(b.handle(inbound("/start")))
    asyncio.run(b.handle(inbound("o que é uma febre de 39?")))
    assert "Nunca dê diagnóstico médico." in llm.prompts[0][0]


def test_the_bots_name_and_personality_reach_the_system_prompt(tmp_path):
    import asyncio

    from test_agent import ScriptedLLM, answer
    from test_bot import FakeChannel, inbound

    from ta import store
    from ta.bot import AgentDeps, Bot
    from ta.grants import OWNER_PERMISSIONS
    from ta.tools import registered

    conn = db.connect(tmp_path / "t.db")
    llm = ScriptedLLM(answer("Bzzzt! Oi!"))
    b = Bot(conn, FakeChannel(), owner_username="dono",
            capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id),
            agent=AgentDeps(llm=llm, registry=registered,
                            permissions=lambda m: OWNER_PERMISSIONS,
                            identity=lambda: ("Rotombot", "animado, diz Bzzzt")))
    asyncio.run(b.handle(inbound("/start")))
    asyncio.run(b.handle(inbound("oi, tudo bem?")))
    system = llm.prompts[0][0]
    assert system.startswith("Your name is Rotombot.") and "Bzzzt" in system
    assert system.index("Bzzzt") < system.index("How to work:"), "style comes before the rules"
