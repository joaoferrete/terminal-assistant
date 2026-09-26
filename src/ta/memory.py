"""Conversation Memory (D13, D33): what was said, kept per conversation.

Two ways back into it, and only two:

- `recent` — the last few messages of a conversation that is still **live**
  (D33). A lone "turn on the light" three days later carries no history, which is
  what the user asked for.
- `search` — a Tool the agent calls on purpose, confined to the current
  conversation. What was said in a private chat is never found from a group, and
  the other way round.

Search matches words without accents or case in Python rather than with SQLite
FTS5. FTS5 could not be confirmed on the server's Python without asking, and a
migration that fails there stops the daemon from starting; for a household's
volume the scan is instant.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

LIVE_WINDOW = timedelta(minutes=15)
RECENT_LIMIT = 8
RETENTION_DAYS = 90


@dataclass(frozen=True)
class Message:
    member_id: int | None     # None is the bot
    text: str
    at: str


def fold(text: str) -> str:
    """Lowercase, no accents: "Café" and "cafe" are the same word to a person."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower()) if not unicodedata.combining(c)
    )


def words(query: str) -> list[str]:
    return [w for w in fold(query).split() if len(w) > 2]


def record(conn: sqlite3.Connection, *, channel: str, conversation_id: str,
           message_id: str | None, member_id: int | None, text: str,
           now: datetime | None = None) -> None:
    conn.execute(
        "INSERT INTO messages (channel, conversation_id, message_id, member_id, text, at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (channel, conversation_id, message_id, member_id, text,
         (now or datetime.now()).isoformat(timespec="seconds")),
    )


def recent(conn: sqlite3.Connection, *, channel: str, conversation_id: str,
           now: datetime | None = None) -> list[Message]:
    """The last messages, if the conversation is live; otherwise nothing."""
    now = now or datetime.now()
    rows = conn.execute(
        "SELECT member_id, text, at FROM messages WHERE channel = ? AND conversation_id = ?"
        " ORDER BY id DESC LIMIT ?",
        (channel, conversation_id, RECENT_LIMIT),
    ).fetchall()
    if not rows or datetime.fromisoformat(rows[0][2]) < now - LIVE_WINDOW:
        return []
    return [Message(*r) for r in reversed(rows)]


def search(conn: sqlite3.Connection, *, channel: str, conversation_id: str, query: str,
           limit: int = 8) -> list[Message]:
    wanted = words(query)
    if not wanted:
        return []
    rows = conn.execute(
        "SELECT member_id, text, at FROM messages WHERE channel = ? AND conversation_id = ?"
        " ORDER BY id DESC",
        (channel, conversation_id),
    ).fetchall()
    return [Message(*r) for r in rows if all(w in fold(r[1]) for w in wanted)][:limit]


def forget_older(conn: sqlite3.Connection, *, days: int = RETENTION_DAYS,
                 now: datetime | None = None) -> int:
    cutoff = ((now or datetime.now()) - timedelta(days=days)).isoformat(timespec="seconds")
    return conn.execute("DELETE FROM messages WHERE at < ?", (cutoff,)).rowcount
