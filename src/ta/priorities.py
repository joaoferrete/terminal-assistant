"""Priorities: the user's own description of what matters to them.

Without it, "priority" is a guess — which is why the initial interview exists at
all. It is markdown, not a form: more expressive, and editable by prompt
("put study above work" rewrites the document).

Stored in SQLite with history, so that a rewrite by prompt is auditable rather
than destructive.

KNOWN GAP: the interview questions and the document template are still
Portuguese, regardless of `TA_LANG`. They were left alone deliberately — the
template's headings end up **inside the stored document**, so translating them
would make an existing profile inconsistent with a new one, and the fix needs to
decide what happens to documents already written. It is a data question, not a
translation one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .db import transaction

# The questions for the first run. Few on purpose: a long questionnaire does not
# get answered.
QUESTIONS = [
    ("trabalho", "O que você faz, e em que contexto? (ex: backend numa fintech)"),
    ("importa", "O que você não pode deixar cair, mesmo numa semana ruim?"),
    ("adia", "O que costuma ficar para depois e você preferia que não ficasse?"),
    ("ritmo", "Como é o seu dia típico? Muitas reuniões, blocos longos, plantão?"),
]

TEMPLATE = """# Prioridades

## Trabalho
{trabalho}

## O que não pode cair
{importa}

## O que costuma ser adiado
{adia}

## Ritmo do dia
{ritmo}
"""


def current(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT content FROM priorities ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row["content"] if row else None


def history(conn: sqlite3.Connection, limit: int = 10) -> list[tuple[str, str]]:
    return [
        (r["created_at"], r["content"])
        for r in conn.execute(
            "SELECT created_at, content FROM priorities ORDER BY id DESC LIMIT ?", (limit,)
        )
    ]


def save(conn: sqlite3.Connection, content: str, *, now: datetime | None = None) -> None:
    """Store a new version. Never overwrites: the history is the audit trail."""
    now = now or datetime.now()
    with transaction(conn):
        conn.execute(
            "INSERT INTO priorities (content, created_at) VALUES (?, ?)",
            (content.strip(), now.isoformat(timespec="seconds")),
        )


def from_answers(answers: dict[str, str]) -> str:
    return TEMPLATE.format(
        **{key: (answers.get(key) or "(não informado)").strip() for key, _ in QUESTIONS}
    )


async def rewrite(conn: sqlite3.Connection, llm, instruction: str) -> str:
    """Rewrite the markdown from a plain-language instruction.

    A new version is stored; the previous one stays in the history. It is the
    "editable by prompt" that was asked for, without becoming destructive.
    """
    from .llm import Prose, for_task

    existing = current(conn) or "# Prioridades\n\n(vazio)\n"
    with for_task("priorities"):
        updated = await llm._structured(
            prompt=(
                f"Documento atual de prioridades:\n\n{existing}\n\n"
                f"Instrução da pessoa: {instruction}\n\n"
                "Devolva o documento inteiro reescrito em markdown, aplicando a "
                "instrução e preservando tudo que ela não pediu para mudar."
            ),
            schema=Prose,
            system=(
                "Você mantém o documento de prioridades de alguém. Edite com "
                "parcimônia: aplique o que foi pedido e não reescreva o resto."
            ),
        )
    save(conn, updated.text)
    return updated.text
