"""The Satellite catalogue (F10, D43, D56): the laptop decides what exists and
what needs a button; there is no shell and no free argument."""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from ta import builtin_tools, db, satellite_actions
from ta.grants import OWNER_PERMISSIONS
from ta.satellite import Hub
from ta.tools import NeedsConfirmation, Turn, registered
from ta.tools import run as run_tool


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_run(args, **kw):
        seen.append(args)
        out = ("(['org.mpris.MediaPlayer2.spotify'],)"
               if any("ListNames" in a for a in args) else "")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: seen.append(args))
    return seen


def test_actions_are_argument_lists_never_a_shell(calls):
    satellite_actions.handle({"op": "volume", "value": "30"}, {})
    satellite_actions.handle({"op": "media", "value": "pause"}, {})
    satellite_actions.handle({"op": "lock"}, {})
    assert ["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "30%"] in calls
    assert any("org.mpris.MediaPlayer2.spotify" in c for c in calls)
    assert ["loginctl", "lock-session"] in calls
    assert all(isinstance(c, list) for c in calls)


@pytest.mark.parametrize(("op", "value"), [
    ("volume", "30; rm -rf ~"), ("open", "file:///etc/passwd"), ("open", "../../bin/sh"),
    ("rm", "-rf"),
])
def test_nothing_free_form_gets_through(calls, op, value):
    out = satellite_actions.handle({"op": op, "value": value}, {})
    assert calls == [] or all("rm" not in " ".join(c) for c in calls)
    assert "error" in out or "no installed app" in out.get("text", "") or \
        "volume is" in out.get("text", "")


def test_the_laptop_refuses_what_needs_a_button_when_it_is_not_confirmed(calls):
    scripts = {"backup": {"run": "/bin/true"}, "luz": {"run": "/bin/true", "safe": True}}
    assert "confirmation" in satellite_actions.handle({"op": "suspend"}, scripts)["error"]
    assert "confirmation" in satellite_actions.handle({"op": "screenshot"}, scripts)["error"]
    assert "confirmation" in satellite_actions.handle(
        {"op": "script", "value": "backup"}, scripts)["error"]
    assert "finished" in satellite_actions.handle(
        {"op": "script", "value": "luz"}, scripts)["text"], "safe runs without the button"
    assert "no script" in satellite_actions.handle(
        {"op": "script", "value": "outro", "confirmed": True}, scripts)["text"]


class LaptopHub(Hub):
    def __init__(self, machines, answer):
        super().__init__()
        self._list, self._answer, self.asked = machines, answer, []

    def connected(self, member_id):
        return bool(self._list)

    def machines(self, member_id=None):
        return self._list

    async def ask(self, member_id, request, timeout=30.0, machine=None):
        self.asked.append((request, machine))
        return self._answer


@pytest.fixture
def ctx(tmp_path):
    def build(machines=({"machine": "notebook", "online": True},), answer=None):
        turn = Turn(member_id=1, conversation_id="c", in_group=False, permissions=OWNER_PERMISSIONS)
        return builtin_tools.ToolContext(
            conn=db.connect(tmp_path / "t.db"), turn=turn, channel="t",
            services={"hub": LaptopHub(list(machines), answer or {"text": "ok"})})
    return build


def run(c, tool_name, **args):
    return asyncio.run(run_tool(registered()[tool_name], c.turn, c, args))


def test_screenshot_and_suspend_always_ask_first(ctx):
    with pytest.raises(NeedsConfirmation):
        run(ctx(), "computer_confirmed", action="suspend")


def test_with_two_computers_on_the_bot_asks_which(ctx):
    two = [{"machine": "notebook", "online": True}, {"machine": "desktop", "online": True}]
    c = ctx(machines=two)
    assert "which computer" in run(c, "computer_act", action="lock").text
    run(c, "computer_act", action="lock", machine="desktop")
    assert c.services["hub"].asked[-1][1] == "desktop"
