import sqlite3

import pytest

from ta import db


def test_it_migrates_from_scratch(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_migrating_is_idempotent(tmp_path):
    path = tmp_path / "t.db"
    db.connect(path).close()
    conn = db.connect(path)  # a second open must not reapply anything
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_the_roles_coexist_in_one_table(tmp_path):
    """ADR 0006: a plain note, a Task and a Reminder are the same entity."""
    conn = db.connect(tmp_path / "t.db")
    conn.executemany(
        "INSERT INTO notes (text, created_at, due, remind_at) VALUES (?,?,?,?)",
        [
            ("ideia solta", "2026-08-10T09:00", None, None),
            ("ligar dentista", "2026-08-10T09:00", "2026-08-14", None),
            ("tomar remedio", "2026-08-10T09:00", None, "2026-08-10T22:00"),
        ],
    )
    assert conn.execute("SELECT count(*) FROM notes WHERE due IS NOT NULL").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM notes WHERE remind_at IS NOT NULL").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 3


def test_a_tag_cascades_when_the_note_is_deleted(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    cur = conn.execute("INSERT INTO notes (text, created_at) VALUES ('x','2026-08-10T09:00')")
    note_id = cur.lastrowid
    conn.execute("INSERT INTO tags (note_id, tag) VALUES (?, 'saude')", (note_id,))
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    assert conn.execute("SELECT count(*) FROM tags").fetchone()[0] == 0


def test_transaction_rolls_back(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        conn.execute("INSERT INTO notes (text, created_at) VALUES ('a','2026-08-10T09:00')")
        # a note_id that does not exist violates the foreign key, aborting the whole thing
        conn.execute("INSERT INTO tags (note_id, tag) VALUES (9999, 'x')")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0


# ── Migration 6: priority canonical in English ──────────────────────────────
def _database_at_v5(path):
    """A database stuck at v5, with Portuguese priorities — the real state.

    Applying the migrations up to 5 and only then writing is what makes this a
    MIGRATION test rather than a current-schema test: the data goes in through the
    old format.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    for version, sql in db.MIGRATIONS:
        if version > 5:
            break
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
    for text, prio in [
        ("com alta", "alta"), ("com media", "media"),
        ("com baixa", "baixa"), ("sem nada", None),
    ]:
        conn.execute(
            "INSERT INTO notes (text, created_at, priority) VALUES (?, '2026-08-01', ?)",
            (text, prio),
        )
    conn.close()


def test_migration_6_translates_the_existing_priorities(tmp_path):
    path = tmp_path / "t.db"
    _database_at_v5(path)

    conn = db.connect(path)   # opening it is what migrates

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    found = dict(conn.execute("SELECT text, priority FROM notes"))
    assert found == {
        "com alta": "high", "com media": "medium",
        "com baixa": "low", "sem nada": None,
    }


def test_the_migration_loses_no_note(tmp_path):
    """What the migration must NOT do, stated separately.

    An `UPDATE` with the wrong `WHERE` would translate the priorities and drop
    rows without the assertion above noticing.
    """
    path = tmp_path / "t.db"
    _database_at_v5(path)
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 4


def test_the_database_is_copied_before_migrating(tmp_path):
    """A migration is atomic, but atomic is not reversible.

    Number 6 rewrites values; a successful and unwanted UPDATE has no way back
    without a copy, and the file holds notes somebody actually wrote.
    """
    path = tmp_path / "t.db"
    _database_at_v5(path)
    db.connect(path).close()

    copies = list(tmp_path.glob("t.db.v5-before-v*"))
    assert len(copies) == 1, f"expected one copy, found {copies}"

    # The copy is the OLD database, not another name for the new one.
    old = sqlite3.connect(copies[0])
    assert old.execute("PRAGMA user_version").fetchone()[0] == 5
    assert old.execute(
        "SELECT priority FROM notes WHERE text = 'com alta'"
    ).fetchone()[0] == "alta"


def test_a_fresh_database_produces_no_copy(tmp_path):
    """Copying a freshly created database would only litter every test and every boot."""
    db.connect(tmp_path / "fresh.db").close()
    assert list(tmp_path.glob("*before-v*")) == []
