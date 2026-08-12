"""The rule that motivated the project, actually exercised.

`ta rules check` only proves it **loads**. Ever since the hour started deciding
what the rule *does* — rather than *whether* it runs — `16:00` governs three
things at once: the ringlight on the way in, the light on the way out, and the
wording of the notification. None of them were covered.

The doubles exist because the rule talks to the house and to the GNOME
extension: without them, the test would switch on a real lamp and write to the
user's `gsettings`.
"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

from ta.engine import Context, Trigger

# The example Rules live in `examples/rules/`, and are loaded by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "rules"))
import meeting  # noqa: E402


class FakeHome:
    def __init__(self):
        self.levels = []

    async def light(self, entity, level=None):
        self.levels.append(level)


class FakeLighter:
    def __init__(self):
        self.actions = []

    async def apply_profile(self, name, *, enable=True):
        self.actions.append(("profile", name))

    async def enable(self, on=True):
        self.actions.append(("enable", on))


class FakeCalendar:
    def __init__(self, title):
        self.title = title

    # `now`, and also `agora`, because both are the real adapter's API and a rule
    # on somebody's disk may call either.
    async def now(self):
        return {"summary": self.title} if self.title else None

    agora = now


class FakeNotify:
    def __init__(self):
        self.sent = []   # (title, body) — the body matters: it must not lie

    async def send(self, title, body, **k):
        self.sent.append((title, body))


async def _run(fn, *, hour, title, active=True):
    home, lighter, notify = FakeHome(), FakeLighter(), FakeNotify()
    h, m = (int(x) for x in hour.split(":"))
    await fn(
        Context(
            trigger=Trigger("mic", active),
            now=datetime(2026, 8, 11, h, m),
            home=home, lighter=lighter, notify=notify,
            calendar=FakeCalendar(title), note=None, extra={},
        )
    )
    return home, lighter, notify


# ── Going in: the light is unconditional, the ringlight is not ──────────────
@pytest.mark.parametrize("hour", ["09:00", "14:00", "17:00", "23:30"])
async def test_joining_a_call_turns_the_light_on_at_any_hour(hour):
    """The original request tied this to 16:00. On a call the light always helps."""
    home, _, _ = await _run(meeting.meeting, hour=hour, title="Planning")
    assert home.levels == [100]


@pytest.mark.parametrize("hour", ["16:00", "17:00", "23:30"])
async def test_the_ringlight_joins_in_once_it_gets_dark(hour):
    """`16:00` on the dot already counts as dark: `after` is greater **or equal**."""
    _, lighter, _ = await _run(meeting.meeting, hour=hour, title="Planning")
    assert ("profile", meeting.RINGLIGHT_PROFILE) in lighter.actions


@pytest.mark.parametrize("hour", ["09:00", "14:00", "15:59"])
async def test_by_day_only_the_light_comes_on(hour):
    """Daylight already covers it; a lit rim in the morning is production nobody asked for."""
    home, lighter, _ = await _run(meeting.meeting, hour=hour, title="Planning")
    assert home.levels == [100]
    assert lighter.actions == []


@pytest.mark.parametrize(
    ("hour", "expected"), [("09:00", "light on"), ("17:00", "light and ringlight on")]
)
async def test_the_notice_never_announces_a_ringlight_that_did_not_turn_on(hour, expected):
    """A notification that lies once stops being read the other times."""
    _, _, notify = await _run(meeting.meeting, hour=hour, title="Planning")
    [(_, body)] = notify.sent
    assert body.endswith(expected)


@pytest.mark.parametrize("hour", ["14:00", "17:00"])
async def test_a_one_on_one_triggers_nothing(hour):
    home, lighter, notify = await _run(meeting.meeting, hour=hour, title="1:1 com Ana")
    assert home.levels == []
    assert lighter.actions == []
    assert notify.sent == []


# ── Going out: here the hour is in charge ──────────────────────────────────
async def test_leaving_before_1600_returns_the_light_to_the_living_level():
    home, lighter, _ = await _run(
        meeting.meeting_end, hour="15:59", title="Planning", active=False
    )
    assert home.levels == [meeting.LIVING_LEVEL]
    assert ("enable", False) in lighter.actions


@pytest.mark.parametrize("hour", ["16:00", "16:30", "22:00"])
async def test_leaving_after_1600_leaves_the_light_where_it_is(hour):
    """`16:00` on the dot already counts as late: `before` is strictly less than.

    `16:30` is the meeting that ran across 16:00 — what decides is the hour you
    leave, not the hour you joined.
    """
    home, lighter, _ = await _run(
        meeting.meeting_end, hour=hour, title="Planning", active=False
    )
    assert home.levels == []                  # the light was not touched
    assert ("enable", False) in lighter.actions   # but the ringlight went off


@pytest.mark.parametrize("hour", ["09:00", "15:59"])
async def test_the_ringlight_is_switched_off_even_if_it_started_by_day(hour):
    """`enable(False)` is unconditional, and that is deliberate.

    Repeating the entry condition here would leave the rim light on after a
    meeting that started at 15:50 and ended at 16:10 — it turned on (no), it did
    not turn off (yes). And turning off what is already off costs nothing.
    """
    _, lighter, _ = await _run(
        meeting.meeting_end, hour=hour, title="Planning", active=False
    )
    assert ("enable", False) in lighter.actions


async def test_the_end_of_a_one_on_one_undoes_nothing_because_nothing_was_done():
    home, lighter, _ = await _run(
        meeting.meeting_end, hour="14:00", title="1:1 com Ana", active=False
    )
    assert home.levels == []
    assert lighter.actions == []


async def test_with_no_calendar_event_the_ending_takes_the_ordinary_path():
    """Leaving after the scheduled end makes `calendar.now()` return nothing.

    The rule adjusts anyway: erring on the side of adjusting is the cheap side.
    """
    home, _, _ = await _run(
        meeting.meeting_end, hour="14:00", title="", active=False
    )
    assert home.levels == [meeting.LIVING_LEVEL]
