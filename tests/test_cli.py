"""O CLI contra um daemon de outra versão.

O `ta` roda do mesmo source tree que o daemon (`pyproject.toml` aponta
`ta.cli:main`), mas o daemon só carrega código novo no `systemctl restart`. Então
o CLI é **sempre** o mais novo dos dois, e precisa sobreviver a um payload velho
dizendo o que fazer — não com traceback, e não em silêncio.
"""
from types import SimpleNamespace

from ta import cli
from ta.config import Config

# Uma task como o daemon VELHO a devolvia: sem `horizon`.
TASK_VELHA = {"id": 7, "text": "revisar os PRs", "due": "2026-08-10", "priority": "baixa"}

BASE = {
    "date": "2026-08-11",
    "weather": None,
    "calendar_available": True,
    "calendar_error": None,
    "calendar_warming": False,
    "events": [],
}


def _rodar(monkeypatch, tasks):
    monkeypatch.setattr(
        cli, "_request", lambda *a, **k: SimpleNamespace(json=lambda: {**BASE, "tasks": tasks})
    )
    assert cli.cmd_today(Config(), SimpleNamespace(date=None)) == 0


def test_today_sem_horizon_avisa_em_vez_de_estourar(monkeypatch, capsys):
    """Antes da guarda isto era `KeyError: 'horizon'` — alto, e inútil."""
    _rodar(monkeypatch, [TASK_VELHA])
    saida = capsys.readouterr().out
    assert "systemctl --user restart ta" in saida
    assert "revisar os PRs" in saida        # degradou, não morreu
    assert "ATRASADA" not in saida          # sem o campo, não há como saber


def test_today_com_horizon_nao_avisa_e_marca_atrasada(monkeypatch, capsys):
    _rodar(monkeypatch, [{**TASK_VELHA, "horizon": "vencida"}])
    saida = capsys.readouterr().out
    assert "desatualizado" not in saida
    assert "ATRASADA" in saida


def test_today_sem_tarefa_nenhuma_nao_indexa_lista_vazia(monkeypatch, capsys):
    """A guarda lê `tasks[0]`, então o caminho do dia vazio tem de vir antes."""
    _rodar(monkeypatch, [])
    assert "nada cobrável hoje" in capsys.readouterr().out
