"""Persistence: SQLite through the standard library, no ORM.

The model is the one from ADR 0006: a single `notes` table. The roles (Task,
Reminder, completed) come from the presence of attributes, not from a type
column.

Order and board position are stored data, not computation. That is what makes the
user's hand beat the LLM permanently (ADR 0003).
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("ta")

SCHEMA_VERSION = 11

# The states of a Note. Stored in English because the rest of the vocabulary is
# (see CONTEXT.md); the translated labels live in the interface.
# `done` and `cancelled` are terminal: the Note left the queue, by different
# doors — one was done, the other never will be.
STATUSES = ("todo", "doing", "hold", "done", "cancelled")
TERMINAL_STATUSES = ("done", "cancelled")


def default_db_path() -> Path:
    # `TA_DB` points the database somewhere else. It is what allows booting a
    # demo or scratch daemon without going anywhere near the real database — and
    # the real one holds notes a person wrote, so "nowhere near" is a
    # requirement, not a convenience.
    if chosen := os.environ.get("TA_DB"):
        return Path(chosen).expanduser()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ta" / "ta.db"


# Each migration is (version, sql). Applied in order, once, controlled by
# `PRAGMA user_version`. Never edit a released migration — add another one.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- One entity only. `due` makes it a Task, `remind_at` makes it a
        -- Reminder, `done_at` completes it. See ADR 0006.
        CREATE TABLE notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            due         TEXT,             -- date (YYYY-MM-DD): the Task role
            remind_at   TEXT,             -- ISO instant: the Reminder role
            fired_at    TEXT,             -- when the Reminder fired; null = pending
            done_at     TEXT,             -- completion
            priority    TEXT,             -- high | medium | low
            group_name  TEXT,             -- grouping (from the user or the LLM)
            sort_key    REAL   NOT NULL DEFAULT 0,   -- stored order
            pos_x       INTEGER,          -- board position, once dragged
            pos_y       INTEGER,
            color       TEXT,
            pinned_by_user INTEGER NOT NULL DEFAULT 0  -- 1 = the LLM does not reorder
        );

        CREATE INDEX idx_notes_due       ON notes(due)       WHERE done_at IS NULL;
        CREATE INDEX idx_notes_remind    ON notes(remind_at) WHERE fired_at IS NULL;
        CREATE INDEX idx_notes_open      ON notes(done_at);

        CREATE TABLE tags (
            note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            tag     TEXT    NOT NULL,
            PRIMARY KEY (note_id, tag)
        );

        -- Link to an event created in the dedicated calendar. We keep only the
        -- identifier: the calendar is the source of truth (ADR 0004).
        CREATE TABLE calendar_links (
            note_id     INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
            uid         TEXT NOT NULL,
            source_uid  TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        -- Priorities: the markdown editable by prompt. One row, versioned by
        -- history so that "put study above work" stays auditable.
        CREATE TABLE priorities (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            content    TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        );
        """,
    ),
    (
        2,
        """
        -- The kanban needs real state, and `done_at IS NOT NULL` could only
        -- answer yes or no. `cancelled` is the case that proves the need: a
        -- cancelled Note left the queue without having been done, and the binary
        -- model had no way to represent that.
        --
        -- `done_at` is NOT replaced: it is still the instant the Note entered
        -- `done`. State and timestamp are different things.
        ALTER TABLE notes ADD COLUMN status TEXT NOT NULL DEFAULT 'todo'
            CHECK (status IN ('todo','doing','hold','done','cancelled'));

        -- Backfill: whatever was already completed stays completed.
        UPDATE notes SET status = 'done' WHERE done_at IS NOT NULL;

        CREATE INDEX idx_notes_status ON notes(status);
        """,
    ),
    (
        3,
        """
        -- The LLM's second pass fired once, at capture, and ended there.
        -- Capturing with no network meant that Note would NEVER be reviewed, and
        -- there was no way to find which ones had been left behind: with no
        -- stamp, "already reviewed" and "never reviewed" were indistinguishable.
        --
        -- NULL in `reviewed_at` is the work queue. `review_attempts` exists so
        -- the queue does not become an infinite loop when a Note always fails —
        -- a dropped network is temporary, a malformed answer is not.
        ALTER TABLE notes ADD COLUMN reviewed_at TEXT;
        ALTER TABLE notes ADD COLUMN review_attempts INTEGER NOT NULL DEFAULT 0;

        -- Backfill: what already exists was captured before this column existed,
        -- and most of it has been through review. Marking it reviewed avoids a
        -- burst of model calls on the first capture after the migration.
        UPDATE notes SET reviewed_at = created_at;

        CREATE INDEX idx_notes_review ON notes(reviewed_at, review_attempts);
        """,
    ),
    (
        4,
        """
        -- Review only touched priority and tags when they were empty, and that
        -- served to "not overwrite what the user wrote". But it also stopped
        -- review from correcting what **it** had decided earlier: in a
        -- review-everything pass, the model's old value looked manual and stayed.
        --
        -- These two columns say WHO decided. A `!high` or `#tag` you typed locks
        -- the field forever; a machine decision is revisable.
        ALTER TABLE notes ADD COLUMN priority_by_user INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE notes ADD COLUMN tags_by_user INTEGER NOT NULL DEFAULT 0;

        -- No backfill to 1: the priorities and tags that exist today were put
        -- there by review, not typed. Marking them as the user's would freeze
        -- exactly what this migration comes to unfreeze.
        """,
    ),
    (
        5,
        """
        -- Deleting for real has no undo, and a note is something a person wrote
        -- in a hurry — misclicking is easy. `deleted_at` is a stamp, not a state:
        -- `cancelled` means "I decided not to do this" and stays in the kanban;
        -- deleted means "I don't want to see this" and leaves everything.
        --
        -- They are independent axes on purpose: you can delete a completed note,
        -- and that distinction would be lost in a sixth `status`.
        ALTER TABLE notes ADD COLUMN deleted_at TEXT;

        CREATE INDEX idx_notes_deleted ON notes(deleted_at);
        """,
    ),
    (
        6,
        """
        -- Priority was the ONLY Portuguese enum in the schema, next to a `status`
        -- that had always been English. They coexisted fine as long as nothing
        -- asked the model for anything in English.
        --
        -- Taking `TA_LANG` to the LLM turns that into a real bug: told to answer
        -- in English it returns "high", the review's validation rejects the value
        -- for not being in the enum, and the priority disappears SILENTLY — no
        -- error, no log. The note comes back from review with no priority and
        -- nobody understands why.
        --
        -- The rule that resolves it: the stored value is canonical and singular;
        -- language is a matter of input and display. `!alta` is still accepted at
        -- capture forever (`PRIORITY_ALIASES`), and the screen shows it in the
        -- user's language.
        UPDATE notes SET priority = 'high'   WHERE priority = 'alta';
        UPDATE notes SET priority = 'medium' WHERE priority = 'media';
        UPDATE notes SET priority = 'low'    WHERE priority = 'baixa';
        """,
    ),
    (
        7,
        """
        -- What each answered model call cost (ADR 0018). The Digest's admin
        -- section reports it (D15), and it is the only way to see whether
        -- routing to the cheaper provider is actually paying off.
        --
        -- `model` is stored, not just `provider`, because a price belongs to a
        -- model and the same provider can serve two at different prices.
        -- `cost_usd` NULL means "no price known for this model" — unknown, which
        -- is not the same as free, and a sum must not treat it as zero.
        CREATE TABLE llm_usage (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          TEXT    NOT NULL,
            provider    TEXT    NOT NULL,
            model       TEXT    NOT NULL,
            task        TEXT    NOT NULL,
            tokens_in   INTEGER NOT NULL,
            tokens_out  INTEGER NOT NULL,
            cost_usd    REAL
        );

        CREATE INDEX idx_llm_usage_at ON llm_usage(at);
        """,
    ),
    (
        8,
        """
        -- Who the bot recognises on a Channel (D21). Identity is the Channel's
        -- numeric id; the username is only how the Owner named them in the
        -- config, and it is kept to notice when it is taken over.
        --
        -- A username can be released and claimed by a stranger. Binding it to
        -- the id on first contact, and never consulting it again, is what stops
        -- that stranger inheriting the Member's access.
        --
        -- This is the seed of `members` (F3), which will absorb it.
        CREATE TABLE channel_identities (
            channel      TEXT NOT NULL,
            external_id  TEXT NOT NULL,
            username     TEXT,
            role         TEXT NOT NULL,     -- 'owner' for now; Grants arrive in F3
            paired_at    TEXT NOT NULL,
            PRIMARY KEY (channel, external_id)
        );

        CREATE UNIQUE INDEX idx_one_owner_per_channel
            ON channel_identities(channel) WHERE role = 'owner';
        """,
    ),
    (
        9,
        """
        -- The people of the household (D6, D12). A Member exists whether or
        -- not they have paired on a Channel: the Owner is born here, with id 1,
        -- so every Note already written has somebody to belong to.
        --
        -- `handle` is how the Owner named them in config.toml — the invite, not
        -- the identity. The identity per Channel stays in `channel_identities`,
        -- which gains the link to the Member it proves.
        CREATE TABLE members (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            handle      TEXT    NOT NULL UNIQUE,
            is_owner    INTEGER NOT NULL DEFAULT 0,
            persona     TEXT,             -- JSON: name to use, tone (D13)
            created_at  TEXT    NOT NULL
        );
        CREATE UNIQUE INDEX idx_one_owner ON members(is_owner) WHERE is_owner = 1;

        INSERT INTO members (id, handle, is_owner, created_at)
        VALUES (1, 'owner', 1, strftime('%Y-%m-%dT%H:%M:%S', 'now', 'localtime'));

        ALTER TABLE channel_identities ADD COLUMN member_id INTEGER REFERENCES members(id);
        UPDATE channel_identities SET member_id = 1 WHERE role = 'owner';

        -- A Note belongs to whoever wrote it. The column cannot be NOT NULL:
        -- SQLite refuses a REFERENCES column with a non-NULL default in ALTER
        -- TABLE while foreign keys are on. So the backfill is here and the
        -- store always writes it; NULL never appears after this migration.
        ALTER TABLE notes ADD COLUMN owner_id INTEGER REFERENCES members(id);
        UPDATE notes SET owner_id = 1;
        CREATE INDEX idx_notes_owner ON notes(owner_id);

        -- A named collection with a scope (D7). The List, not the Note, decides
        -- who sees its items: a household List is seen by every Member.
        CREATE TABLE lists (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT    NOT NULL,
            scope       TEXT    NOT NULL CHECK (scope IN ('household', 'personal')),
            owner_id    INTEGER NOT NULL REFERENCES members(id),
            created_at  TEXT    NOT NULL,
            UNIQUE (name, owner_id)
        );
        ALTER TABLE notes ADD COLUMN list_id INTEGER REFERENCES lists(id) ON DELETE SET NULL;
        -- Who put it in that List: typed/dragged locks it, a model's choice is
        -- revisable — the same rule as `tags_by_user`.
        ALTER TABLE notes ADD COLUMN list_by_user INTEGER NOT NULL DEFAULT 0;
        CREATE INDEX idx_notes_list ON notes(list_id);

        -- Priorities are one person's description of what matters to them.
        ALTER TABLE priorities ADD COLUMN member_id INTEGER REFERENCES members(id);
        UPDATE priorities SET member_id = 1;
        """,
    ),
    (
        10,
        """
        -- Conversation Memory (D13): what was said, per conversation, searchable
        -- only from inside that conversation. It holds both sides — what a
        -- Member wrote and what the bot answered — because "what did you tell me
        -- about X" is a question about the bot's side.
        CREATE TABLE messages (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            channel          TEXT    NOT NULL,
            conversation_id  TEXT    NOT NULL,
            message_id       TEXT,
            member_id        INTEGER REFERENCES members(id),   -- NULL = the bot
            text             TEXT    NOT NULL,
            at               TEXT    NOT NULL
        );
        CREATE INDEX idx_messages_conversation ON messages(channel, conversation_id, at);

        -- What the bot did because of a message (Receipt): the undo, and the
        -- answer to "what did you do here?" (D11, T4.4). `pending` holds an action
        -- waiting for the asker's button (D27); it runs only when pressed.
        CREATE TABLE receipts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            channel          TEXT    NOT NULL,
            conversation_id  TEXT    NOT NULL,
            message_id       TEXT,             -- the Member's message that caused it
            bot_message_id   TEXT,             -- the bot's answer, which a reply may quote
            member_id        INTEGER NOT NULL REFERENCES members(id),
            tool             TEXT    NOT NULL,
            args             TEXT,             -- JSON: what a pending action will run with
            summary          TEXT    NOT NULL,
            undo             TEXT,             -- JSON: how to undo, NULL = cannot
            state            TEXT    NOT NULL DEFAULT 'done'
                             CHECK (state IN ('done', 'pending', 'undone', 'refused')),
            at               TEXT    NOT NULL
        );
        CREATE INDEX idx_receipts_message ON receipts(channel, conversation_id, message_id);
        """,
    ),
    (
        11,
        """
        -- Whose request a model call served, for the per-Member daily ceiling
        -- (D30). NULL for calls made for nobody in particular (organize from the
        -- CLI, the Digest prose). Calls before this migration were all the Owner's.
        ALTER TABLE llm_usage ADD COLUMN member_id INTEGER REFERENCES members(id);
        UPDATE llm_usage SET member_id = 1;
        """,
    ),
]


