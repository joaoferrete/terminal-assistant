"""Priorities: a descrição, mantida pelo usuário, do que importa para ele.

Sem isso, "prioridade" é chute — foi o motivo de o próprio usuário pedir a
entrevista inicial. É markdown, não formulário: mais expressivo, e editável por
prompt ("prioriza estudo acima de trabalho" reescreve o arquivo).

Guardado no SQLite com histórico, para que uma reescrita por prompt seja
auditável em vez de destrutiva.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .db import transaction

# As perguntas da primeira execução. Poucas de propósito: um questionário longo
# não é respondido.
PERGUNTAS = [
    ("trabalho", "O que você faz, e em que contexto? (ex: backend numa empresa de telemetria)"),
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
    """Grava uma nova versão. Nunca sobrescreve: o histórico é a auditoria."""
    now = now or datetime.now()
    with transaction(conn):
        conn.execute(
            "INSERT INTO priorities (content, created_at) VALUES (?, ?)",
            (content.strip(), now.isoformat(timespec="seconds")),
        )


def from_answers(respostas: dict[str, str]) -> str:
    return TEMPLATE.format(
        **{chave: (respostas.get(chave) or "(não informado)").strip() for chave, _ in PERGUNTAS}
    )


async def rewrite(conn: sqlite3.Connection, llm, instrucao: str) -> str:
    """Reescreve o markdown a partir de uma instrução em linguagem natural.

    Uma nova versão é gravada; a anterior fica no histórico. É o "alterável via
    prompt" pedido, sem virar destrutivo.
    """
    from .llm import Prose

    atual = current(conn) or "# Prioridades\n\n(vazio)\n"
    novo = await llm._structured(
        prompt=(
            f"Documento atual de prioridades:\n\n{atual}\n\n"
            f"Instrução da pessoa: {instrucao}\n\n"
            "Devolva o documento inteiro reescrito em markdown, aplicando a "
            "instrução e preservando tudo que ela não pediu para mudar."
        ),
        schema=Prose,
        system=(
            "Você mantém o documento de prioridades de alguém. Edite com "
            "parcimônia: aplique o que foi pedido e não reescreva o resto."
        ),
    )
    save(conn, novo.text)
    return novo.text
