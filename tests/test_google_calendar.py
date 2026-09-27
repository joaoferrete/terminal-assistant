"""Google Calendar per Member (D14, T5.1), against a simulated Google.

What matters: the pasted code only works for the link this bot issued, to whom
it issued it, once; tokens are born unreadable to anyone else; events never get
attendees; and the interface is the one Rules and the review already use.
"""
import asyncio
import json
import os
import stat
from datetime import date, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from test_bot import FakeChannel, inbound

from ta import db, memory, store
from ta.bot import Bot
from ta.members import SYSTEM
from ta.sensors.google_calendar import (
    SCOPES,
    CalendarError,
    Connector,
    GoogleCalendar,
    Link,
    TokenStore,
)


class FakeGoogle:
    """Token endpoint and Calendar API, recording what they were sent."""

    def __init__(self):
        self.posts, self.created = [], []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            self.posts.append(form)
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt",
                                                 "expires_in": 3600})
            if form.get("refresh_token") == ["rt"]:
                return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})
            return httpx.Response(400, json={"error": "invalid_grant"})
        path = request.url.path
        if path.endswith("/calendarList/primary"):
            return httpx.Response(200, json={"id": "eu@gmail.com"})
        if path.endswith("/calendarList"):
            return httpx.Response(200, json={"items": [
                {"id": "eu@gmail.com", "summary": "eu@gmail.com", "primary": True,
                 "selected": True},
                {"id": "ta123@group", "summary": "Terminal Assistant", "selected": True}]})
        if path.endswith("/events") and request.method == "GET":
            if "ta123" in path:
                return httpx.Response(200, json={"items": []})
            assert request.url.params["singleEvents"] == "true"
            return httpx.Response(200, json={"items": [
                {"id": "e1", "summary": "Dentista",
                 "start": {"dateTime": "2026-09-26T14:00:00-03:00"},
                 "end": {"dateTime": "2026-09-26T15:00:00-03:00"}},
                {"id": "e2", "summary": "Feriado", "start": {"date": "2026-09-26"},
                 "end": {"date": "2026-09-27"}}]})
        if path.endswith("/events") and request.method == "POST":
            self.created.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "new-event"})
        return httpx.Response(404)


@pytest.fixture
def google(tmp_path):
    fake = FakeGoogle()
    transport = httpx.MockTransport(fake)
    connector = Connector("cid", "secret", transport=transport)
    store_ = TokenStore(tmp_path / "google")
    return fake, transport, connector, store_


def pasted_for(url: str) -> str:
    state = parse_qs(urlparse(url).query)["state"][0]
    return f"http://localhost/?state={state}&code=4/abc&scope=x"


# ── The consent link and the paste-back ─────────────────────────────────────
def test_the_consent_link_asks_for_the_least_it_needs_and_uses_pkce(google):
    _, _, connector, _ = google
    q = parse_qs(urlparse(connector.consent_url(1)).query)
    assert q["scope"][0].split() == list(SCOPES)
    assert "https://www.googleapis.com/auth/calendar" not in q["scope"][0].split(), \
        "never the full scope, which can delete calendars"
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://localhost"]
    assert q["access_type"] == ["offline"]


def test_a_pasted_code_works_once_for_the_member_it_was_issued_to(google):
    fake, _, connector, _ = google
    url = connector.consent_url(1)
    with pytest.raises(CalendarError):
        connector.exchange(pasted_for(url), member_id=2)   # someone else's paste
    url = connector.consent_url(1)
    tokens = connector.exchange(pasted_for(url), member_id=1)
    assert tokens["refresh_token"] == "rt"
    assert fake.posts[-1]["code_verifier"], "PKCE binds the code to our link"
    with pytest.raises(CalendarError):
        connector.exchange(pasted_for(url), member_id=1)   # replayed


def test_only_the_localhost_redirect_is_taken_as_a_paste():
    assert Connector.looks_pasted("http://localhost/?state=a&code=b")
    assert not Connector.looks_pasted("comprar pão")
    assert not Connector.looks_pasted("https://evil.example/?state=a&code=b")


