"""Routines: a named sequence of actions, made in the chat (F10, D57, D58).

"Cria uma rotina chegada que liga a luz da sala e o ventilador" stores the steps;
"cheguei em casa" — one of its phrases — runs them. A phrase is matched by the
code, with no model (the pre-router's rule: what can be recognised for free is),
and a paraphrase ("tô chegando, prepara a casa") reaches the same Routine through
the agent's `routine_run`.

A Routine runs with the Grant of whoever starts it, never its creator's. A
household Routine started by someone who may not touch the TV does the rest and
says it skipped the TV.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Routine:
    id: int
    owner_id: int
    name: str
    scope: str
    steps: list[dict]
    phrases: list[str]


def fold(text: str) -> str:
    """Case, accents and punctuation out: "Cheguei em casa!" == "cheguei em casa"."""
    bare = "".join(c for c in unicodedata.normalize("NFKD", text.lower())
                   if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w\s]", " ", bare).split())


def _row(r) -> Routine:
    return Routine(r["id"], r["owner_id"], r["name"], r["scope"], json.loads(r["steps"]),
                   json.loads(r["phrases"]))


def visible(conn: sqlite3.Connection, member_id: int) -> list[Routine]:
    """The Member's own Routines and the household's."""
    return [_row(r) for r in conn.execute(
        "SELECT * FROM routines WHERE deleted_at IS NULL AND (owner_id = ? OR scope = 'household')"
        " ORDER BY name", (member_id,))]


def by_name(conn: sqlite3.Connection, member_id: int, name: str) -> Routine | None:
    wanted = fold(name)
    mine = [r for r in visible(conn, member_id) if fold(r.name) == wanted]
    # One's own first: a personal "cinema" wins over the household's.
    return next((r for r in mine if r.owner_id == member_id), mine[0] if mine else None)


def by_phrase(conn: sqlite3.Connection, member_id: int, text: str) -> Routine | None:
    said = fold(text)
    if not said:
        return None
    found = [r for r in visible(conn, member_id) if said in {fold(p) for p in r.phrases}]
    return next((r for r in found if r.owner_id == member_id), found[0] if found else None)


def create(conn: sqlite3.Connection, *, owner_id: int, name: str, steps: list[dict],
           phrases: list[str], household: bool = False, now: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO routines (owner_id, name, scope, steps, phrases, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (owner_id, name.strip(), "household" if household else "personal",
         json.dumps(steps, ensure_ascii=False), json.dumps(phrases, ensure_ascii=False),
         (now or datetime.now()).isoformat(timespec="seconds")))
    return cur.lastrowid


def delete(conn: sqlite3.Connection, routine_id: int, *, member_id: int, is_owner: bool) -> bool:
    """Only its creator, or the Owner (D58)."""
    cur = conn.execute(
        "UPDATE routines SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL"
        " AND (owner_id = ? OR ?)",
        (datetime.now().isoformat(timespec="seconds"), routine_id, member_id, int(is_owner)))
    return cur.rowcount == 1
