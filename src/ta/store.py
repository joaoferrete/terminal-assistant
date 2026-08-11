"""Operações sobre Notes. A camada entre o parser e o SQLite.

Não sabe nada de HTTP nem de LLM. Isso é de propósito: é o que permite testar a
captura inteira sem subir o daemon.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .db import STATUSES, TERMINAL_STATUSES, transaction
from .notes import ParsedNote, parse

# Desempate DENTRO de uma faixa de horizonte — não é mais o primeiro critério de
# exibição (ver `by_urgency`). Sem prioridade vem depois de baixa: não declarar
# não é o mesmo que declarar baixa.
PRIORITY_RANK = {"alta": 0, "media": 1, "baixa": 2, None: 3}

# O Horizon de uma Task: a faixa de tempo em que o prazo dela cai, medida contra
# hoje. É **derivado do relógio, nunca gravado** — a mesma nota muda de faixa à
# meia-noite sem ninguém escrever nada, e é justamente por isso que não pode
# morar em `sort_key` (ADR 0010).
HORIZONS = ("vencida", "hoje", "semana", "depois")
HORIZON_RANK = {h: i for i, h in enumerate(HORIZONS)}

# Janela **rolante**, não semana do calendário: numa sexta a semana do calendário
# está quase vazia e a tarefa de segunda cairia em `depois`.
DIAS_DE_SEMANA = 7


@dataclass
class Note:
    id: int
    text: str
    created_at: str
    due: str | None
    remind_at: str | None
    fired_at: str | None
    done_at: str | None
    priority: str | None
    group_name: str | None
    sort_key: float
    pos_x: int | None
    pos_y: int | None
    color: str | None
    pinned_by_user: bool
    status: str
    tags: list[str]
    # Quem decidiu prioridade e tags. `!alta`/`#tag` digitados travam o campo; o
    # que a revisão pôs ela mesma pode rever depois.
    priority_by_user: bool = False
    tags_by_user: bool = False
    deleted_at: str | None = None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_task(self) -> bool:
        return self.due is not None

    @property
    def is_reminder(self) -> bool:
        return self.remind_at is not None

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    @property
    def is_terminal(self) -> bool:
        """Saiu da fila — feita ou cancelada. Caminhos diferentes, fila igual."""
        return self.status in TERMINAL_STATUSES


def _row_to_note(row: sqlite3.Row, tags: list[str]) -> Note:
    return Note(
        id=row["id"],
        text=row["text"],
        created_at=row["created_at"],
        due=row["due"],
        remind_at=row["remind_at"],
        fired_at=row["fired_at"],
        done_at=row["done_at"],
        priority=row["priority"],
        group_name=row["group_name"],
        sort_key=row["sort_key"],
        pos_x=row["pos_x"],
        pos_y=row["pos_y"],
        color=row["color"],
        priority_by_user=bool(row["priority_by_user"]),
        tags_by_user=bool(row["tags_by_user"]),
        deleted_at=row["deleted_at"],
        pinned_by_user=bool(row["pinned_by_user"]),
        status=row["status"],
        tags=tags,
    )


def add_note(conn: sqlite3.Connection, raw: str, *, now: datetime | None = None) -> Note:
    """Captura uma Note. Determinístico, sem rede (ADR 0003)."""
    now = now or datetime.now()
    p: ParsedNote = parse(raw, now=now)

    # sort_key nasce no fim da fila; `organize` e o arrastar reescrevem depois.
    with transaction(conn):
        next_key = conn.execute("SELECT COALESCE(MAX(sort_key), 0) + 1 FROM notes").fetchone()[0]
        cur = conn.execute(
            """
            INSERT INTO notes (text, created_at, due, remind_at, priority, sort_key,
                               priority_by_user, tags_by_user)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                p.text,
                now.isoformat(timespec="seconds"),
                p.due.isoformat() if p.due else None,
                p.remind_at.isoformat(timespec="seconds") if p.remind_at else None,
                p.priority,
                next_key,
                # Escreveu `!alta` ou `#tag`? Então o campo é seu, e a revisão
                # não o toca nunca — nem numa reetiquetagem futura.
                1 if p.priority else 0,
                1 if p.tags else 0,
            ),
        )
        note_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO tags (note_id, tag) VALUES (?, ?)",
            [(note_id, t) for t in p.tags],
        )

    return get_note(conn, note_id)


def get_note(conn: sqlite3.Connection, note_id: int) -> Note:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        raise KeyError(f"nota {note_id} não existe")
    tags = [r["tag"] for r in conn.execute("SELECT tag FROM tags WHERE note_id = ?", (note_id,))]
    return _row_to_note(row, sorted(tags))


