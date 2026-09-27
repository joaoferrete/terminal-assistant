"""The pushed Digest (F5, D15, D35): sections that never take each other down,
admin-only parts, and exactly one message a day at the Member's hour."""
import asyncio
import json
from datetime import date, datetime

import httpx
import pytest

from ta import db, digest, store
from ta.digest import Settings, Sources

TODAY = date(2026, 9, 26)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


# ── When ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("text", "expected"), [
    ("7", "07:00"), ("7h", "07:00"), ("07:30", "07:30"), ("7:5", "07:05"),
    ("25:00", None), ("sete", None), ("", None),
])
def test_times_are_read_as_people_write_them(text, expected):
    assert digest.parse_time(text) == expected


def test_it_goes_once_a_day_at_its_hour_and_not_long_after():
    s = Settings(enabled=True, time="07:00")
    assert not digest.is_due(s, datetime(2026, 9, 26, 6, 59))
    assert digest.is_due(s, datetime(2026, 9, 26, 7, 0))
    assert digest.is_due(s, datetime(2026, 9, 26, 9, 30)), "a short outage still gets it"
    assert not digest.is_due(s, datetime(2026, 9, 26, 10, 1)), "an 07:00 summary at 10 is noise"
    s.last = "2026-09-26"
    assert not digest.is_due(s, datetime(2026, 9, 26, 7, 5)), "never twice"


def test_only_the_owner_has_it_on_by_default(conn):
    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    assert Settings.load(conn, 1).enabled, "migration 12 turns the Owner's on"
    assert not Settings.load(conn, 2).enabled, "a housemate asks for theirs"


# ── What ────────────────────────────────────────────────────────────────────
def build(src, member=1, admin=True, **kw):
    return asyncio.run(digest.build(src, member, is_admin=admin, today=TODAY, **kw))


def test_notes_show_what_is_overdue_and_due_today(conn):
    store.add_note(conn, "pagar o condomínio @2026-09-24")
    store.add_note(conn, "renovar o seguro @2026-09-26")
    store.add_note(conn, "algum dia")
    out = build(Sources(conn=conn), sections=("notes",))
    assert "pagar o condomínio" in out and "vencida" in out and "renovar o seguro" in out
    assert "algum dia" not in out


def test_admin_sections_are_left_out_for_everyone_else(conn, tmp_path):
    proc = fake_proc(tmp_path)
    src = Sources(conn=conn, proc=proc, thermal=tmp_path / "none")
    assert "Servidor" in build(src, sections=("server",), admin=True)
    assert "Servidor" not in build(src, sections=("server", "ai_cost", "adguard"), admin=False)


def test_a_failing_section_does_not_take_the_others_down(conn):
    def broken(request):
        raise httpx.ConnectError("down")

    src = Sources(conn=conn, weather={"latitude": 1, "longitude": 2, "place": "Casa"},
                  transport=httpx.MockTransport(broken))
    out = build(src, sections=("weather", "notes"))
    assert "Nada vencido" in out, "notes still there with the weather down"


def test_weather_comes_from_open_meteo_and_credits_it(conn):
    seen = []

    def meteo(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"daily": {
            "weather_code": [61], "temperature_2m_max": [24.6], "temperature_2m_min": [15.2],
            "precipitation_probability_max": [80]}})

    src = Sources(conn=conn, weather={"latitude": -23.5, "longitude": -46.6, "place": "SP"},
                  transport=httpx.MockTransport(meteo))
    out = build(src, sections=("weather",))
    assert "SP: chuva, 15–25°C, chuva 80%" in out
    assert "Open-Meteo" in out, "CC BY 4.0 asks for the credit"
    assert seen[0]["latitude"] == "-23.5"


def test_no_location_means_no_weather_rather_than_a_guess(conn):
    assert "°C" not in build(Sources(conn=conn), sections=("weather",))


def test_adguard_stats_for_admins(conn):
    def adguard(request):
        assert request.url.path == "/control/stats"
        return httpx.Response(200, json={
            "num_dns_queries": 1000, "num_blocked_filtering": 250,
            "top_blocked_domains": [{"ads.example": 90}, {"tracker.example": 40}],
            "top_clients": [{"192.168.68.20": 500}]})

    src = Sources(conn=conn, adguard={"url": "http://adguard", "user": "a", "password": "b"},
                  transport=httpx.MockTransport(adguard))
    out = build(src, sections=("adguard",))
    assert "1000 consultas · 250 bloqueadas (25%)" in out
    assert "ads.example" in out and "192.168.68.20" in out


def fake_proc(tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "loadavg").write_text("0.42 0.30 0.20 1/100 999\n")
    (proc / "meminfo").write_text("MemTotal: 8000000 kB\nMemAvailable: 6000000 kB\n")
    (proc / "uptime").write_text("172800.0 1000.0\n")
    return proc


def test_server_health_reads_the_machine(conn, tmp_path):
    thermal = tmp_path / "thermal" / "thermal_zone0"
    thermal.mkdir(parents=True)
    (thermal / "temp").write_text("52000\n")
    out = build(Sources(conn=conn, proc=fake_proc(tmp_path), thermal=tmp_path / "thermal",
                        disk=str(tmp_path)), sections=("server",))
    assert "carga 0.42 · RAM 25%" in out and "no ar há 2 dia(s)" in out and "52°C" in out


# ── Sending it ──────────────────────────────────────────────────────────────
def test_the_daemon_sends_it_once_to_the_members_private_chat(tmp_path):
    from starlette.testclient import TestClient
    from test_daemon import FakeCalendar, FakeLighter
    from test_groups import SendingChannel

    from ta.config import Config
    from ta.daemon import _send_due_digests, create_app

    ch = SendingChannel()
    app = create_app(Config(auto_review=False), db_path=tmp_path / "t.db",
                     rules_dir=tmp_path / "x", calendar=FakeCalendar(), lighter=FakeLighter(),
                     channel=ch, background=False)
    with TestClient(app) as c:
        side = db.connect(tmp_path / "t.db")
        side.execute("INSERT INTO channel_identities (channel, external_id, username, role,"
                     " paired_at, member_id) VALUES ('telegram', '1001', 'dono', 'owner', 'x', 1)")

        async def twice():
            at = datetime(2026, 9, 26, 7, 1)
            return await _send_due_digests(c.app, at), await _send_due_digests(c.app, at)

        assert c.portal.call(twice) == (1, 0)
    assert ch.sent_to[0][0] == "1001" and "Seu dia" in ch.sent_to[0][1]
    assert json.loads(side.execute("SELECT digest FROM members WHERE id = 1").fetchone()[0])[
        "last"] == "2026-09-26"


def test_a_member_turns_theirs_on_from_the_chat(conn):
    from ta.builtin_tools import ToolContext
    from ta.grants import OWNER_PERMISSIONS
    from ta.tools import Turn, registered
    from ta.tools import run as run_tool

    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    turn = Turn(member_id=2, conversation_id="c", in_group=False, permissions=OWNER_PERMISSIONS)
    ctx = ToolContext(conn=conn, turn=turn, channel="telegram", services={})
    asyncio.run(run_tool(registered()["digest_set"], turn, ctx, {"time": "8h"}))
    s = Settings.load(conn, 2)
    assert s.enabled and s.time == "08:00"
