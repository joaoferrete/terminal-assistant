"""Chat Rules: automations asked for in conversation, stored as data (F10, D46, D54).

"Quando a porta abrir depois das 22h, acende a luz do corredor" becomes one row:
a Trigger (an Entity reaching a state, or the microphone), an optional condition
(a time window, another Entity's state), and one Tool call. The agent never writes
a Python rule: a model writing code the daemon imports is code execution by
another name (ADR 0020).

Like a file Rule, a chat Rule is a Rule — the glossary's word — and both are
listed together. Unlike one, it fires with its **creator's** Grant, read at fire
time, and every firing is told in the creator's chat with [Undo] (D45: nothing
acts invisibly).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta

# The same Rule does not fire twice within this: a door sensor that flaps
# open/closed/open must not switch the hall light three times.
DEBOUNCE = timedelta(seconds=60)


@dataclass(frozen=True)
class ChatRule:
    id: int
    owner_id: int
    name: str
    scope: str
    trigger_kind: str
    trigger_entity: str | None
    trigger_to: str | None
    cond_after: str | None
    cond_before: str | None
    cond_entity: str | None
    cond_state: str | None
    action_tool: str
    action_args: dict
    enabled: bool
    last_fired_at: str | None

    def describe(self) -> str:
        """One line, for lists: when, if, then."""
        when = (f"when {self.trigger_entity} becomes {self.trigger_to or 'anything'}"
                if self.trigger_kind == "state" else f"when the microphone turns {self.trigger_to}")
        cond = []
        if self.cond_after or self.cond_before:
            cond.append(f"between {self.cond_after or '00:00'} and {self.cond_before or '24:00'}")
        if self.cond_entity:
            cond.append(f"if {self.cond_entity} is {self.cond_state}")
        args = ", ".join(f"{k}={v}" for k, v in self.action_args.items())
        return (f"{when}{' ' + ' and '.join(cond) if cond else ''}: {self.action_tool} {args}"
                + ("" if self.enabled else " [off]"))


def _row(r) -> ChatRule:
    return ChatRule(r["id"], r["owner_id"], r["name"], r["scope"], r["trigger_kind"],
                    r["trigger_entity"], r["trigger_to"], r["cond_after"], r["cond_before"],
                    r["cond_entity"], r["cond_state"], r["action_tool"],
                    json.loads(r["action_args"]), bool(r["enabled"]), r["last_fired_at"])


def create(conn: sqlite3.Connection, *, owner_id: int, name: str, trigger_kind: str,
           trigger_entity: str | None, trigger_to: str | None, action_tool: str,
           action_args: dict, cond_after: str | None = None, cond_before: str | None = None,
           cond_entity: str | None = None, cond_state: str | None = None,
           household: bool = False, now: datetime | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO chat_rules (owner_id, name, scope, trigger_kind, trigger_entity,"
        " trigger_to, cond_after, cond_before, cond_entity, cond_state, action_tool,"
        " action_args, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (owner_id, name.strip(), "household" if household else "personal", trigger_kind,
         trigger_entity, trigger_to, cond_after, cond_before, cond_entity, cond_state,
         action_tool, json.dumps(action_args, ensure_ascii=False),
         (now or datetime.now()).isoformat(timespec="seconds")))
    return cur.lastrowid


def visible(conn: sqlite3.Connection, member_id: int) -> list[ChatRule]:
    return [_row(r) for r in conn.execute(
        "SELECT * FROM chat_rules WHERE deleted_at IS NULL"
        " AND (owner_id = ? OR scope = 'household')"
        " ORDER BY name", (member_id,))]


def get(conn: sqlite3.Connection, rule_id: int) -> ChatRule | None:
    r = conn.execute("SELECT * FROM chat_rules WHERE id = ? AND deleted_at IS NULL",
                     (rule_id,)).fetchone()
    return _row(r) if r else None


def by_name(conn: sqlite3.Connection, member_id: int, name: str) -> ChatRule | None:
    wanted = " ".join(name.lower().split())
    return next((r for r in visible(conn, member_id)
                 if " ".join(r.name.lower().split()) == wanted), None)


def set_enabled(conn: sqlite3.Connection, rule_id: int, enabled: bool, *, member_id: int,
                is_owner: bool) -> bool:
    cur = conn.execute("UPDATE chat_rules SET enabled = ? WHERE id = ? AND deleted_at IS NULL"
                       " AND (owner_id = ? OR ?)", (int(enabled), rule_id, member_id,
                                                     int(is_owner)))
    return cur.rowcount == 1


def delete(conn: sqlite3.Connection, rule_id: int, *, member_id: int, is_owner: bool) -> bool:
    cur = conn.execute("UPDATE chat_rules SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL"
                       " AND (owner_id = ? OR ?)",
                       (datetime.now().isoformat(timespec="seconds"), rule_id, member_id,
                        int(is_owner)))
    return cur.rowcount == 1


def triggered(conn: sqlite3.Connection, kind: str, entity: str | None, new: str,
              now: datetime) -> list[ChatRule]:
    """The enabled Rules this signal starts, past their debounce."""
    out = []
    for r in conn.execute("SELECT * FROM chat_rules WHERE deleted_at IS NULL AND enabled = 1"
                          " AND trigger_kind = ?", (kind,)):
        rule = _row(r)
        if kind == "state" and rule.trigger_entity != entity:
            continue
        if rule.trigger_to and rule.trigger_to.lower() != str(new).lower():
            continue
        if rule.last_fired_at and now - datetime.fromisoformat(rule.last_fired_at) < DEBOUNCE:
            continue
        out.append(rule)
    return out


def mark_fired(conn: sqlite3.Connection, rule_id: int, now: datetime) -> None:
    conn.execute("UPDATE chat_rules SET last_fired_at = ? WHERE id = ?",
                 (now.isoformat(timespec="seconds"), rule_id))


def _clock(text: str | None) -> time | None:
    if not text:
        return None
    hh, _, mm = text.partition(":")
    return time(int(hh) % 24, int(mm or 0))


def in_window(rule: ChatRule, now: datetime) -> bool:
    """The time condition; a window may cross midnight ("22:00" to "06:00")."""
    after, before = _clock(rule.cond_after), _clock(rule.cond_before)
    if after is None and before is None:
        return True
    t_ = now.time()
    if after is not None and before is not None and after > before:
        return t_ >= after or t_ < before
    return (after is None or t_ >= after) and (before is None or t_ < before)
