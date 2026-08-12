"""The CLI against a daemon of another version.

`ta` runs from the same source tree as the daemon (`pyproject.toml` points at
`ta.cli:main`), but the daemon only picks up new code on `systemctl restart`. So
the CLI is **always** the newer of the two, and has to survive an old payload
telling it what to do — not with a traceback, and not in silence.
"""
from types import SimpleNamespace

from ta import cli
from ta.config import Config
from ta.i18n import t

# A task the way the OLD daemon returned it: no `horizon`.
OLD_TASK = {"id": 7, "text": "revisar os PRs", "due": "2026-08-10", "priority": "low"}

BASE = {
    "date": "2026-08-11",
    "weather": None,
    "calendar_available": True,
    "calendar_error": None,
    "calendar_warming": False,
    "events": [],
}


def _run(monkeypatch, tasks):
    monkeypatch.setattr(
        cli, "_request", lambda *a, **k: SimpleNamespace(json=lambda: {**BASE, "tasks": tasks})
    )
    assert cli.cmd_today(Config(), SimpleNamespace(date=None)) == 0


def test_today_without_horizon_warns_instead_of_blowing_up(monkeypatch, capsys):
    """Before the guard this was `KeyError: 'horizon'` — loud, and useless."""
    _run(monkeypatch, [OLD_TASK])
    out = capsys.readouterr().out
    assert "systemctl --user restart ta" in out
    assert "revisar os PRs" in out            # it degraded, it did not die
    assert t("cli.overdue") not in out        # without the field there is no way to know


def test_today_with_horizon_does_not_warn_and_marks_overdue(monkeypatch, capsys):
    _run(monkeypatch, [{**OLD_TASK, "horizon": "overdue"}])
    out = capsys.readouterr().out
    assert "systemctl --user restart ta" not in out
    assert t("cli.overdue") in out


def test_today_with_no_task_at_all_does_not_index_an_empty_list(monkeypatch, capsys):
    """The guard reads `tasks[0]`, so the empty-day path has to come first."""
    _run(monkeypatch, [])
    # Compared against the catalogue, not against a literal: the suite runs in
    # `pt`, so a hardcoded string would be testing the language rather than the
    # behaviour — and would break the day somebody re-words the message.
    assert t("cli.nothing_due") in capsys.readouterr().out


def test_the_note_line_follows_the_language(monkeypatch):
    """`ta list` respondia `tarefa, prazo` e `prio high` em qualquer idioma.

    Meio comando traduzido nao e meio problema: `ta list` e `ta note` sao os dois
    comandos que todo mundo usa, e eram os que respondiam no idioma errado.

    O VALOR da prioridade tambem conta. Ele e canonico em ingles no banco, e
    imprimi-lo cru mostrava `prio high` para quem le portugues -- o mesmo defeito
    que o `PRIO_LABEL` tinha do lado oposto.
    """
    from ta import i18n
    from ta.cli import _fmt_note

    nota = {
        "id": 1, "text": "x", "status": "todo", "priority": "high", "tags": [],
        "due": "2026-08-14", "remind_at": None,
        "roles": {"task": True, "reminder": False},
    }

    def _in(lang):
        monkeypatch.setenv("TA_LANG", lang)
        i18n.reset_cache()
        return _fmt_note(nota)

    assert "tarefa, prazo 2026-08-14" in _in("pt")
    assert "prio alta" in _in("pt")
    assert "task, due 2026-08-14" in _in("en")
    assert "prio high" in _in("en")


def test_rules_check_reads_the_directory_the_daemon_reads():
    """Ele validava `$PWD/rules`, que o ADR 0014 aposentou.

    `load_rules` trata diretorio ausente como "nenhuma regra", entao o comando
    imprimia `0 regra(s), 0 com erro` e saia com codigo 0. E o check que se roda
    ANTES de `systemctl --user restart ta`: dava luz verde sem ler regra nenhuma.
    """
    import inspect as _inspect

    from ta.cli import cmd_rules
    from ta.daemon import user_rules_dir

    # So as linhas de CODIGO: a primeira versao deste assert casou com a frase do
    # comentario que explica o bug, e falhou acusando o conserto.
    codigo = "\n".join(
        linha for linha in _inspect.getsource(cmd_rules).splitlines()
        if not linha.lstrip().startswith("#")
    )
    assert "user_rules_dir()" in codigo
    assert 'Path.cwd() / "rules"' not in codigo, "voltou a olhar o diretorio errado"
    assert user_rules_dir().name == "rules"
