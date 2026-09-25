"""The people of the household, and who is looking (D6, D12, D21).

A **Member** is a person the bot and the board recognise. The Owner is born with
the database (id 1), so everything written before Members existed already has
somebody to belong to.

A **Viewer** is who is looking right now, and from where: a Member in a private
conversation sees their own Notes plus the household's; the same Member in a
group sees only the household's (invariant 3). `SYSTEM` is the scheduler, the
review queue and the rule engine — code acting for nobody in particular, which is
the only thing allowed to see every Note.

`SYSTEM` is a distinct object on purpose, not a `None`: a read path that forgets
its viewer should fail loudly, never quietly mean "everyone".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

OWNER_ID = 1


@dataclass(frozen=True)
class Member:
    id: int
    handle: str
    is_owner: bool


@dataclass(frozen=True)
class Viewer:
    member_id: int
    in_group: bool = False


class _System:
    def __repr__(self) -> str:
        return "SYSTEM"


SYSTEM = _System()
OWNER = Viewer(OWNER_ID)


def _row(r) -> Member:
    return Member(id=r["id"], handle=r["handle"], is_owner=bool(r["is_owner"]))


def get(conn: sqlite3.Connection, member_id: int) -> Member | None:
    r = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    return _row(r) if r else None


def by_handle(conn: sqlite3.Connection, handle: str) -> Member | None:
    r = conn.execute("SELECT * FROM members WHERE handle = ?", (handle.lower(),)).fetchone()
    return _row(r) if r else None


def all_members(conn: sqlite3.Connection) -> list[Member]:
    return [_row(r) for r in conn.execute("SELECT * FROM members ORDER BY id")]


def sync(conn: sqlite3.Connection, *, owner: str | None, invited: list[str],
         now: datetime | None = None) -> None:
    """Make the database agree with the config: the Owner's handle, and one row
    per invited handle.

    Invited Members are created, never deleted. Removing somebody from the config
    revokes their access (pairing checks the config), but their Notes are theirs
    and stay — deleting a person's notes because a line left a file would be the
    wrong default for data somebody wrote.
    """
    stamp = (now or datetime.now()).isoformat(timespec="seconds")
    if owner:
        conn.execute("UPDATE members SET handle = ? WHERE id = ?", (owner.lower(), OWNER_ID))
    for handle in invited:
        h = handle.lower()
        if owner and h == owner.lower():
            continue
        conn.execute(
            "INSERT OR IGNORE INTO members (handle, is_owner, created_at) VALUES (?, 0, ?)",
            (h, stamp),
        )