def test_tokens_are_born_readable_by_the_owner_only(google):
    _, _, _, store_ = google
    store_.save(1, "eu@gmail.com", "rt")
    path = next(store_.dir.glob("*.json"))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store_.dir).st_mode) == 0o700


# ── The calendar ────────────────────────────────────────────────────────────
@pytest.fixture
def cal(google):
    fake, transport, connector, store_ = google
    store_.save(1, "eu@gmail.com", "rt")
    return fake, GoogleCalendar(1, connector, store_, transport=transport)


def test_today_reads_timed_and_all_day_events(cal):
    _, c = cal
    events = c.today(date(2026, 9, 26))
    assert [e.summary for e in events] == ["Feriado", "Dentista"]
    dentista = events[1]
    assert not dentista.all_day and dentista.end - dentista.start == (
        datetime(2026, 9, 26, 15) - datetime(2026, 9, 26, 14))


def test_the_write_target_is_the_dedicated_calendar(cal):
    _, c = cal
    targets = c.write_targets()
    assert [t.name for t in targets] == ["Terminal Assistant"]
    assert targets[0].personal, "a gmail account routes as personal"


def test_an_event_never_gets_attendees(cal):
    """ADR 0007: guests would email real people."""
    fake, c = cal
    uid = c.create_event(c.write_targets()[0].uid, "Dentista",
                         datetime(2026, 9, 26, 14), datetime(2026, 9, 26, 15))
    assert uid == "new-event"
    assert "attendees" not in fake.created[0] and fake.created[0]["summary"] == "Dentista"


def test_a_refused_refresh_says_why(google):
    _, transport, connector, store_ = google
    store_.save(1, "eu@gmail.com", "expired")
    c = GoogleCalendar(1, connector, store_, transport=transport)
    with pytest.raises(CalendarError):
        c.today()
    assert "refresh token" in c.error


# ── From the chat ───────────────────────────────────────────────────────────
@pytest.fixture
def bot(tmp_path, google):
    _, transport, connector, store_ = google
    conn = db.connect(tmp_path / "t.db")
    b = Bot(conn, FakeChannel(), owner_username="dono",
            capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id),
            calendar_link=Link(connector, store_, transport=transport))
    asyncio.run(b.handle(inbound("/start")))
    return b, conn, store_


def test_connecting_from_the_chat_end_to_end(bot):
    b, conn, store_ = bot
    asyncio.run(b.handle(inbound("/conectar_agenda")))
    url = b.channel.sent[-1].split("autorize: ")[1].split("\n")[0]
    asyncio.run(b.handle(inbound(pasted_for(url))))
    assert "eu@gmail.com" in b.channel.sent[-1]
    assert store_.accounts(1) == [("eu@gmail.com", "rt")]


def test_the_pasted_address_is_never_a_note_or_a_memory(bot):
    """It carries a live authorisation code."""
    b, conn, _ = bot
    asyncio.run(b.handle(inbound("/conectar_agenda")))
    url = b.channel.sent[-1].split("autorize: ")[1].split("\n")[0]
    asyncio.run(b.handle(inbound(pasted_for(url))))
    assert store.list_notes(conn, viewer=SYSTEM) == []
    assert memory.search(conn, channel="telegram", conversation_id="c1", query="code") == []


def test_a_bad_paste_says_to_try_again(bot):
    b, _, store_ = bot
    asyncio.run(b.handle(inbound("http://localhost/?state=invented&code=x")))
    assert "/conectar_agenda" in b.channel.sent[-1]
    assert store_.accounts(1) == []


def test_the_daemon_uses_a_members_google_calendar_once_connected(tmp_path, google):
    from types import SimpleNamespace

    from ta.daemon import _calendar_for

    _, _, connector, store_ = google
    machine = SimpleNamespace(available=False)
    app = SimpleNamespace(state=SimpleNamespace(google=connector, google_tokens=store_,
                                                calendar=machine))
    assert _calendar_for(app, 1) is machine
    store_.save(1, "eu@gmail.com", "rt")
    assert isinstance(_calendar_for(app, 1), GoogleCalendar)
    assert _calendar_for(app, 2) is machine, "each Member has their own"