def list_notes(
    conn: sqlite3.Connection, *, include_done: bool = False, deleted: bool = False
) -> list[Note]:
    """Lista as Notes. Por padrão esconde as terminais e as apagadas.

    `deleted=True` inverte o filtro e devolve **só** as apagadas — é a lixeira,
    não um "inclui também".
    """
    onde = ["deleted_at IS NOT NULL"] if deleted else ["deleted_at IS NULL"]
    params: list = []
    if not include_done and not deleted:
        onde.append(f"status NOT IN ({','.join('?' * len(TERMINAL_STATUSES))})")
        params += list(TERMINAL_STATUSES)
    sql = f"SELECT * FROM notes WHERE {' AND '.join(onde)} ORDER BY sort_key"
    rows = list(conn.execute(sql, params))
    if not rows:
        return []

    # Uma query para todas as tags, em vez de N+1.
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" * len(ids))
    tag_map: dict[int, list[str]] = {i: [] for i in ids}
    for r in conn.execute(f"SELECT note_id, tag FROM tags WHERE note_id IN ({placeholders})", ids):
        tag_map[r["note_id"]].append(r["tag"])

    return [_row_to_note(r, sorted(tag_map[r["id"]])) for r in rows]


def due_today(conn: sqlite3.Connection, *, today: date | None = None) -> list[Note]:
    """Tasks cobráveis hoje ou já vencidas. Base do Digest."""
    today = today or date.today()
    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    rows = list(
        conn.execute(
            f"SELECT * FROM notes WHERE deleted_at IS NULL"
            f" AND status NOT IN ({placeholders})"
            " AND due IS NOT NULL AND due <= ? ORDER BY due, sort_key",
            (*TERMINAL_STATUSES, today.isoformat()),
        )
    )
    return [_row_to_note(r, []) for r in rows]


def pending_reminders(conn: sqlite3.Connection, *, now: datetime | None = None) -> list[Note]:
    """Reminders com hora vencida e ainda não disparados."""
    now = now or datetime.now()
    rows = list(
        conn.execute(
            "SELECT * FROM notes WHERE deleted_at IS NULL"
            " AND fired_at IS NULL AND remind_at IS NOT NULL"
            " AND remind_at <= ? ORDER BY remind_at",
            (now.isoformat(timespec="seconds"),),
        )
    )
    return [_row_to_note(r, []) for r in rows]


def mark_fired(conn: sqlite3.Connection, note_id: int, *, now: datetime | None = None) -> None:
    now = now or datetime.now()
    conn.execute(
        "UPDATE notes SET fired_at = ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), note_id),
    )


def set_schedule(
    conn: sqlite3.Connection,
    note_id: int,
    *,
    due: date | None,
    remind_at: datetime | None,
) -> None:
    """Reescreve prazo e lembrete — inclusive para **nenhum**.

    Existe para a segunda passada do LLM poder desfazer o que o regex marcou.
    Diferente de `move_note`, aqui `None` significa "apaga", não "não mexe": a
    correção mais importante que essa função faz é justamente tirar o prazo de
    uma frase retrospectiva.

    Não marca `pinned_by_user` — isto é decisão de máquina, não da sua mão.
    """
    conn.execute(
        "UPDATE notes SET due = ?, remind_at = ? WHERE id = ?",
        (
            due.isoformat() if due else None,
            remind_at.isoformat(timespec="minutes") if remind_at else None,
            note_id,
        ),
    )


def set_tags(conn: sqlite3.Connection, note_id: int, tags: list[str]) -> None:
    """Reescreve as tags. Usado pela revisão quando você não escreveu nenhuma."""
    conn.execute("DELETE FROM tags WHERE note_id = ?", (note_id,))
    conn.executemany(
        "INSERT INTO tags (note_id, tag) VALUES (?, ?)",
        [(note_id, t) for t in sorted(set(tags))],
    )


def set_priority(conn: sqlite3.Connection, note_id: int, priority: str | None) -> None:
    conn.execute("UPDATE notes SET priority = ? WHERE id = ?", (priority, note_id))


def mark_reviewed(conn: sqlite3.Connection, note_id: int, *, now: datetime | None = None) -> None:
    """Sai da fila de revisão."""
    now = now or datetime.now()
    conn.execute(
        "UPDATE notes SET reviewed_at = ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), note_id),
    )


