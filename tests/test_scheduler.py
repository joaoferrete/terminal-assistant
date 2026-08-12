from datetime import datetime, timedelta

from ta.i18n import t
from ta.scheduler import Scheduler, lateness_label, lateness_of


def make():
    hours, reminders = [], []

    async def on_time(minute, now):
        hours.append(minute)

    async def on_reminder(now):
        reminders.append(now)

    return Scheduler(on_time, on_reminder), hours, reminders


async def test_the_first_tick_fires_no_time_trigger():
    """Starting the daemon at 16:00 must not run the 16:00 rule."""
    s, hours, _ = make()
    await s.tick(datetime(2026, 8, 10, 16, 0))
    assert hours == []


async def test_it_fires_once_per_minute_not_once_per_tick():
    s, hours, _ = make()
    await s.tick(datetime(2026, 8, 10, 15, 59, 0))   # the first tick only registers
    await s.tick(datetime(2026, 8, 10, 16, 0, 5))
    await s.tick(datetime(2026, 8, 10, 16, 0, 25))   # same minute
    await s.tick(datetime(2026, 8, 10, 16, 0, 45))   # same minute
    await s.tick(datetime(2026, 8, 10, 16, 1, 5))
    assert hours == ["16:00", "16:01"]


async def test_reminders_are_checked_on_every_tick():
    s, _, reminders = make()
    for sec in (0, 20, 40):
        await s.tick(datetime(2026, 8, 10, 16, 0, sec))
    assert len(reminders) == 3


# ── Lateness ────────────────────────────────────────────────────────────────
def test_a_small_delay_gets_no_label():
    assert lateness_label(timedelta(seconds=30)) == ""
    assert lateness_label(timedelta(minutes=2)) == ""


def test_the_lateness_label_in_minutes_and_hours():
    # Compared against the catalogue rather than a literal: the wording belongs to
    # `i18n`, and pinning it here would make this test measure the language.
    assert lateness_label(timedelta(minutes=20)) == t("reminder.late_minutes", n=20)
    assert lateness_label(timedelta(hours=3)) == t("reminder.late_hours", n=3)


def test_a_large_delay_does_not_pretend_to_be_now():
    """Starting the daemon after a weekend must not warn as if it were happening now."""
    assert lateness_label(timedelta(days=2)) == t("reminder.very_late")


def test_lateness_is_computed_from_the_iso_string():
    now = datetime(2026, 8, 10, 16, 30)
    assert lateness_of("2026-08-10T16:00:00", now) == timedelta(minutes=30)
