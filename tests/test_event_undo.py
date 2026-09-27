"""T5.2, decided 2026-09-26: the review creates the event with no confirmation,
says so in the writer's private chat, and [Undo] deletes it."""
import asyncio
from datetime import datetime

import httpx
from test_reminders_chat import SendingChannel, bot, press  # noqa: F401 - the fixture

from ta import store
from ta.bot import AgentDeps
from ta.grants import OWNER_PERMISSIONS
from ta.sensors.google_calendar import Connector, GoogleCalendar, TokenStore
from ta.tools import registered


def test_the_event_is_announced_to_its_writer_and_undo_deletes_it(bot):  # noqa: F811
    deleted = []

    async def delete_event(member_id, note_id):
        deleted.append((member_id, note_id))
        return True

    bot.agent = AgentDeps(llm=None, registry=registered, permissions=lambda m: OWNER_PERMISSIONS,
                          services={"delete_event": delete_event})
    note = store.add_note(bot.conn, "dentista sexta 14h", owner_id=2)
    assert asyncio.run(bot.event_created(note, "Dentista", datetime(2026, 10, 2, 14, 0)))
    assert bot.channel.to[-1] == "2002" and "Dentista · 02/10 14:00" in bot.channel.sent[-1]
    [button] = bot.channel.buttons[-1]
    asyncio.run(bot.handle(press(button.data, sender="1001")))
    assert deleted == [], "only the writer can undo"
    asyncio.run(bot.handle(press(button.data, sender="2002")))
    assert deleted == [(2, note.id)]


def test_google_deletes_the_one_event(tmp_path):
    seen = []

    def google(request):
        seen.append((request.method, request.url.path))
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        return httpx.Response(204)

    t = httpx.MockTransport(google)
    tokens = TokenStore(tmp_path / "g")
    tokens.save(1, "eu@gmail.com", "rt")
    cal = GoogleCalendar(1, Connector("cid", "s", transport=t), tokens, transport=t)
    cal.delete_event("eu@gmail.com|ta123@group", "ev1")
    assert seen[-1] == ("DELETE", "/calendar/v3/calendars/ta123@group/events/ev1")
