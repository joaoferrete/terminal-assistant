import sqlite3

import pytest

from ta import db


def test_migra_do_zero(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_migrar_e_idempotente(tmp_path):
    path = tmp_path / "t.db"
    db.connect(path).close()
    conn = db.connect(path)  # segunda abertura não deve reaplicar
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_papeis_convivem_na_mesma_tabela(tmp_path):
    """ADR 0006: nota solta, Task e Reminder são a mesma entidade."""
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


def test_tag_cascateia_ao_deletar_nota(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    cur = conn.execute("INSERT INTO notes (text, created_at) VALUES ('x','2026-08-10T09:00')")
    note_id = cur.lastrowid
    conn.execute("INSERT INTO tags (note_id, tag) VALUES (?, 'saude')", (note_id,))
    conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
    assert conn.execute("SELECT count(*) FROM tags").fetchone()[0] == 0


def test_transaction_faz_rollback(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        conn.execute("INSERT INTO notes (text, created_at) VALUES ('a','2026-08-10T09:00')")
        # note_id inexistente viola a foreign key, abortando a transação inteira
        conn.execute("INSERT INTO tags (note_id, tag) VALUES (9999, 'x')")
    assert conn.execute("SELECT count(*) FROM notes").fetchone()[0] == 0
