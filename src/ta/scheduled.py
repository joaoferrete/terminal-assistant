"""Scheduled actions: a Tool call that runs later (F9, D39).

"Liga a luz daqui 10 min" and "acende a luz todo dia às 7h" are the same thing:
a row with the Tool, its arguments, and when. The row lives in the database, so a
restart does not lose it, and it can be listed ("quanto falta?") and cancelled.

What this module decides, and the bot does not:

- **When** the next run is. For a recurring row it is computed from the time of
  day and the calendar every time, never by adding 24 hours to the last run, so a
  late or skipped run does not drift the schedule.
- **What is too late.** A run more than `LATE` behind (the server was off, the
  daemon was restarting) is skipped and the author is told. Switching a light on
  at 07:40 because the box rebooted at 07:39 is worse than not switching it.

Running the Tool is the bot's job (`Bot.run_scheduled`): it re-reads the author's
Grant at fire time, so a Grant revoked after scheduling is honoured.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

LATE = timedelta(minutes=15)
REPEATS = ("once", "daily", "weekdays", "weekends")
# A household does not need a timer years ahead, and a far-off date is more
# likely a misread year than a wish.
FURTHEST = timedelta(days=366)


class BadSchedule(ValueError):
    """The time or repetition cannot be understood. Said to the model."""


@dataclass(frozen=True)
class Scheduled:
    id: int
    member_id: int
    channel: str
    conversation_id: str
    in_group: bool
    tool: str
    args: dict
    summary: str
    repeat: str
    time_of_day: str | None
    next_at: datetime
    state: str


def _row(r) -> Scheduled:
    return Scheduled(r["id"], r["member_id"], r["channel"], r["conversation_id"],
                     bool(r["in_group"]), r["tool"], json.loads(r["args"]), r["summary"],
                     r["repeat"], r["time_of_day"], datetime.fromisoformat(r["next_at"]),
                     r["state"])


def _hhmm(text: str) -> tuple[int, int] | None:
    raw = text.strip().lower().replace("h", ":").rstrip(":")
    hh, _, mm = raw.partition(":")
    if not hh.isdigit() or (mm and not mm.isdigit()):
        return None
    h, m = int(hh), int(mm or 0)
    return (h, m) if 0 <= h < 24 and 0 <= m < 60 else None


def _fits(repeat: str, day: datetime) -> bool:
    if repeat == "weekdays":
        return day.weekday() < 5
    if repeat == "weekends":
        return day.weekday() >= 5
    return True


def next_occurrence(repeat: str, time_of_day: str, after: datetime) -> datetime:
    """The first moment strictly after `after` at `time_of_day` on a day `repeat`
    allows."""
    h, m = _hhmm(time_of_day) or (0, 0)
    candidate = after.replace(hour=h, minute=m, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    while not _fits(repeat, candidate):
        candidate += timedelta(days=1)
    return candidate


def when(*, in_minutes: str = "", at: str = "", repeat: str = "once",
         now: datetime) -> tuple[datetime, str | None]:
    """Parse what the model gave into (first run, time of day for a recurring row).

    `in_minutes` is for "daqui 10 min"; `at` is "HH:MM" (the next one) or
    "YYYY-MM-DD HH:MM". A recurring row needs a time of day, never a delay:
    "every day in 10 minutes" means nothing.
    """
    repeat = (repeat or "once").strip().lower()
    if repeat not in REPEATS:
        raise BadSchedule(f"repeat must be one of {', '.join(REPEATS)}")
    if str(in_minutes).strip():
        if repeat != "once":
            raise BadSchedule("a repeating action needs a time of day (at=HH:MM), not a delay")
        try:
            minutes = float(str(in_minutes).strip())
        except ValueError:
            raise BadSchedule(f"not a number of minutes: {in_minutes!r}") from None
        if not 0 < minutes <= FURTHEST.total_seconds() / 60:
            raise BadSchedule("the delay must be positive and under a year")
        return (now + timedelta(minutes=minutes)).replace(microsecond=0), None
    text = at.strip()
    if not text:
        raise BadSchedule("give in_minutes or at")
    if len(text) > 5 and text[:4].isdigit():
        if repeat != "once":
            raise BadSchedule("a repeating action takes only a time of day (HH:MM)")
        try:
            moment = datetime.fromisoformat(text.replace(" ", "T"))
        except ValueError:
            raise BadSchedule(f"not a date and time: {at!r}; use YYYY-MM-DD HH:MM") from None
        if moment <= now or moment - now > FURTHEST:
            raise BadSchedule("that moment is in the past, or more than a year away")
        return moment.replace(second=0, microsecond=0), None
    parsed = _hhmm(text)
    if parsed is None:
        raise BadSchedule(f"not a time: {at!r}; use HH:MM")
    time_of_day = f"{parsed[0]:02d}:{parsed[1]:02d}"
    return next_occurrence(repeat, time_of_day, now), (time_of_day if repeat != "once" else None)


def add(conn: sqlite3.Connection, *, member_id: int, channel: str, conversation_id: str,
        in_group: bool, tool: str, args: dict, summary: str, next_at: datetime,
        repeat: str = "once", time_of_day: str | None = None,
        now: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO scheduled (member_id, channel, conversation_id, in_group, tool, args,"
        " summary, repeat, time_of_day, next_at, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (member_id, channel, conversation_id, int(in_group), tool,
         json.dumps(args, ensure_ascii=False), summary, repeat, time_of_day,
         next_at.isoformat(timespec="seconds"),
         (now or datetime.now()).isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def get(conn: sqlite3.Connection, scheduled_id: int) -> Scheduled | None:
    r = conn.execute("SELECT * FROM scheduled WHERE id = ?", (scheduled_id,)).fetchone()
    return _row(r) if r else None


def active(conn: sqlite3.Connection, member_id: int) -> list[Scheduled]:
    """A Member's own pending actions, soonest first. Nobody lists another's."""
    return [_row(r) for r in conn.execute(
        "SELECT * FROM scheduled WHERE member_id = ? AND state = 'active' ORDER BY next_at",
        (member_id,))]


def cancel(conn: sqlite3.Connection, scheduled_id: int, member_id: int) -> bool:
    """Cancel one of the Member's own. False if it is not theirs, or not active —
    the same answer for both, so ids are not confirmed to strangers."""
    cur = conn.execute(
        "UPDATE scheduled SET state = 'cancelled'"
        " WHERE id = ? AND member_id = ? AND state = 'active'", (scheduled_id, member_id))
    return cur.rowcount == 1


def due(conn: sqlite3.Connection, now: datetime) -> list[Scheduled]:
    return [_row(r) for r in conn.execute(
        "SELECT * FROM scheduled WHERE state = 'active' AND next_at <= ? ORDER BY next_at",
        (now.isoformat(timespec="seconds"),))]


def is_late(s: Scheduled, now: datetime) -> bool:
    return now - s.next_at > LATE


def advance(conn: sqlite3.Connection, s: Scheduled, now: datetime) -> datetime | None:
    """After a run (or a skip): a one-off is finished; a recurring row moves to its
    next occurrence after `now`. Returns the next run, if any.

    Called BEFORE the Tool runs, so a Tool that hangs or crashes the round cannot
    make the same row fire again on the next tick.
    """
    if s.repeat == "once" or not s.time_of_day:
        conn.execute("UPDATE scheduled SET state = 'done', last_run_at = ? WHERE id = ?",
                     (now.isoformat(timespec="seconds"), s.id))
        return None
    nxt = next_occurrence(s.repeat, s.time_of_day, now)
    conn.execute("UPDATE scheduled SET next_at = ?, last_run_at = ? WHERE id = ?",
                 (nxt.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), s.id))
    return nxt


def remaining(at: datetime, now: datetime) -> str:
    """"2h05", "10 min", "3d 4h": how long until `at`, computed by the code so the
    model never does arithmetic on clocks."""
    # Rounded up: "in 0 min" reads as already gone.
    total = -(-max(0, int((at - now).total_seconds())) // 60)
    days, total = divmod(total, 1440)
    hours, minutes = divmod(total, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h{minutes:02d}"
    return f"{minutes} min"
