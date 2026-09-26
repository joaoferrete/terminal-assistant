"""Satellites (F6): the laptop reports, the server decides, and capture never waits.

Invariant 5 is the one this file is for: `ta note` on a Satellite never waits for
the server.
"""
import asyncio
import os
import stat
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient
from test_security import TOKEN, build

from ta import board_access, db, outbox, satellite
from ta.members import SYSTEM
from ta.satellite import Hub, RemoteLighter, RemoteNotifier


# ── The hub and the remote actuators ────────────────────────────────────────
def test_actions_wait_for_the_satellite_and_arrive_together():
    async def go():
        hub = Hub()
        hub.push(1, {"kind": "lighter", "op": "toggle"})
        hub.push(1, {"kind": "notify", "title": "x"})
        return await hub.next(1, wait=0.1), await hub.next(1, wait=0.05)

    first, empty = asyncio.run(go())
    assert [a["kind"] for a in first] == ["lighter", "notify"] and empty == []


def test_a_long_absence_does_not_come_back_to_a_thousand_actions():
    hub = Hub()
    for i in range(satellite.MAX_QUEUED + 10):
        hub.push(1, {"n": i})
    got = asyncio.run(hub.next(1, wait=0.1))
    assert len(got) == satellite.MAX_QUEUED and got[-1]["n"] == satellite.MAX_QUEUED + 9


def test_the_remote_ring_light_is_honest_about_a_laptop_that_is_away(monkeypatch):
    hub = Hub()
    lighter = RemoteLighter(hub, 1)
    assert not lighter.available
    assert asyncio.run(lighter.enable(True)) is False, "no pretending it turned on"
    hub.seen(1)
    assert asyncio.run(lighter.apply_profile("Meet")) is True
    assert asyncio.run(hub.next(1, wait=0.1)) == [
        {"kind": "lighter", "op": "apply_profile", "name": "Meet", "enable": True}]
    later = satellite.time.monotonic() + satellite.SEEN_FOR + 1
    monkeypatch.setattr(satellite.time, "monotonic", lambda: later)
    assert not RemoteNotifier(hub, 1).available


def test_a_server_with_no_desktop_hands_the_ring_light_to_the_satellite(tmp_path):
    from test_daemon import FakeCalendar

    from ta.config import Config
    from ta.daemon import create_app

    class NoLighter:
        available = False

        async def take_over(self):
            pass

        async def hand_back(self):
            pass

    app = create_app(Config(auto_review=False), db_path=tmp_path / "t.db",
                     rules_dir=tmp_path / "x", calendar=FakeCalendar(), lighter=NoLighter(),
                     background=False)
    with TestClient(app) as c:
        assert isinstance(c.app.state.lighter, RemoteLighter)


# ── The routes ──────────────────────────────────────────────────────────────
@pytest.fixture
def server(tmp_path):
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        c.side = db.connect(tmp_path / "t.db")
        c.side.execute("INSERT INTO members (id, handle, is_owner, created_at)"
                       " VALUES (2, 'ana', 0, 'x')")
        yield c


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_the_owners_satellite_reports_the_microphone(server):
    seen = []

    async def on_mic(active, apps):
        seen.append((active, apps))

    server.app.state.on_mic = on_mic
    r = server.post("/satellite/signal", json={"mic": True, "apps": ["meet"]},
                    headers=bearer(TOKEN))
    assert r.status_code == 200 and seen == [(True, ["meet"])]


def test_a_housemates_laptop_does_not_drive_the_owners_rules(server):
    token = board_access.session_cookie(TOKEN, 2)
    assert server.post("/satellite/signal", json={"mic": True},
                       headers=bearer(token)).status_code == 403


def test_a_satellite_collects_what_was_queued_for_it(server):
    server.app.state.hub.push(1, {"kind": "notify", "title": "Lembrete"})
    r = server.get("/satellite/actions?wait=0.1", headers=bearer(TOKEN))
    assert r.json() == {"actions": [{"kind": "notify", "title": "Lembrete"}]}


def test_a_code_from_the_bot_buys_a_satellite_token_for_that_member(server):
    code = server.app.state.satellite_codes.issue(2)
    r = server.post("/satellite/redeem", json={"code": code})   # no credential needed
    assert r.status_code == 200
    token = r.json()["token"]
    note = server.post("/notes", json={"text": "da Ana"}, headers=bearer(token)).json()
    assert note["id"] and server.side.execute(
        "SELECT owner_id FROM notes WHERE id = ?", (note["id"],)).fetchone()[0] == 2
    assert server.post("/satellite/redeem", json={"code": code}).status_code == 401, "once"


