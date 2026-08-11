"""Semeia um banco de demonstração, em inglês, num diretório temporário.

Existe por dois motivos.

O primeiro é as imagens do README: as notas reais do autor citam nome de colega e
conteúdo de trabalho, e limpar isso à mão só funciona enquanto ele lembrar. Aqui
os dados são fictícios por construção, e a screenshot pode ser regerada quando a
interface mudar em vez de envelhecer numa pasta.

O segundo é experimentar. `make demo` sobe um daemon isolado, com banco próprio,
sem tocar no banco de verdade — dá para clicar em tudo, apagar tudo, e fechar.

A seleção cobre de propósito as quatro faixas de Horizon, as cinco colunas do
kanban, as três prioridades e uma nota arrastada à mão, porque uma tela de
demonstração que só mostra o caso feliz não mostra o produto.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ta.db import connect  # noqa: E402
from ta.store import add_note, move_note, set_status  # noqa: E402

HOJE = date.today()


def d(dias: int) -> str:
    return (HOJE + timedelta(days=dias)).isoformat()


# (texto, prazo, status, prioridade, tags)
NOTAS = [
    # `vencida` — a faixa que o quadro existe para mostrar primeiro.
    ("Reply to the landlord about the lease", d(-3), "todo", "high", ["personal"]),
    ("Rotate the staging API keys", d(-1), "doing", "high", ["work"]),
    # `hoje`
    ("Review the pull request from the data team", d(0), "todo", "high", ["work"]),
    ("Pick up the dry cleaning", d(0), "todo", "low", ["personal", "errand"]),
    ("Write the incident postmortem", d(0), "doing", "medium", ["work"]),
    # `semana` — janela rolante de 7 dias
    ("Book the dentist appointment", d(2), "todo", "medium", ["health"]),
    ("Prepare slides for the quarterly review", d(4), "hold", "high", ["work"]),
    ("Renew the domain before it lapses", d(6), "todo", "medium", ["personal"]),
    # `depois`, e o que não tem prazo nenhum — moram juntos de propósito
    ("Plan the trip in September", d(40), "todo", "low", ["personal"]),
    ("Idea: a rule that dims the lights during calls", None, "todo", "medium", ["ideas"]),
    ("Read the paper on CRDTs someone linked", None, "todo", None, ["reading"]),
    # Fora da fila: os dois caminhos de saída são diferentes, e o kanban mostra isso.
    ("Ship the board redesign", d(-2), "done", "high", ["work"]),
    ("Migrate the blog to a static site", d(-10), "cancelled", "low", ["personal"]),
]


def semear(db_path: Path) -> int:
    conn = connect(db_path)
    for i, (texto, prazo, status, prio, tags) in enumerate(NOTAS):
        # Escrito pelo caminho normal, e não por INSERT cru: assim a demo exercita
        # o mesmo parser e as mesmas regras que o uso real, e uma quebra nele
        # aparece aqui antes de aparecer para alguém.
        n = add_note(conn, texto)
        conn.execute(
            "UPDATE notes SET due = ?, priority = ?, created_at = ? WHERE id = ?",
            (prazo, prio, (datetime.now() - timedelta(hours=len(NOTAS) - i)).isoformat(), n.id),
        )
        for tag in tags:
            conn.execute(
                "INSERT OR IGNORE INTO tags (note_id, tag) VALUES (?, ?)", (n.id, tag)
            )
        if status != "todo":
            set_status(conn, n.id, status)

    # Uma nota arrastada à mão, para a visão geral não parecer uma grade vazia —
    # e porque a posição gravada é o mecanismo que faz a mão vencer o modelo.
    move_note(conn, 3, pos_x=48, pos_y=430)
    total = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
    conn.close()
    return total


if __name__ == "__main__":
    destino = Path(os.environ.get("TA_DB", "/tmp/ta-demo/demo.db"))
    destino.parent.mkdir(parents=True, exist_ok=True)
    if destino.exists():
        destino.unlink()
    print(f"{semear(destino)} notas em {destino}")
