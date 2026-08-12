"""Scheduler: the time triggers.

Two roles. Firing Rules at given times (`at_time`), and firing the Reminders that
have come due — which is the path that closes the engine's first complete loop
(time → notify) and proves the thesis that both halves share one engine.

One explicit decision: **no retroactive flood.** If the daemon was down for
hours, starting it must not spit out dozens of notifications for times that
already passed. A due Reminder fires once, but a missed time is missed.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

log = logging.getLogger("ta.scheduler")

TICK_S = 20.0

# A Reminder later than this fires with a lateness note rather than pretending it
# is now. Without it, starting the daemon after a weekend would announce Friday's
# "take the pills".
MAX_LATENESS = timedelta(hours=12)


class Scheduler:
    def __init__(
        self,
        on_time: Callable[[str, datetime], Awaitable[None]],
        on_reminder: Callable[[datetime], Awaitable[None]],
    ) -> None:
        self.on_time = on_time
        self.on_reminder = on_reminder
        self._last_minute: str | None = None

    async def tick(self, now: datetime) -> None:
        """One cycle. Takes `now` so it is testable without waiting on the clock."""
        minute = now.strftime("%H:%M")

        # A time trigger fires once per minute, not once per tick.
        if minute != self._last_minute:
            first = self._last_minute is None
            self._last_minute = minute
            # It does not fire on the first tick: starting the daemon at 16:00
            # must not run the 16:00 rule as if the minute had just turned.
            if not first:
                await self.on_time(minute, now)

        await self.on_reminder(now)

    async def run(self) -> None:
        while True:
            try:
                await self.tick(datetime.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler tick failed; the loop carries on")
            await asyncio.sleep(TICK_S)


def lateness_of(remind_at: str, now: datetime) -> timedelta:
    return now - datetime.fromisoformat(remind_at)


def lateness_label(late: timedelta) -> str:
    """An honest label when the Reminder fires late."""
    from .i18n import t

    if late <= timedelta(minutes=2):
        return ""
    if late < timedelta(hours=1):
        return t("reminder.late_minutes", n=int(late.total_seconds() // 60))
    if late < MAX_LATENESS:
        return t("reminder.late_hours", n=int(late.total_seconds() // 3600))
    return t("reminder.very_late")
