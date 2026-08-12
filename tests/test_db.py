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


# ── Migração 6: prioridade canônica em inglês ───────────────────────────────
def _banco_na_v5(path):
    """Um banco parado na v5, com prioridades em português — o estado real.

    Aplicar as migrações até a 5 e só então escrever é o que torna isto um teste
    de MIGRAÇÃO e não de schema atual: os dados entram pelo formato antigo.
    """
    conn = sqlite3.connect(path, isolation_level=None)
    for versao, sql in db.MIGRATIONS:
        if versao > 5:
            break
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {versao};\nCOMMIT;")
    for texto, prio in [
        ("com alta", "alta"), ("com media", "media"),
        ("com baixa", "baixa"), ("sem nada", None),
    ]:
        conn.execute(
            "INSERT INTO notes (text, created_at, priority) VALUES (?, '2026-08-01', ?)",
            (texto, prio),
        )
    conn.close()


def test_a_migracao_6_traduz_as_prioridades_existentes(tmp_path):
    path = tmp_path / "t.db"
    _banco_na_v5(path)

    conn = db.connect(path)   # é a abertura que migra

    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    achado = dict(conn.execute("SELECT text, priority FROM notes"))
    assert achado == {
        "com alta": "high", "com media": "medium",
        "com baixa": "low", "sem nada": None,
    }


def test_a_migracao_nao_perde_nota(tmp_path):
    """O que a migração NÃO pode fazer, dito em separado.

    Um `UPDATE` com `WHERE` errado traduziria as prioridades e apagaria linhas
    sem que o assert acima notasse.
    """
    path = tmp_path / "t.db"
    _banco_na_v5(path)
    conn = db.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 4


def test_o_banco_e_copiado_antes_de_migrar(tmp_path):
    """Migração é atômica, mas atômico não é reversível.

    A 6 reescreve valores; um UPDATE bem-sucedido e indesejado não tem volta sem
    cópia, e o arquivo tem notas que a pessoa escreveu.
    """
    path = tmp_path / "t.db"
    _banco_na_v5(path)
    db.connect(path).close()

    copias = list(tmp_path.glob("t.db.v5-before-v*"))
    assert len(copias) == 1, f"esperava uma cópia, achei {copias}"

    # A cópia é o banco ANTIGO, não outro nome para o novo.
    antigo = sqlite3.connect(copias[0])
    assert antigo.execute("PRAGMA user_version").fetchone()[0] == 5
    assert antigo.execute(
        "SELECT priority FROM notes WHERE text = 'com alta'"
    ).fetchone()[0] == "alta"


def test_banco_novo_nao_gera_copia(tmp_path):
    """Copiar num banco recém-criado só geraria lixo em todo teste e todo boot."""
    db.connect(tmp_path / "novo.db").close()
    assert list(tmp_path.glob("*before-v*")) == []