def _backup_before_migrating(db_path: Path, frm: int, to: int) -> None:
    """Copy the database before the first pending migration runs.

    Migrations are atomic — they either apply whole or not at all — but atomic is
    not reversible: number 6 rewrites priority values, and a successful,
    unwanted `UPDATE` has no way back without a copy. The database holds notes a
    person wrote, and this is far too cheap not to do.

    It only happens when a migration is pending, so it costs nothing on a normal
    boot. A failed backup **prevents** the migration: carrying on without the
    safety net would be the exact opposite of why it exists.
    """
    target = db_path.with_suffix(f"{db_path.suffix}.v{frm}-before-v{to}")
    if target.exists():
        return   # we already migrated from here once; do not overwrite the older copy
    shutil.copy2(db_path, target)
    log.warning("database copied to %s before migrating v%d → v%d", target, frm, to)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open the connection, create the directory if needed, and migrate."""
    db_path = path or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL: the daemon writes while the board reads, without blocking.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    current = conn.execute("PRAGMA user_version").fetchone()[0]
    target = MIGRATIONS[-1][0] if MIGRATIONS else 0
    # A freshly created database has nothing to preserve, and copying an empty
    # file would only produce litter in every test.
    if current and current < target and db_path.exists():
        _backup_before_migrating(db_path, current, target)

    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply the pending migrations. Returns the final version.

    The BEGIN/COMMIT lives inside the script rather than in a
    `with transaction(...)` around it, because `executescript()` emits an
    implicit COMMIT before running the script — opening the transaction in Python
    would make the following COMMIT fail with "cannot commit - no transaction is
    active".

    `user_version` goes into the same script so that migration and version number
    are atomic: either both happen or neither does. And it does not accept a
    bound parameter, hence the interpolation — the value comes from MIGRATIONS,
    never from input.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    for version, sql in MIGRATIONS:
        if version <= current:
            continue
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        current = version
    return current


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """An explicit transaction.

    The connection is in autocommit (`isolation_level=None`), so BEGIN/COMMIT are
    ours. Without this, a write interrupted halfway through several notes would
    leave partial state — which is exactly what motivated choosing SQLite over
    rewriting a whole file on every note.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