def count_review_attempt(conn: sqlite3.Connection, note_id: int) -> int:
    """Registra uma tentativa e devolve o total. Fila com teto, não laço."""
    conn.execute(
        "UPDATE notes SET review_attempts = review_attempts + 1 WHERE id = ?", (note_id,)
    )
    return conn.execute(
        "SELECT review_attempts FROM notes WHERE id = ?", (note_id,)
    ).fetchone()[0]


def soft_delete(conn: sqlite3.Connection, note_id: int, *, now: datetime | None = None) -> None:
    """Some da lixeira para fora. Reversível de propósito.

    Nota é coisa escrita às pressas, e errar o clique é fácil — apagar de verdade
    não teria volta. O evento na agenda, se houver, **não** é removido: apagar a
    anotação sobre um compromisso não desmarca o compromisso.
    """
    now = now or datetime.now()
    conn.execute(
        "UPDATE notes SET deleted_at = ? WHERE id = ?",
        (now.isoformat(timespec="seconds"), note_id),
    )


def restore(conn: sqlite3.Connection, note_id: int) -> None:
    conn.execute("UPDATE notes SET deleted_at = NULL WHERE id = ?", (note_id,))


def purge(conn: sqlite3.Connection, note_id: int) -> bool:
    """Apaga em definitivo. **Só alcança o que já está na lixeira.**

    Essa restrição é a segurança inteira desta função: não existe caminho que
    pule o soft delete, então nada é destruído sem ter passado por um estado
    recuperável antes. Devolve se algo foi apagado.

    As `tags` e o vínculo em `calendar_links` saem por `ON DELETE CASCADE`. O
    evento na agenda **não** sai — apagar a anotação sobre um compromisso não
    desmarca o compromisso, e isto vale mais ainda aqui, onde não há volta.
    """
    cur = conn.execute(
        "DELETE FROM notes WHERE id = ? AND deleted_at IS NOT NULL", (note_id,)
    )
    return cur.rowcount > 0


def purge_all(conn: sqlite3.Connection) -> int:
    """Esvazia a lixeira. Devolve quantas foram."""
    return conn.execute("DELETE FROM notes WHERE deleted_at IS NOT NULL").rowcount


def queue_all_for_review(conn: sqlite3.Connection) -> int:
    """Devolve TODAS as Notes não terminais para a fila de revisão.

    Zera `review_attempts` de propósito: um pedido explícito do usuário é uma
    ordem nova, não a continuação de tentativas antigas que já desistiram.
    Concluída e cancelada ficam de fora — revisar prazo de coisa encerrada é
    gastar chamada de modelo para nada.
    """
    placeholders = ",".join("?" * len(TERMINAL_STATUSES))
    cur = conn.execute(
        f"UPDATE notes SET reviewed_at = NULL, review_attempts = 0"
        f" WHERE deleted_at IS NULL AND status NOT IN ({placeholders})",
        TERMINAL_STATUSES,
    )
    return cur.rowcount


def pending_review(
    conn: sqlite3.Connection, *, max_attempts: int = 5, limit: int = 10
) -> list[int]:
    """Notes que nunca foram revisadas, mais antigas primeiro.

    `limit` existe para uma semana offline não virar uma rajada de chamadas de
    modelo na primeira captura com rede. A fila drena aos poucos, e a ordem por
    id garante que ninguém fica para trás para sempre.
    """
    return [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM notes WHERE deleted_at IS NULL"
            " AND reviewed_at IS NULL AND review_attempts < ?"
            " ORDER BY id LIMIT ?",
            (max_attempts, limit),
        )
    ]


def set_status(
    conn: sqlite3.Connection, note_id: int, status: str, *, now: datetime | None = None
) -> None:
    """Muda o estado da Note.

    `done_at` é mantido em sincronia como *carimbo de tempo*, não como estado:
    entrar em `done` grava o instante, sair limpa. Cancelar não grava `done_at` —
    a Note não foi feita.
    """
    if status not in STATUSES:
        raise ValueError(f"status inválido: {status!r}. Válidos: {', '.join(STATUSES)}")
    now = now or datetime.now()
    done_at = now.isoformat(timespec="seconds") if status == "done" else None
    conn.execute(
        "UPDATE notes SET status = ?, done_at = ? WHERE id = ?", (status, done_at, note_id)
    )


def mark_done(conn: sqlite3.Connection, note_id: int, *, now: datetime | None = None) -> None:
    set_status(conn, note_id, "done", now=now)


