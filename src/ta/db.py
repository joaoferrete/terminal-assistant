"""Persistência: SQLite via stdlib, sem ORM.

O modelo é o do ADR 0006: uma única tabela `notes`. Os papéis (Task, Reminder,
concluída) vêm da presença de atributos, não de uma coluna de tipo.

A ordem e a posição no mural são dados gravados, não cálculo. É isso que faz a
mão do usuário vencer o LLM de forma permanente (ADR 0003).
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 5

# Estados de uma Note. Guardados em inglês porque o resto do vocabulário é
# (ver CONTEXT.md); os rótulos em português vivem na interface.
# `done` e `cancelled` são terminais: a Note saiu da fila, por caminhos
# diferentes — uma foi feita, a outra não vai ser.
STATUSES = ("todo", "doing", "hold", "done", "cancelled")
TERMINAL_STATUSES = ("done", "cancelled")


def default_db_path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ta" / "ta.db"


# Cada migração é (versão, sql). Aplicadas em ordem, uma vez, controladas por
# `PRAGMA user_version`. Nunca editar uma migração já lançada — acrescentar outra.
MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        -- Uma entidade só. `due` a torna Task, `remind_at` a torna Reminder,
        -- `done_at` a conclui. Ver ADR 0006.
        CREATE TABLE notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            text        TEXT    NOT NULL,
            created_at  TEXT    NOT NULL,
            due         TEXT,             -- data (YYYY-MM-DD): papel Task
            remind_at   TEXT,             -- instante ISO: papel Reminder
            fired_at    TEXT,             -- quando o Reminder disparou; nulo = pendente
            done_at     TEXT,             -- conclusão
            priority    TEXT,             -- alta | media | baixa
            group_name  TEXT,             -- agrupamento (do usuário ou do LLM)
            sort_key    REAL   NOT NULL DEFAULT 0,   -- ordem gravada
            pos_x       INTEGER,          -- posição no mural, quando arrastada
            pos_y       INTEGER,
            color       TEXT,
            pinned_by_user INTEGER NOT NULL DEFAULT 0  -- 1 = o LLM não reordena
        );

        CREATE INDEX idx_notes_due       ON notes(due)       WHERE done_at IS NULL;
        CREATE INDEX idx_notes_remind    ON notes(remind_at) WHERE fired_at IS NULL;
        CREATE INDEX idx_notes_open      ON notes(done_at);

        CREATE TABLE tags (
            note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
            tag     TEXT    NOT NULL,
            PRIMARY KEY (note_id, tag)
        );

        -- Vínculo com evento criado na agenda dedicada. Guardamos só o
        -- identificador: a agenda é a fonte de verdade (ADR 0004).
        CREATE TABLE calendar_links (
            note_id     INTEGER PRIMARY KEY REFERENCES notes(id) ON DELETE CASCADE,
            uid         TEXT NOT NULL,
            source_uid  TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );

        -- Priorities: o markdown editável por prompt. Uma linha, versionada por
        -- histórico para que "prioriza estudo acima de trabalho" seja auditável.
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
        -- Kanban precisa de estado de verdade, e `done_at IS NOT NULL` só sabia
        -- dizer sim ou não. `cancelled` é o caso que prova a necessidade: uma
        -- Note cancelada saiu da fila sem ter sido feita, e o modelo binário não
        -- tinha como representar isso.
        --
        -- `done_at` NÃO é substituído: continua sendo o instante em que a Note
        -- entrou em `done`. Estado e carimbo de tempo são coisas diferentes.
        ALTER TABLE notes ADD COLUMN status TEXT NOT NULL DEFAULT 'todo'
            CHECK (status IN ('todo','doing','hold','done','cancelled'));

        -- Backfill: o que já estava concluído continua concluído.
        UPDATE notes SET status = 'done' WHERE done_at IS NOT NULL;

        CREATE INDEX idx_notes_status ON notes(status);
        """,
    ),
    (
        3,
        """
        -- A segunda passada do LLM disparava uma vez, na captura, e acabava ali.
        -- Capturar sem rede significava que aquela Note NUNCA seria revisada, e
        -- não havia como descobrir quais tinham ficado para trás: sem carimbo,
        -- "já foi revisada" e "nunca foi" eram indistinguíveis.
        --
        -- NULL em `reviewed_at` é a fila de trabalho. `review_attempts` existe
        -- para a fila não virar laço infinito quando uma Note falha sempre —
        -- rede caída é temporário, resposta malformada não é.
        ALTER TABLE notes ADD COLUMN reviewed_at TEXT;
        ALTER TABLE notes ADD COLUMN review_attempts INTEGER NOT NULL DEFAULT 0;

        -- Backfill: o que já existe foi capturado antes desta coluna existir, e
        -- em boa parte já passou pela revisão. Marcar como revisado evita uma
        -- rajada de chamadas de modelo na primeira captura após a migração.
        UPDATE notes SET reviewed_at = created_at;

        CREATE INDEX idx_notes_review ON notes(reviewed_at, review_attempts);
        """,
    ),
    (
        4,
        """
        -- A revisão só mexia em prioridade e tags quando estavam vazias, e isso
        -- servia para "não sobrescrever o que o usuário escreveu". Mas ela também
        -- impedia a revisão de corrigir o que **ela mesma** decidiu antes: num
        -- `revisar tudo`, o valor antigo do modelo parecia manual e ficava.
        --
        -- Estas duas colunas dizem QUEM decidiu. `!alta` e `#tag` escritos por
        -- você travam o campo para sempre; decisão de máquina é revisável.
        ALTER TABLE notes ADD COLUMN priority_by_user INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE notes ADD COLUMN tags_by_user INTEGER NOT NULL DEFAULT 0;

        -- Sem backfill para 1: as prioridades e tags que existem hoje foram
        -- postas pela revisão, não digitadas. Marcá-las como do usuário
        -- congelaria justamente o que esta migração vem destravar.
        """,
    ),
    (
        5,
        """
        -- Apagar de verdade não tem volta, e nota é coisa que a pessoa escreveu
        -- às pressas — errar o clique é fácil. `deleted_at` é um carimbo, não um
        -- estado: `cancelled` significa "decidi não fazer" e continua no kanban;
        -- apagada significa "não quero mais ver isto" e sai de tudo.
        --
        -- São eixos independentes de propósito: dá para apagar uma nota
        -- concluída, e a distinção se perderia num sexto `status`.
        ALTER TABLE notes ADD COLUMN deleted_at TEXT;

        CREATE INDEX idx_notes_deleted ON notes(deleted_at);
        """,
    ),
]


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Abre a conexão, cria o diretório se preciso, e migra."""
    db_path = path or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL: o daemon escreve enquanto o mural lê, sem bloquear.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Aplica as migrações pendentes. Devolve a versão final.

    O BEGIN/COMMIT fica dentro do script, e não num `with transaction(...)` em
    volta, porque `executescript()` emite um COMMIT implícito antes de rodar o
    script — abrir a transação em Python faria o COMMIT seguinte falhar com
    "cannot commit - no transaction is active".

    `user_version` entra no mesmo script para que migração e número de versão
    sejam atômicos: ou os dois acontecem, ou nenhum. E não aceita parâmetro
    ligado, daí a interpolação — o valor vem de MIGRATIONS, nunca de entrada.
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
    """Transação explícita.

    A conexão está em autocommit (`isolation_level=None`), então BEGIN/COMMIT
    são nossos. Sem isso, uma escrita interrompida no meio de várias notas
    deixaria estado parcial — que é exatamente o que motivou escolher SQLite em
    vez de reescrever um arquivo inteiro a cada nota.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
