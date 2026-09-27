"""The Telegram Channel and the Owner-only bot (F1).

Nothing here reaches Telegram: the Channel answers through `httpx.MockTransport`,
and the bot talks to a fake Channel that records what it would have sent.
"""
import asyncio
import json
import logging
import sqlite3
import types

import httpx
import pytest

from ta import db
from ta.bot import Bot, role_of
from ta.channel import Inbound
from ta.channel.telegram import TelegramChannel

OWNER_ID = "1001"


def inbound(text="comprar pão", *, sender_id=OWNER_ID, username="dono", private=True,
            unsupported=False):
    return Inbound(channel="telegram", conversation_id="c1", message_id="7",
                   sender_id=sender_id, sender_username=username, private=private,
                   text=text, unsupported=unsupported)


class FakeChannel:
    name = "telegram"
    configured = True

    def __init__(self):
        self.sent: list[str] = []
        self.buttons: list = []       # the buttons of each sent message, in order
        self.acks: list = []

    async def reply(self, to, text, buttons=None, *, quiet=False):
        self.sent.append(text)
        self.buttons.append(buttons or [])
        self.quiet = quiet
        return f"bot{len(self.sent)}"

    async def answered(self, to, text=None):
        self.acks.append(text)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


@pytest.fixture
def bot(conn):
    captured = []

    def capture(text, owner_id=1):
        captured.append(text)
        return types.SimpleNamespace(id=len(captured), due=None)

    b = Bot(conn, FakeChannel(), capture=capture, owner_username="dono")
    b.captured = captured
    return b


def run(bot, msg):
    asyncio.run(bot.handle(msg))


# ── Pairing and identity (D21) ──────────────────────────────────────────────
def test_the_owner_pairs_on_first_contact_and_the_text_is_still_captured(bot, conn):
    run(bot, inbound("ligar pro dentista"))
    assert role_of(conn, "telegram", OWNER_ID) == "owner"
    assert bot.captured == ["ligar pro dentista"]
    assert len(bot.channel.sent) == 2   # "I know you now", then "noted"


def test_the_username_matches_regardless_of_case(bot, conn):
    run(bot, inbound(username="DoNo"))
    assert role_of(conn, "telegram", OWNER_ID) == "owner"


def test_after_pairing_the_id_counts_even_if_the_username_changes(bot):
    run(bot, inbound("primeira"))
    run(bot, inbound("segunda", username="outro_nome"))
    assert bot.captured == ["primeira", "segunda"]


def test_a_stranger_who_takes_the_owners_username_is_ignored(bot):
    """Invariant 4: the takeover that binding to the id exists to refuse."""
    run(bot, inbound("sou eu"))
    sent_before = list(bot.channel.sent)
    run(bot, inbound("sou eu, juro", sender_id="6666", username="dono"))
    assert bot.captured == ["sou eu"]
    assert bot.channel.sent == sent_before, "a stranger gets silence, not an answer"


def test_a_stranger_is_answered_with_silence(bot, conn):
    run(bot, inbound(sender_id="42", username="alguem"))
    assert bot.captured == [] and bot.channel.sent == []
    assert role_of(conn, "telegram", "42") is None


def test_with_no_owner_configured_nobody_pairs(conn):
    b = Bot(conn, FakeChannel(), capture=lambda t, owner_id=1: None, owner_username=None)
    asyncio.run(b.handle(inbound()))
    assert b.channel.sent == []


def test_only_one_owner_per_channel_even_at_the_database(conn):
    def capture(t, owner_id=1):
        return types.SimpleNamespace(id=1, due=None)

    run_bot = Bot(conn, FakeChannel(), capture=capture, owner_username="dono")
    asyncio.run(run_bot.handle(inbound()))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO channel_identities (channel, external_id, username, role, paired_at)"
            " VALUES ('telegram', '9', 'x', 'owner', 'now')"
        )


# ── What happens to a message (invariant 1) ─────────────────────────────────
def test_group_messages_are_not_handled_yet(bot):
    run(bot, inbound(private=False))
    assert bot.captured == [] and bot.channel.sent == []


