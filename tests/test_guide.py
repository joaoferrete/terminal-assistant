"""The guide stays true (F10, D48): the agent answers "how do I…" from it, so a
Tool, a command or a setting missing from it is a question the bot answers wrong.

This is the enforcement of the AGENTS.md rule: a new user-visible capability
enters `docs/guide.md` in the same commit, or CI fails here.
"""
import asyncio
import re
import tomllib
from pathlib import Path

import pytest

from ta import builtin_tools, guide  # noqa: F401 - registers the Tools
from ta.tools import registered

ROOT = Path(__file__).resolve().parents[1]
GUIDE = (ROOT / "docs" / "guide.md").read_text()
CONFIG_DOC = (ROOT / "docs" / "configuration.md").read_text()


def shipped_tools():
    # Every module that ships Tools: the built-ins and each `agent_*` module.
    return sorted(n for n, t in registered().items()
                  if t.fn.__module__ == "ta.builtin_tools"
                  or t.fn.__module__.startswith("ta.agent_"))


@pytest.mark.parametrize("name", shipped_tools())
def test_every_tool_is_in_the_guide(name):
    assert f"`{name}`" in GUIDE, f"add `{name}` to docs/guide.md (AGENTS.md rule)"


def test_every_command_in_the_menu_is_in_the_guide():
    from ta.bot import Bot

    menu = Bot.menu(None)
    for command, _ in [("start", ""), *menu]:
        assert f"`/{command}`" in GUIDE, f"add /{command} to the guide's Commands"


def _toml_blocks(text):
    return [tomllib.loads(b) for b in re.findall(r"```toml\n(.*?)```", text, flags=re.S)]


def _tables(doc, prefix=""):
    for key, value in doc.items():
        if isinstance(value, dict):
            yield prefix + key
            # One level down names a real table (llm.tiers); deeper is a user's own
            # name (members.ana_silva), which the config docs show with their own.
            if not prefix:
                for sub, v in value.items():
                    if isinstance(v, dict) and key in ("llm",):
                        yield f"{key}.{sub}"


def test_every_setting_the_guide_shows_exists():
    known = {t for d in _toml_blocks(CONFIG_DOC) for t in _tables(d)}
    for block in _toml_blocks(GUIDE):
        for table in _tables(block):
            assert table in known, f"the guide shows [{table}], which configuration.md does not"


def test_every_environment_variable_the_guide_names_exists():
    for var in set(re.findall(r"`((?:TA|GOOGLE|GEMINI|DEEPSEEK|HA|TELEGRAM)_[A-Z_]+)`", GUIDE)):
        assert f"`{var}`" in CONFIG_DOC, f"the guide names {var}, unknown to configuration.md"


def test_help_finds_a_topic_by_its_words():
    title, body = guide.find("como conecto o email")
    assert title == "Email" and "/conectar_email" in body
    assert guide.find("members permissions")[0] == "Members and permissions"


def test_the_help_tool_lists_topics_when_asked_nothing():
    from ta.tools import Turn

    turn = Turn(member_id=1, conversation_id="c", in_group=False, permissions=None)
    ctx = builtin_tools.ToolContext(conn=None, turn=turn, channel="t", services={})
    out = asyncio.run(registered()["help"].fn(ctx, topic=""))
    assert "Calendar" in out.text and "Email" in out.text
