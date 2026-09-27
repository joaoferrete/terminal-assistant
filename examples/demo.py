"""Seed a demo database, in English, in a temporary directory.

It exists for two reasons.

The first is the README images: real notes cite colleagues' names and work
content, and cleaning that by hand only works while you remember to. Here the
data is fictional by construction, and the screenshot can be regenerated when the
interface changes rather than ageing in a folder.

The second is trying things out. `make demo` boots an isolated daemon, with its
own database, without touching the real one — you can click everything, delete
everything, and close it.

The selection deliberately covers the four Horizon bands, the five kanban
columns, the three priorities and one hand-dragged note, because a demo screen
that only shows the happy case does not show the product.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ta.db import connect  # noqa: E402
from ta.store import add_note, move_note, set_status  # noqa: E402

TODAY = date.today()


def d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


# (text, due, status, priority, tags)
NOTES = [
    # `overdue` — the band the board exists to show first.
    ("Reply to the landlord about the lease", d(-3), "todo", "high", ["personal"]),
    ("Rotate the staging API keys", d(-1), "doing", "high", ["work"]),
    # `today`
    ("Review the pull request from the data team", d(0), "todo", "high", ["work"]),
    ("Pick up the dry cleaning", d(0), "todo", "low", ["personal", "errand"]),
    ("Write the incident postmortem", d(0), "doing", "medium", ["work"]),
    # `week` — the rolling 7-day window
    ("Book the dentist appointment", d(2), "todo", "medium", ["health"]),
    ("Prepare slides for the quarterly review", d(4), "hold", "high", ["work"]),
    ("Renew the domain before it lapses", d(6), "todo", "medium", ["personal"]),
    # `later`, and whatever has no deadline at all — they live together on purpose
    ("Plan the trip in September", d(40), "todo", "low", ["personal"]),
    ("Idea: a rule that dims the lights during calls", None, "todo", "medium", ["ideas"]),
    ("Read the paper on CRDTs someone linked", None, "todo", None, ["reading"]),
    # Out of the queue: the two exits differ, and the kanban shows that.
    ("Ship the board redesign", d(-2), "done", "high", ["work"]),
    ("Migrate the blog to a static site", d(-10), "cancelled", "low", ["personal"]),
]


LIST_ITEMS = ["Milk", "Coffee beans", "Dish soap", "A very long item name that has to wrap "
              "inside its card instead of pushing the layout sideways"]


def seed(db_path: Path) -> int:
    conn = connect(db_path)
    for i, (text, due, status, prio, tags) in enumerate(NOTES):
        # Written through the normal path rather than a raw INSERT: that way the
        # demo exercises the same parser and the same rules as real use, and a
        # break in it shows up here before it shows up for somebody.
        n = add_note(conn, text)
        conn.execute(
            "UPDATE notes SET due = ?, priority = ?, created_at = ? WHERE id = ?",
            (due, prio, (datetime.now() - timedelta(hours=len(NOTES) - i)).isoformat(), n.id),
        )
        for tag in tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (note_id, tag) VALUES (?, ?)", (n.id, tag)
            )
        if status != "todo":
            set_status(conn, n.id, status)

    # One hand-dragged note, so the free-form view does not look like an empty
    # grid — and because the stored position is the mechanism that makes the hand
    # beat the model.
    move_note(conn, 3, pos_x=48, pos_y=430)

    # A household List with items, so the Lists view has something to show. It
    # is created here rather than left to the boot sync, because the demo must
    # not depend on whatever the person running it has in their config.toml.
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.execute(
        "INSERT INTO lists (name, scope, owner_id, created_at)"
        " VALUES ('groceries', 'household', 1, ?)", (now,)
    )
    for item in LIST_ITEMS:
        add_note(conn, item, list_id=cur.lastrowid)
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    conn.close()
    return total


if __name__ == "__main__":
    target = Path(os.environ.get("TA_DB", "/tmp/ta-demo/demo.db"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    print(f"{seed(target)} notes in {target}")
