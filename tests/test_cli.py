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