def test_start_is_a_greeting_not_a_note(bot):
    run(bot, inbound("/start"))
    assert bot.captured == []
    run(bot, inbound("/start"))
    assert bot.captured == [] and len(bot.channel.sent) == 2   # paired, then hello


def test_an_unknown_command_is_answered_not_captured(bot):
    run(bot, inbound("oi"))
    run(bot, inbound("/qualquer coisa"))
    assert bot.captured == ["oi"]
    assert len(bot.channel.sent) == 3


def test_what_cannot_be_read_yet_is_answered_not_dropped(bot):
    run(bot, inbound("", unsupported=True))
    assert bot.captured == []
    assert bot.channel.sent[-1]   # it says so


def test_the_reply_mentions_the_deadline_when_there_is_one(conn):
    ch = FakeChannel()
    b = Bot(conn, ch, capture=lambda t, owner_id=1: types.SimpleNamespace(id=5, due="2026-10-02"),
            owner_username="dono")
    asyncio.run(b.handle(inbound("pagar boleto @sexta")))
    assert "2026-10-02" in ch.sent[-1] and "5" in ch.sent[-1]


# ── The Telegram Channel ────────────────────────────────────────────────────
def update(uid, text="oi", *, chat_type="private", is_bot=False, sender=1001, **extra):
    msg = {"message_id": uid * 10, "from": {"id": sender, "username": "dono", "is_bot": is_bot},
           "chat": {"id": 555, "type": chat_type}, **extra}
    if text is not None:
        msg["text"] = text
    return {"update_id": uid, "message": msg}


def test_an_update_becomes_an_inbound():
    m = TelegramChannel.parse(update(3, "comprar leite"))
    assert (m.sender_id, m.conversation_id, m.message_id, m.text, m.private) == (
        "1001", "555", "30", "comprar leite", True)


def test_a_voice_note_carries_its_file_and_length():
    m = TelegramChannel.parse(update(3, None, voice={"file_id": "abc", "duration": 12}))
    assert (m.voice_file_id, m.voice_seconds, m.unsupported) == ("abc", 12, False)


def test_a_photo_with_no_caption_is_unsupported_not_invisible():
    m = TelegramChannel.parse(update(3, None, photo=[{"file_id": "p"}]))
    assert m.unsupported and m.voice_file_id is None


def test_other_bots_and_non_messages_are_skipped():
    assert TelegramChannel.parse(update(1, is_bot=True)) is None
    assert TelegramChannel.parse({"update_id": 1, "edited_message": {}}) is None


# The bot's own account, which the loop asks for before its first poll.
ME = {"ok": True, "result": {"id": 4242, "username": "TA_bot", "is_bot": True}}


def telegram(responses, calls):
    """A Telegram that answers `responses` in order, then cancels the loop."""
    pending = list(responses)

    def handler(request: httpx.Request):
        calls.append((request.url.path, json.loads(request.content)))
        if not pending:
            raise asyncio.CancelledError
        r = pending.pop(0)
        return r if isinstance(r, httpx.Response) else httpx.Response(200, json=r)

    async def no_sleep(_):
        return None

    return TelegramChannel("123:SECRET", transport=httpx.MockTransport(handler), sleep=no_sleep)


def test_the_loop_delivers_and_advances_the_offset():
    calls, got = [], []
    ch = telegram([ME, {"ok": True, "result": [update(5), update(6)]}], calls)

    async def handler(m):
        got.append(m.text)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ch.run(handler))
    assert got == ["oi", "oi"]
    assert calls[2][1]["offset"] == 7, "the next poll must not redeliver 5 and 6"


def test_a_handler_that_crashes_does_not_stop_the_loop_or_repeat_the_message():
    calls, got = [], []
    ch = telegram([ME, {"ok": True, "result": [update(5), update(6)]}], calls)

    async def handler(m):
        got.append(m.message_id)
        if m.message_id == "50":
            raise RuntimeError("boom")

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ch.run(handler))
    assert got == ["50", "60"]
    assert calls[2][1]["offset"] == 7


