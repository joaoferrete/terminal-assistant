"""The board link the bot sends, and the session cookie it buys (T1.7).

The bug behind this: on a phone the board 401'd on every reload. It strips
`?token=` from the address bar (ADR 0012), and a page load cannot send the header
the JavaScript builds from `localStorage`. The user had to scan the QR code again
after every refresh.
"""
import asyncio
from datetime import datetime, timedelta

from starlette.testclient import TestClient
from test_bot import FakeChannel, inbound
from test_security import TOKEN, build

from ta import board_access, db
from ta.board_access import BoardCodes, session_cookie, session_member, session_valid
from ta.bot import Bot
from ta.config import Config
from ta.daemon import _board_link

T0 = datetime(2026, 9, 25, 12, 0)


# ── The code ────────────────────────────────────────────────────────────────
def test_a_code_works_exactly_once():
    codes = BoardCodes()
    code = codes.issue(now=T0)
    assert codes.redeem(code, T0)
    assert not codes.redeem(code, T0), "a code in a chat log must not be reusable"


def test_a_code_expires_after_five_minutes():
    codes = BoardCodes()
    code = codes.issue(now=T0)
    assert not codes.redeem(code, T0 + timedelta(minutes=5, seconds=1))


def test_an_invented_code_is_refused():
    assert not BoardCodes().redeem("chute")


# ── The cookie ──────────────────────────────────────────────────────────────
def test_the_cookie_is_signed_not_the_token_itself():
    value = session_cookie(TOKEN, now=T0)
    assert TOKEN not in value
    assert session_valid(TOKEN, value, T0 + timedelta(days=1))


def test_a_tampered_or_expired_cookie_is_refused():
    value = session_cookie(TOKEN, 2, now=T0)
    version, member, issued, mac = value.split(".")
    assert not session_valid(TOKEN, f"{version}.{member}.{int(issued) + 1}.{mac}", T0)
    assert not session_valid(TOKEN, value, T0 + timedelta(days=91))
    assert not session_valid(TOKEN, "lixo", T0)


def test_editing_the_member_in_a_cookie_does_not_make_you_someone_else():
    """The cookie names who is looking (F3). Changing that number by hand must
    void it, or anyone could read the Owner's notes by typing a 1."""
    value = session_cookie(TOKEN, 2, now=T0)
    assert session_member(TOKEN, value, T0) == 2
    forged = value.replace("v2.2.", "v2.1.", 1)
    assert session_member(TOKEN, forged, T0) is None


def test_a_cookie_from_before_members_is_still_the_owners():
    """v1 was only ever issued to the Owner; reading it as theirs keeps that
    phone signed in across the upgrade."""
    import hashlib
    import hmac as hmac_

    issued = int(T0.timestamp())
    mac = hmac_.new(TOKEN.encode(), f"session:{issued}".encode(), hashlib.sha256).hexdigest()
    assert session_member(TOKEN, f"v1.{issued}.{mac}", T0) == 1


def test_rotating_the_token_revokes_every_session():
    assert not session_valid("a-new-token", session_cookie(TOKEN, now=T0), T0)


# ── Through the daemon ──────────────────────────────────────────────────────
def test_the_code_buys_a_cookie_and_the_reload_works(tmp_path):
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        code = c.app.state.board_codes.issue()
        r = c.get(f"/board?code={code}", follow_redirects=False)

        assert r.status_code == 303 and r.headers["location"] == "/board"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=lax" in cookie

        # The reload: no query, no header, only the cookie the browser keeps.
        assert c.get("/board").status_code == 200
        # And the data calls the board makes afterwards.
        assert c.get("/notes").status_code == 200


def test_a_used_code_is_refused_with_how_to_get_another(tmp_path):
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        code = c.app.state.board_codes.issue()
        c.get(f"/board?code={code}", follow_redirects=False)
        c.cookies.clear()
        r = c.get(f"/board?code={code}", follow_redirects=False)
    assert r.status_code == 401 and "/board" in r.json()["error"]


def test_with_no_cookie_the_board_still_401s(tmp_path):
    """The old guarantee stands: the fix adds a door, it does not open the house."""
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        assert c.get("/board").status_code == 401


# ── The link the bot sends ──────────────────────────────────────────────────
class FakeApp:
    class state:  # noqa: N801 - mirrors Starlette's `app.state`
        board_codes = BoardCodes()


def test_on_loopback_there_is_no_link_to_send():
    assert _board_link(FakeApp, Config()) is None


def test_the_link_uses_the_public_url_and_carries_a_fresh_code():
    cfg = Config(host="0.0.0.0", token=TOKEN, public_url="http://casa.lan:7777/")
    first, second = _board_link(FakeApp, cfg), _board_link(FakeApp, cfg)
    assert first.startswith("http://casa.lan:7777/board?code=")
    assert first != second, "every link is a new one-time code"


def test_without_a_public_url_it_guesses_the_lan_address(monkeypatch):
    monkeypatch.setattr(board_access, "lan_address", lambda: "192.168.1.9")
    link = _board_link(FakeApp, Config(host="0.0.0.0", token=TOKEN))
    assert link.startswith("http://192.168.1.9:7777/board?code=")


def bot_with(tmp_path, link):
    return Bot(db.connect(tmp_path / "t.db"), FakeChannel(),
               capture=lambda t, owner_id=1: None, owner_username="dono", board_link=link)


def test_pairing_sends_the_board_link(tmp_path):
    b = bot_with(tmp_path, lambda member_id: "http://x/board?code=abc")
    asyncio.run(b.handle(inbound("/start")))
    assert any("http://x/board?code=abc" in m for m in b.channel.sent)


def test_board_command_sends_a_link_or_says_why_not(tmp_path):
    b = bot_with(tmp_path, lambda member_id: "http://x/board?code=abc")
    asyncio.run(b.handle(inbound("/start")))
    asyncio.run(b.handle(inbound("/board")))
    assert "code=abc" in b.channel.sent[-1]

    b.board_link = lambda member_id: None
    asyncio.run(b.handle(inbound("/board")))
    assert "TA_HOST" in b.channel.sent[-1]