def test_a_queued_note_keeps_the_day_it_was_written(server):
    """"amanhã" typed yesterday means today, whenever it arrives."""
    yesterday = datetime.now() - timedelta(days=1)
    r = server.post("/notes", json={"text": "pagar o boleto amanhã",
                                    "captured_at": yesterday.isoformat(timespec="seconds")},
                    headers=bearer(TOKEN))
    assert r.json()["due"] == date.today().isoformat()


def test_a_capture_time_in_the_future_is_refused(server):
    later = (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds")
    r = server.post("/notes", json={"text": "x", "captured_at": later}, headers=bearer(TOKEN))
    assert r.status_code == 400


# ── The capture queue (invariant 5) ─────────────────────────────────────────
def test_the_queue_is_private_and_keeps_order_through_a_partial_flush(tmp_path):
    q = tmp_path / "outbox.jsonl"
    for text in ("um", "dois", "três"):
        outbox.add(text, where=q)
    assert stat.S_IMODE(os.stat(q).st_mode) == 0o600

    sent = []

    def flaky(item):
        if item["text"] == "dois":
            raise ConnectionError
        sent.append(item["text"])

    assert outbox.flush(flaky, where=q) == (1, 2)
    assert [x["text"] for x in outbox.pending(where=q)] == ["dois", "três"]
    assert outbox.flush(lambda item: sent.append(item["text"]), where=q) == (2, 0)
    assert sent == ["um", "dois", "três"] and not q.exists()


def test_ta_note_on_a_satellite_never_waits_for_the_server(tmp_path, monkeypatch, capsys):
    from ta import cli
    from ta.config import Config

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    cfg = Config(server="http://192.0.2.1:7777", token=TOKEN)

    def down(*a, **k):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr(httpx, "request", down)
    assert cli.cmd_note(cfg, SimpleNamespace(text=["ligar", "pro", "dentista"])) == 0
    assert [x["text"] for x in outbox.pending()] == ["ligar pro dentista"]
    assert "1" in capsys.readouterr().out


def test_without_a_server_offline_is_still_an_error(monkeypatch):
    """A laptop running its own daemon has nothing to queue for: the daemon IS
    local, and "it is down" is the useful answer."""
    from ta import cli
    from ta.config import Config

    monkeypatch.setattr(httpx, "request", lambda *a, **k: (_ for _ in ()).throw(
        httpx.ConnectError("x")))
    with pytest.raises(cli.Offline):
        cli.cmd_note(Config(), SimpleNamespace(text=["x"]))


# ── The laptop side ─────────────────────────────────────────────────────────
class FakeLighter:
    def __init__(self):
        self.calls = []

    async def enable(self, on=True):
        self.calls.append(("enable", on))

    async def toggle(self):
        self.calls.append(("toggle",))

    async def apply_profile(self, name, *, enable=True):
        self.calls.append(("profile", name))

    async def set(self, key, value):
        self.calls.append(("set", key, value))


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, title, body="", *, urgency="normal"):
        self.sent.append(title)


def test_the_satellite_runs_what_the_server_asked_for():
    from ta.config import Config
    from ta.satellite_client import Satellite

    def server(request):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"actions": [
            {"kind": "lighter", "op": "apply_profile", "name": "Meet"},
            {"kind": "notify", "title": "Reunião"},
            {"kind": "unknown"}]})

    lighter, notifier = FakeLighter(), FakeNotifier()
    sat = Satellite(Config(server="http://srv", token=TOKEN), lighter=lighter,
                    notifier=notifier, transport=httpx.MockTransport(server))
    assert asyncio.run(sat.poll_once()) == 3
    assert lighter.calls == [("profile", "Meet")] and notifier.sent == ["Reunião"]


def test_everything_queued_for_a_member_is_theirs_only(server):
    server.app.state.hub.push(1, {"kind": "notify", "title": "do dono"})
    token = board_access.session_cookie(TOKEN, 2)
    r = server.get("/satellite/actions?wait=0.1", headers=bearer(token))
    assert r.json() == {"actions": []}
    assert SYSTEM is not None


def test_the_satellites_warnings_reach_its_journal(monkeypatch):
    """`main()` sets logging to ERROR first; the Satellite must override it, or a
    server that went away would never show in `journalctl --user -u ta-satellite`."""
    import logging

    from ta import cli
    from ta.config import Config

    logging.basicConfig(level=logging.ERROR, force=True)

    class Stop(Exception):
        pass

    def fake_run(coro):
        coro.close()
        raise Stop

    monkeypatch.setattr("asyncio.run", fake_run)
    with pytest.raises(Stop):
        cli.cmd_satellite(Config(server="http://srv", token=TOKEN),
                          SimpleNamespace(action="run", code=None))
    assert logging.getLogger().getEffectiveLevel() == logging.INFO
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