def mark_undone(conn: sqlite3.Connection, note_id: int) -> None:
    """Desmarcar é tão importante quanto marcar: clique errado acontece."""
    set_status(conn, note_id, "todo")


def horizon(due: str | None, *, today: date | None = None) -> str:
    """A faixa de tempo de um prazo, contada de hoje. Um de `HORIZONS`.

    Sem prazo cai em `depois`, junto do que é para muito longe. Isso é escolha,
    não descuido: numa faixa própria no fim, uma nota `!alta` sem data ficaria
    atrás de uma `!baixa` que vence em setembro — e o que não tem data marcada
    não é, por isso, menos importante que o futuro distante.
    """
    if due is None:
        return "depois"
    today = today or date.today()
    dia = date.fromisoformat(due)
    if dia < today:
        return "vencida"
    if dia == today:
        return "hoje"
    if dia <= today + timedelta(days=DIAS_DE_SEMANA):
        return "semana"
    return "depois"


def by_urgency(notes: list[Note], *, today: date | None = None) -> list[Note]:
    """Ordem de exibição: o relógio decide a faixa, o resto decide dentro dela.

    O prazo domina a prioridade — uma `!baixa` que vence hoje vem antes de uma
    `!alta` que vence em três dias, porque a de hoje é a que precisa ser feita
    hoje (ADR 0010). A prioridade não perdeu valor, mudou de escopo: ela ordena
    dentro da faixa.
    """
    today = today or date.today()
    return sorted(
        notes,
        key=lambda n: (
            # Fora da fila, sempre no fim. Este termo vem ANTES do horizonte de
            # propósito: sem isso, uma nota concluída na semana passada — prazo no
            # passado, logo `vencida` — subiria para o topo do quadro.
            n.is_terminal,
            HORIZON_RANK[horizon(n.due, today=today)],
            PRIORITY_RANK.get(n.priority, 3),
            # Com prazo antes de sem prazo. Sem este termo, `n.due or ""` mapeia
            # a nota sem data para `""`, que ordena antes de qualquer data ISO, e
            # dentro de `depois` as ideias soltas passariam na frente das tarefas
            # datadas.
            n.due is None,
            n.due or "",
            # E por fim o que o `organize` gravou, ou a ordem de captura.
            n.sort_key,
        ),
    )


def move_note(
    conn: sqlite3.Connection,
    note_id: int,
    *,
    sort_key: float | None = None,
    pos_x: int | None = None,
    pos_y: int | None = None,
    group_name: str | None = None,
    color: str | None = None,
) -> None:
    """Reordenar/arrastar/colorir à mão.

    Cor nula no banco não é ausência de cor: significa "usa o padrão da
    prioridade", derivado na hora de desenhar. Só o que a mão escolheu fica
    gravado, e é por isso que mudar a prioridade recolore uma nota nunca tocada e
    não recolore uma que você pintou.

    Marca `pinned_by_user`, que é o que faz um `organize` posterior respeitar a
    decisão do usuário em vez de desfazê-la (ADR 0003).
    """
    sets, params = ["pinned_by_user = 1"], []
    for column, value in (
        ("sort_key", sort_key),
        ("pos_x", pos_x),
        ("pos_y", pos_y),
        ("group_name", group_name),
        ("color", color),
    ):
        # `color=""` pede a volta ao padrão da prioridade, o que é diferente de
        # `color=None`, que quer dizer "não mexe na cor".
        if column == "color" and value == "":
            sets.append("color = NULL")
            continue
        if value is not None:
            sets.append(f"{column} = ?")
            params.append(value)
    params.append(note_id)
    conn.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id = ?", params)


STATUS_MARK = {"todo": "[ ]", "doing": "[~]", "hold": "[-]", "done": "[x]", "cancelled": "[/]"}


def export_markdown(conn: sqlite3.Connection) -> str:
    """Válvula de escape da escolha de SQLite: despeja tudo em markdown."""
    lines = ["# Notas", ""]
    for n in by_urgency(list_notes(conn, include_done=True)):
        box = STATUS_MARK[n.status]
        bits = []
        if n.status not in ("todo", "done"):
            bits.append(n.status)
        if n.due:
            bits.append(f"prazo {n.due}")
        if n.remind_at:
            bits.append(f"lembra {n.remind_at}")
        if n.priority:
            bits.append(f"prio {n.priority}")
        if n.tags:
            bits.append(" ".join(f"#{t}" for t in n.tags))
        suffix = f"  _({', '.join(bits)})_" if bits else ""
        lines.append(f"- {box} {n.text}{suffix}")
    return "\n".join(lines) + "\n"
