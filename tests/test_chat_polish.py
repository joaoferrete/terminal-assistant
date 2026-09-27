"""How the bot talks on Telegram (F10): formatting that renders, and a word
before a slow Tool."""
import asyncio
import json

import httpx
import pytest
from test_agent import answer, call, make, say  # noqa: F401 - the fixture

from ta.channel.telegram import TelegramChannel, to_html
from ta.tools import ToolResult, tool


@pytest.mark.parametrize(("md", "html"), [
    ("**Feito!** luz *azul*", "<b>Feito!</b> luz <i>azul</i>"),
    ("a < b & c", "a &lt; b &amp; c"),
    ("[site](https://x.com/?a=1&b=2)", '<a href="https://x.com/?a=1&amp;b=2">site</a>'),
    ("- um\n- dois", "• um\n• dois"),
    ("snake_case e 2*3*4", "snake_case e 2*3*4"),
    ("`ta note`", "<code>ta note</code>"),
])
def test_markdown_becomes_telegram_html(md, html):
    assert to_html(md) == html


def test_a_note_cannot_open_a_tag():
    assert "<script>" not in to_html("<script>alert(1)</script>")


def test_if_telegram_refuses_the_formatting_the_text_still_goes():
    sent = []

    def api(request):
        body = json.loads(request.content)
        sent.append(body)
        if body.get("parse_mode"):
            return httpx.Response(400, json={"ok": False,
                                             "description": "Bad Request: can't parse entities"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    ch = TelegramChannel("1:x", transport=httpx.MockTransport(api))
    assert asyncio.run(ch.send("c", "**oi**")) == "5"
    assert sent[-1] == {"chat_id": "c", "text": "**oi**"}


@tool(description="slow (test)", name="test_slow", slow=True)
async def _slow(ctx):
    return ToolResult(text="ok")


def test_a_slow_tool_is_announced_once_by_the_code(make):  # noqa: F811
    b = make(call("test_slow"), call("test_slow", '{"x": 1}'), call("test_slow"),
             answer("Pronto."))
    say(b, "procura aí")
    progress = [m for m in b.channel.sent if m.startswith("⏳")]
    assert progress == ["⏳ Um instante, já volto…"], "once, from the catalogue"
    assert len(b.llm.prompts) == 4, "no model call for the progress message"
