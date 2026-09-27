"""Receipts: what the bot did because of one message (D11, T4.4).

The same record answers three things: "what did you do here?" (a reply to the
message), the [undo] button, and a **pending** action waiting for the asker's
confirmation (D27) — which is stored with its arguments and runs only when the
button is pressed, by the Member it was proposed to.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Receipt:
    id: int
    member_id: int
    tool: str
    args: dict
    summary: str
    undo: dict | None
    state: str


def _row(r) -> Receipt:
    return Receipt(r["id"], r["member_id"], r["tool"], json.loads(r["args"] or "{}"),
                   r["summary"], json.loads(r["undo"]) if r["undo"] else None, r["state"])


def record(conn: sqlite3.Connection, *, channel: str, conversation_id: str,
           message_id: str | None, member_id: int, tool: str, summary: str,
           args: dict | None = None, undo: dict | None = None, state: str = "done",
           now: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO receipts (channel, conversation_id, message_id, member_id, tool, args,"
        " summary, undo, state, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (channel, conversation_id, message_id, member_id, tool,
         json.dumps(args or {}, ensure_ascii=False), summary,
         json.dumps(undo, ensure_ascii=False) if undo else None, state,
         (now or datetime.now()).isoformat(timespec="seconds")),
    )
    return cur.lastrowid


def get(conn: sqlite3.Connection, receipt_id: int) -> Receipt | None:
    r = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
    return _row(r) if r else None


def set_state(conn: sqlite3.Connection, receipt_id: int, state: str) -> None:
    conn.execute("UPDATE receipts SET state = ? WHERE id = ?", (state, receipt_id))


def set_undo(conn: sqlite3.Connection, receipt_id: int, undo: dict | None) -> None:
    conn.execute("UPDATE receipts SET undo = ? WHERE id = ?",
                 (json.dumps(undo, ensure_ascii=False) if undo else None, receipt_id))


def answered_by(conn: sqlite3.Connection, receipt_ids: list[int], bot_message_id: str) -> None:
    conn.executemany("UPDATE receipts SET bot_message_id = ? WHERE id = ?",
                     [(bot_message_id, rid) for rid in receipt_ids])


def for_message(conn: sqlite3.Connection, *, channel: str, conversation_id: str,
                message_id: str) -> list[Receipt]:
    """What was done because of a message — whether `message_id` is the Member's
    message or the bot's answer to it, since a reply can quote either."""
    return [_row(r) for r in conn.execute(
        "SELECT * FROM receipts WHERE channel = ? AND conversation_id = ?"
        " AND (message_id = ? OR bot_message_id = ?) ORDER BY id",
        (channel, conversation_id, message_id, message_id))]
