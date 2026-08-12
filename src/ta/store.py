"""Operations on Notes. The layer between the parser and SQLite.

It knows nothing about HTTP or the LLM. That is on purpose: it is what allows
testing the whole capture path without booting the daemon.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .db import STATUSES, TERMINAL_STATUSES, transaction
from .notes import ParsedNote, parse

# A tiebreak WITHIN a horizon band — no longer the first display criterion (see
# `by_urgency`). No priority comes after low: not declaring one is not the same
# as declaring it low.
PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2, None: 3}

# A Task's Horizon: the band of time its deadline falls into, measured against
# today. It is **derived from the clock, never stored** — the same note changes
# band at midnight with nobody writing anything, and that is exactly why it
# cannot live in `sort_key` (ADR 0010).
#
# The values are English because they are internal wire values, not stored data:
# they travel in the JSON payload and are catalogue keys, and the user only ever
# sees the translated label. Contrast with tags and priority aliases, which are
# stored and therefore never change for cosmetic reasons.
HORIZONS = ("overdue", "today", "week", "later")
HORIZON_RANK = {h: i for i, h in enumerate(HORIZONS)}

# A **rolling** window, not the calendar week: on a Friday the calendar week is
# nearly empty and Monday's task would land in `later`.
WEEK_DAYS = 7


@dataclass
class Note:
    id: int
    text: str
    created_at: str
    due: str | None
    remind_at: str | None
    fired_at: str | None
    done_at: str | None
    priority: str | None
    group_name: str | None
    sort_key: float
    pos_x: int | None
    pos_y: int | None
    color: str | None
    pinned_by_user: bool
    status: str
    tags: list[str]
    # Who decided priority and tags. A typed `!high`/`#tag` locks the field; what
    # review put there itself, review may revisit later.
    priority_by_user: bool = False
    tags_by_user: bool = False
    deleted_at: str | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_task(self) -> bool:
        return self.due is not None

    @property
    def is_reminder(self) -> bool:
        return self.remind_at is not None

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    @property
    def is_terminal(self) -> bool:
        """Left the queue — done or cancelled. Different doors, same queue."""
        return self.status in TERMINAL_STATUSES


def _row_to_note(row: sqlite3.Row, tags: list[str]) -> Note:
    return Note(
        id=row["id"],
        text=row["text"],
        created_at=row["created_at"],
        due=row["due"],
        remind_at=row["remind_at"],
        fired_at=row["fired_at"],
        done_at=row["done_at"],
        priority=row["priority"],
        group_name=row["group_name"],
        sort_key=row["sort_key"],
        pos_x=row["pos_x"],
        pos_y=row["pos_y"],
        color=row["color"],
        priority_by_user=bool(row["priority_by_user"]),
        tags_by_user=bool(row["tags_by_user"]),
        deleted_at=row["deleted_at"],
        pinned_by_user=bool(row["pinned_by_user"]),
        status=row["status"],
        tags=tags,
    )


def add_note(conn: sqlite3.Connection, raw: str, *, now: datetime | None = None) -> Note:
    """Capture a Note. Deterministic, no network (ADR 0003)."""
    now = now or datetime.now()
    p: ParsedNote = parse(raw, now=now)

    # sort_key is born at the end of the queue; `organize` and dragging rewrite
    # it later.
    with transaction(conn):
        next_key = conn.execute("SELECT COALESCE(MAX(sort_key), 0) + 1 FROM notes").fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO notes (text, created_at, due, remind_at, priority, sort_key,
                               priority_by_user, tags_by_user)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p.text,
                now.isoformat(timespec="seconds"),
                p.due.isoformat() if p.due else None,
                p.remind_at.isoformat(timespec="seconds") if p.remind_at else None,
                p.priority,
                next_key,
                # Did you write `!high` or `#tag`? Then the field is yours, and
                # review never touches it — not even in a future re-tagging.
                1 if p.priority else 0,
                1 if p.tags else 0,
            ),
        )
        note_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO tags (note_id, tag) VALUES (?, ?)",
            [(note_id, t) for t in p.tags],
        )

    return get_note(conn, note_id)


def get_note(conn: sqlite3.Connection, note_id: int) -> Note:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise KeyError(f"note {note_id} does not exist")
    tags = [r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE note_id = ?", (note_id,))]
    return _row_to_note(row, sorted(tags))


def list_notes(
    conn: sqlite3.Connection, *, include_done: bool = False, deleted: bool = False
) -> list[Note]:
    """List the Notes. By default hides the terminal and the deleted ones.

    `deleted=True` inverts the filter and returns **only** the deleted ones — it
    is the trash, not an "also include".
    """
    where = ["deleted_at IS NOT NULL"] if deleted else ["deleted_at IS NULL"]
    params: list = []
    if not include_done and not deleted:
        where.append(f"status NOT IN ({','.join('?' * len(TERMINAL_STATUSES))})")
        params += list(TERMINAL_STATUSES)
    sql = f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY sort_key"
    rows = list(conn.execute(sql, params))
    if not rows:
        return []

    # One query for all the tags, instead of N+1.
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    tag_map: dict[int, list[str]] = {i: [] for i in ids}
    for r in conn.execute(f"SELECT note_id, tag FROM tags WHERE note_id IN ({placeholders})", ids):
        tag_map[r["note_id"]].append(r["tag"])

    return [_row_to_note(r, sorted(tag_map[r["id"]])) for r in rows]


def due_today(conn: sqlite3.Connection, *, today: date | None = None) -> list[Note]:
    """Tasks chaseable today or already overdue. The basis of the Digest."""
    today = today or date.today()
    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    rows = list(
        conn.execute(
            f"SELECT * FROM notes WHERE deleted_at IS NULL"
            f" AND status NOT IN ({placeholders})"
            " AND due IS NOT NULL AND due <= ? ORDER BY due, sort_key",
            (*TERMINAL_STATUSES, today.isoformat()),
        )
    )
    return [_row_to_note(r, []) for r in rows]


def pending_reminders(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[Note]:
    """Reminders whose time has passed and that have not fired yet."""
    now = now or datetime.now()
    rows = list(
        conn.execute(
            "SELECT * FROM notes WHERE deleted_at IS NULL"
            " AND fired_at IS NULL AND remind_at IS NOT NULL"
            " AND remind_at <= ? ORDER BY remind_at",
            (now.isoformat(timespec="seconds"),),
        )
    )
    return [_row_to_note(r, []) for r in rows]


def mark_fired(conn: sqlite3.Connection, note_id: int, *, now: datetime | None = None) -> None:
    now = now or datetime.now()
    conn.execute(
        "UPDATE notes SET fired_at = ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), note_id),
    )


def set_schedule(
    conn: sqlite3.Connection,
    note_id: int,
    *,
    due: date | None,
    remind_at: datetime | None,
) -> None:
    """Rewrite deadline and reminder — including to **nothing**.

    It exists so the LLM's second pass can undo what the regex marked. Unlike
    `move_note`, here `None` means "erase", not "leave alone": the most important
    correction this function makes is precisely removing the deadline from a
    retrospective sentence.

    It does not set `pinned_by_user` — this is a machine decision, not your hand.
    """
    conn.execute(
        "UPDATE notes SET due = ?, remind_at = ? WHERE id = ?",
        (
            due.isoformat() if due else None,
            remind_at.isoformat(timespec="minutes") if remind_at else None,
            note_id,
        ),
    )


def set_tags(conn: sqlite3.Connection, note_id: int, tags: list[str]) -> None:
    """Rewrite the tags. Used by review when you wrote none yourself."""
    conn.execute("DELETE FROM tags WHERE note_id = ?", (note_id,))
    conn.executemany(
        "INSERT INTO tags (note_id, tag) VALUES (?, ?)",
        [(note_id, t) for t in sorted(set(tags))],
    )


def set_priority(conn: sqlite3.Connection, note_id: int, priority: str | None) -> None:
    conn.execute("UPDATE notes SET priority = ? WHERE id = ?", (priority, note_id))


def mark_reviewed(conn: sqlite3.Connection, note_id: int, *, now: datetime | None = None) -> None:
    """Leaves the review queue."""
    now = now or datetime.now()
    conn.execute(
        "UPDATE notes SET reviewed_at = ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), note_id),
    )


def count_review_attempt(conn: sqlite3.Connection, note_id: int) -> int:
    """Record an attempt and return the total. A queue with a ceiling, not a loop."""
    conn.execute(
        "UPDATE notes SET review_attempts = review_attempts + 1 WHERE id = ?", (note_id,)
    )
    return conn.execute(
        "SELECT review_attempts FROM notes WHERE id = ?", (note_id,)
    ).fetchone()[0]


def soft_delete(conn: sqlite3.Connection, note_id: int, *, now: datetime | None = None) -> None:
    """Disappears from everywhere except the trash. Reversible on purpose.

    A note is something written in a hurry, and misclicking is easy — deleting
    for real would have no way back. The calendar event, if there is one, is
    **not** removed: deleting the note about an appointment does not cancel the
    appointment.
    """
    now = now or datetime.now()
    conn.execute(
        "UPDATE notes SET deleted_at = ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), note_id),
    )


def restore(conn: sqlite3.Connection, note_id: int) -> None:
    conn.execute("UPDATE notes SET deleted_at = NULL WHERE id = ?", (note_id,))


def purge(conn: sqlite3.Connection, note_id: int) -> bool:
    """Delete permanently. **Only reaches what is already in the trash.**

    That restriction is this function's entire safety: there is no path that
    skips the soft delete, so nothing is destroyed without having passed through
    a recoverable state first. Returns whether anything was deleted.

    The `tags` and the `calendar_links` row go through `ON DELETE CASCADE`. The
    calendar event does **not** — deleting the note about an appointment does not
    cancel the appointment, and that matters even more here, where there is no
    way back.
    """
    cur = conn.execute(
        "DELETE FROM notes WHERE id = ? AND deleted_at IS NOT NULL", (note_id,)
    )
    return cur.rowcount > 0


def purge_all(conn: sqlite3.Connection) -> int:
    """Empty the trash. Returns how many went."""
    return conn.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL").rowcount


def queue_all_for_review(conn: sqlite3.Connection) -> int:
    """Put ALL non-terminal Notes back into the review queue.

    It resets `review_attempts` on purpose: an explicit request from the user is
    a new order, not the continuation of old attempts that already gave up. Done
    and cancelled stay out — reviewing the deadline of something finished spends
    a model call for nothing.
    """
    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    cur = conn.execute(
        f"UPDATE notes SET reviewed_at = NULL, review_attempts = 0"
        f" WHERE deleted_at IS NULL AND status NOT IN ({placeholders})",
        TERMINAL_STATUSES,
    )
    return cur.rowcount


def pending_review(
    conn: sqlite3.Connection, *, max_attempts: int = 5, limit: int = 10
) -> list[int]:
    """Notes that were never reviewed, oldest first.

    `limit` exists so a week offline does not become a burst of model calls on
    the first capture with network. The queue drains gradually, and ordering by
    id guarantees nobody is left behind forever.
    """
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM notes WHERE deleted_at IS NULL"
            " AND reviewed_at IS NULL AND review_attempts < ?"
            " ORDER BY id LIMIT ?",
            (max_attempts, limit),
        )
    ]


def set_status(
    conn: sqlite3.Connection, note_id: int, status: str, *, now: datetime | None = None
) -> None:
    """Change the Note's state.

    `done_at` is kept in sync as a *timestamp*, not as state: entering `done`
    records the instant, leaving clears it. Cancelling never writes `done_at` —
    the Note was not done.
    """
    if status not in STATUSES:
        raise ValueError(f"invalid status: {status!r}. Valid: {', '.join(STATUSES)}")
    now = now or datetime.now()
    done_at = now.isoformat(timespec="seconds") if status == "done" else None
    conn.execute(
        "UPDATE notes SET status = ?, done_at = ? WHERE id = ?", (status, done_at, note_id)
    )


def mark_done(conn: sqlite3.Connection, note_id: int, *, now: datetime | None = None) -> None:
    set_status(conn, note_id, "done", now=now)


def mark_undone(conn: sqlite3.Connection, note_id: int) -> None:
    """Unmarking matters as much as marking: a wrong click happens."""
    set_status(conn, note_id, "todo")


def horizon(due: str | None, *, today: date | None = None) -> str:
    """The time band of a deadline, counted from today. One of `HORIZONS`.

    No deadline lands in `later`, alongside what is far off. That is a choice,
    not an oversight: in a band of its own at the end, a `!high` note with no date
    would sit behind a `!low` due in September — and what has no date marked is
    not, for that reason, less important than the distant future.
    """
    if due is None:
        return "later"
    today = today or date.today()
    day = date.fromisoformat(due)
    if day < today:
        return "overdue"
    if day == today:
        return "today"
    if day <= today + timedelta(days=WEEK_DAYS):
        return "week"
    return "later"


def by_urgency(notes: list[Note], *, today: date | None = None) -> list[Note]:
    """Display order: the clock decides the band, the rest decides within it.

    The deadline dominates priority — a `!low` due today comes before a `!high`
    due in three days, because today's is the one that has to be done today
    (ADR 0010). Priority did not lose value, it changed scope: it orders within
    the band.
    """
    today = today or date.today()
    return sorted(
        notes,
        key=lambda n: (
            # Out of the queue, always at the end. This term comes BEFORE the
            # horizon on purpose: without it, a note completed last week —
            # deadline in the past, therefore `overdue` — would rise to the top
            # of the board.
            n.is_terminal,
            HORIZON_RANK[horizon(n.due, today=today)],
            PRIORITY_RANK.get(n.priority, 3),
            # With a deadline before without one. Without this term, `n.due or ""`
            # maps an undated note to `""`, which sorts before any ISO date, and
            # inside `later` the loose ideas would jump ahead of dated tasks.
            n.due is None,
            n.due or "",
            # And finally what `organize` stored, or the capture order.
            n.sort_key,
        ),
    )


def move_note(
    conn: sqlite3.Connection,
    note_id: int,
    *,
    sort_key: float | None = None,
    pos_x: int | None = None,
    pos_y: int | None = None,
    group_name: str | None = None,
    color: str | None = None,
) -> None:
    """Reorder / drag / colour by hand.

    A null colour in the database is not the absence of colour: it means "use the
    priority default", derived at drawing time. Only what the hand chose is
    stored, and that is why changing the priority recolours a note nobody ever
    touched and does not recolour one you painted.

    It sets `pinned_by_user`, which is what makes a later `organize` respect the
    user's decision instead of undoing it (ADR 0003).
    """
    sets, params = ["pinned_by_user = 1"], []
    for column, value in (
        ("sort_key", sort_key),
        ("pos_x", pos_x),
        ("pos_y", pos_y),
        ("group_name", group_name),
        ("color", color),
    ):
        # `color=""` asks to go back to the priority default, which is different
        # from `color=None`, which means "do not touch the colour".
        if column == "color" and value == "":
            sets.append("color = NULL")
            continue
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    params.append(note_id)
    conn.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id = ?", params)


STATUS_MARK = {"todo": "[ ]", "doing": "[~]", "hold": "[-]", "done": "[x]", "cancelled": "[/]"}


def export_markdown(conn: sqlite3.Connection) -> str:
    """The escape hatch that justifies choosing SQLite: dump everything as markdown.

    The labels go through the catalogue. They used to be hardcoded Portuguese,
    which meant `ta export` answered in Portuguese no matter what `TA_LANG` said —
    a gap the i18n pass missed because nothing in the export is on screen, so
    nobody looked at it.
    """
    from .i18n import t

    lines = [f"# {t('export.title')}", ""]
    for n in by_urgency(list_notes(conn, include_done=True)):
        box = STATUS_MARK[n.status]
        bits = []
        if n.status not in ("todo", "done"):
            bits.append(n.status)
        if n.due:
            bits.append(f"{t('export.due')} {n.due}")
        if n.remind_at:
            bits.append(f"{t('export.reminds')} {n.remind_at}")
        if n.priority:
            bits.append(f"{t('export.priority')} {n.priority}")
        if n.tags:
            bits.append(" ".join(f"#{t_}" for t_ in n.tags))
        suffix = f"  _({', '.join(bits)})_" if bits else ""
        lines.append(f"- {box} {n.text}{suffix}")
    return "\n".join(lines) + "\n"
