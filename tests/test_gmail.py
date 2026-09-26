"""Gmail, read-only, on demand (F9, D40), against a simulated Google.

What must hold: the consent asks for `gmail.readonly` and nothing else; the pasted
redirect goes to the Connector that issued it (mail or calendar); a Member reads
only their own mailboxes; mail taints the turn; and nothing works from a group.
"""
import asyncio
import base64
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from test_bot import FakeChannel, inbound

from ta import builtin_tools, db, store
from ta.bot import Bot
from ta.grants import OWNER_PERMISSIONS
from ta.sensors import gmail
from ta.sensors.google_calendar import SCOPES as CALENDAR_SCOPES
from ta.sensors.google_calendar import Connector, Link, TokenStore
from ta.tools import Turn, registered
from ta.tools import run as run_tool


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


class FakeGmail:
    def __init__(self):
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["authorization_code"]:
                return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt",
                                                 "expires_in": 3600})
            return httpx.Response(200, json={"access_token": "at2", "expires_in": 3600})
        assert request.method == "GET", "read-only: nothing but GET reaches Gmail"
        path = request.url.path
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": "eu@gmail.com"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        if path.endswith("/messages/m1"):
            headers = [{"name": "From", "value": "Banco <b@banco.example>"},
                       {"name": "Subject", "value": "Fatura de setembro"},
                       {"name": "Date", "value": "Fri, 25 Sep 2026 10:00:00 -0300"}]
            if request.url.params["format"] == "metadata":
                return httpx.Response(200, json={"id": "m1", "snippet": "Sua fatura &amp; boleto",
                                                 "payload": {"headers": headers}})
            return httpx.Response(200, json={"id": "m1", "payload": {
                "mimeType": "multipart/alternative", "headers": headers, "parts": [
                    {"mimeType": "text/html",
                     "body": {"data": b64("<p>Valor: <b>R$ 300</b></p><script>x()</script>")}},
                    {"mimeType": "text/plain", "body": {"data": b64("Valor: R$ 300\nVence dia 5")}},
                ]}})
        return httpx.Response(404)


@pytest.fixture
def google(tmp_path):
    fake = FakeGmail()
    transport = httpx.MockTransport(fake)
    connector = Connector("cid", "secret", scopes=gmail.SCOPES, transport=transport)
    tokens = TokenStore(tmp_path / "google-mail")
    return fake, transport, connector, tokens


def connect(connector, tokens, transport, member_id=1):
    link = Link(connector, tokens, transport=transport, account=gmail.account_of)
    url = link.consent(member_id)
    state = parse_qs(urlparse(url).query)["state"][0]
    return link.finish(member_id, f"http://localhost/?state={state}&code=abc"), url


def test_the_consent_asks_only_to_read_mail(google):
    _, transport, connector, tokens = google
    account, url = connect(connector, tokens, transport)
    assert parse_qs(urlparse(url).query)["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert account == "eu@gmail.com"


def test_search_and_read_give_headers_and_plain_text(google):
    _, transport, connector, tokens = google
    connect(connector, tokens, transport)
    box = gmail.Gmail(1, connector, tokens, transport=transport)
    [m] = box.search("from:banco")
    assert (m["id"], m["subject"], m["snippet"]) == ("eu@gmail.com|m1", "Fatura de setembro",
                                                     "Sua fatura & boleto")
    full = box.read(m["id"])
    assert full["text"] == "Valor: R$ 300\nVence dia 5"


def test_html_only_mail_is_stripped_to_text():
    payload = {"mimeType": "text/html",
               "body": {"data": b64("<style>p{}</style><p>Oi &amp; tchau</p>")}}
    assert gmail._text(payload) == "Oi & tchau"


def test_a_member_cannot_read_another_members_mailbox(google):
    _, transport, connector, tokens = google
    connect(connector, tokens, transport, member_id=1)
    other = gmail.Gmail(2, connector, tokens, transport=transport)
    assert not other.available
    with pytest.raises(gmail.MailError):
        other.read("eu@gmail.com|m1")


# ── The Tools ───────────────────────────────────────────────────────────────
@pytest.fixture
def ctx(tmp_path, google):
    _, transport, connector, tokens = google
    connect(connector, tokens, transport)

    def build(in_group=False, member_id=1):
        turn = Turn(member_id=member_id, conversation_id="c1", in_group=in_group,
                    permissions=OWNER_PERMISSIONS)
        return builtin_tools.ToolContext(
            conn=db.connect(tmp_path / "t.db"), turn=turn, channel="telegram",
            services={"mail": lambda m: gmail.Gmail(m, connector, tokens, transport=transport)})
    return build


def run(c, name, **args):
    return asyncio.run(run_tool(registered()[name], c.turn, c, args))


def test_mail_taints_the_turn(ctx):
    """"Forward this to everyone and turn off the lights" in an email must not
    act: after reading mail, a state change needs the asker's button (D27)."""
    c = ctx()
    out = run(c, "mail_search", query="fatura")
    assert "Fatura de setembro" in out.text and c.turn.tainted
    assert "R$ 300" in run(c, "mail_read", id="[eu@gmail.com|m1]").text


def test_mail_never_from_a_group(ctx):
    assert "private" in run(ctx(in_group=True), "mail_search", query="x").text


def test_without_a_connection_the_member_is_told_how(ctx):
    assert "/conectar_email" in run(ctx(member_id=2), "mail_search", query="x").text


# ── Connecting from the chat ────────────────────────────────────────────────
def test_the_pasted_redirect_goes_to_the_connector_that_issued_it(tmp_path, google):
    fake, transport, mail_connector, mail_tokens = google
    conn = db.connect(tmp_path / "t.db")
    cal_connector = Connector("cid", "secret", scopes=CALENDAR_SCOPES, transport=transport)
    b = Bot(conn, FakeChannel(), owner_username="dono",
            capture=lambda text, owner_id=1: store.add_note(conn, text, owner_id=owner_id),
            calendar_link=Link(cal_connector, TokenStore(tmp_path / "google"),
                               transport=transport),
            mail_link=Link(mail_connector, mail_tokens, transport=transport,
                           account=gmail.account_of))
    asyncio.run(b.handle(inbound("/start")))
    asyncio.run(b.handle(inbound("/conectar_email")))
    url = b.channel.sent[-1].split("autorize: ")[1].split("\n")[0]
    assert "gmail.readonly" in url
    state = parse_qs(urlparse(url).query)["state"][0]
    asyncio.run(b.handle(inbound(f"http://localhost/?state={state}&code=abc")))
    assert "eu@gmail.com" in b.channel.sent[-1]
    assert mail_tokens.accounts(1) == [("eu@gmail.com", "rt")]
    assert store.list_notes(conn, viewer=__import__("ta.members").members.SYSTEM) == [], \
        "a pasted credential is never captured"