def test_an_outage_is_retried_and_the_token_never_reaches_the_log(caplog):
    # INFO, not the default WARNING: httpx logs every request at INFO with the
    # full URL, and the first version of this test could not see that leak.
    caplog.set_level(logging.INFO)
    calls = []
    ch = telegram([httpx.Response(502, text="bad gateway"),
                   {"ok": False, "description": "Unauthorized"},
                   {"ok": True, "result": []}], calls)

    async def handler(m):
        pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ch.run(handler))
    assert len(calls) == 4
    assert "SECRET" not in caplog.text, "the bot token is in the URL; it must not be logged"
    assert "Unauthorized" in caplog.text


def test_a_reply_quotes_the_message_it_answers():
    calls = []
    ch = telegram([{"ok": True, "result": {}}], calls)
    asyncio.run(ch.reply(inbound(), "Anotado"))
    path, body = calls[0]
    assert path.endswith("/sendMessage")
    assert body["chat_id"] == "c1" and body["reply_parameters"]["message_id"] == 7


def test_the_daemon_capture_path_is_shared_with_the_bot(tmp_path):
    """A Note from the phone is born exactly like one from `ta note`."""
    from starlette.testclient import TestClient
    from test_daemon import FakeCalendar, FakeLighter

    from ta.config import Config
    from ta.daemon import create_app

    ch = FakeChannel()
    app = create_app(Config(auto_review=False), db_path=tmp_path / "t.db",
                     rules_dir=tmp_path / "none", calendar=FakeCalendar(),
                     lighter=FakeLighter(), channel=ch, background=False)
    with TestClient(app) as c:
        c.app.state.bot.owner_username = "dono"
        c.portal.call(c.app.state.bot.handle, inbound("revisar PR @2026-10-01 !alta"))
        notes = c.get("/notes").json()["notes"]
    note = next(n for n in notes if n["text"].startswith("revisar PR"))
    assert note["due"] == "2026-10-01" and note["priority"] == "high"
    assert "2026-10-01" in ch.sent[-1]


def test_a_pressed_button_becomes_an_inbound_with_its_data():
    m = TelegramChannel.parse({"update_id": 9, "callback_query": {
        "id": "cb7", "from": {"id": 1001, "username": "dono"}, "data": "ok:3",
        "message": {"message_id": 44, "chat": {"id": 555, "type": "private"}}}})
    assert (m.callback, m.callback_id, m.message_id, m.sender_id) == ("ok:3", "cb7", "44", "1001")


def test_a_reply_to_a_message_carries_which_one():
    m = TelegramChannel.parse(update(3, "o que você fez aqui?",
                                     reply_to_message={"message_id": 12}))
    assert m.reply_to_message_id == "12"


def test_buttons_go_as_an_inline_keyboard_and_the_sent_id_comes_back():
    from ta.channel import Button

    calls = []
    ch = telegram([{"ok": True, "result": {"message_id": 99}}], calls)
    sent = asyncio.run(ch.reply(inbound(), "Confirma?", [Button("Sim", "ok:1")]))
    assert sent == "99"
    assert calls[0][1]["reply_markup"] == {
        "inline_keyboard": [[{"text": "Sim", "callback_data": "ok:1"}]]}


def test_answering_a_button_also_removes_the_keyboard():
    calls = []
    ch = telegram([{"ok": True, "result": True}, {"ok": True, "result": True}], calls)
    press = TelegramChannel.parse({"update_id": 1, "callback_query": {
        "id": "cb1", "from": {"id": 1}, "data": "x",
        "message": {"message_id": 5, "chat": {"id": 7, "type": "private"}}}})
    asyncio.run(ch.answered(press, "Feito."))
    assert [p for p, _ in calls] == ["/bot123:SECRET/answerCallbackQuery",
                                     "/bot123:SECRET/editMessageReplyMarkup"]


def test_a_group_message_that_mentions_the_bot_is_addressed_to_it():
    me = {"id": 4242, "username": "TA_bot"}
    m = TelegramChannel.parse(update(3, "@ta_bot o que tem na lista?", chat_type="group"), me)
    assert m.mentioned and not m.private
    plain = TelegramChannel.parse(update(4, "acabou o café", chat_type="group"), me)
    assert not plain.mentioned
    replying = TelegramChannel.parse(update(5, "e agora?", chat_type="group",
                                            reply_to_message={"message_id": 1,
                                                              "from": {"id": 4242}}), me)
    assert replying.mentioned, "replying to the bot addresses it too"
