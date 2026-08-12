"""The registry that answers "what works on this machine?".

Before it, the answer was scattered across five `shutil.which` calls, a
`Calendar.error` that `/health` computed and threw away, and a `Home.configured` —
and it reached the user only through `journalctl`, after the daemon came up.

These tests cover the two things that go wrong in a diagnosis: **lying** (saying
something is missing when it is configured) and **dying** (blowing up in exactly
the bare environment where it is most needed).
"""
from pathlib import Path

import pytest

from ta.capabilities import Capability, by_command, inspect
from ta.cli import HELP_GROUPS, build_parser
from ta.config import Config
from ta.i18n import lang

REPO = Path(__file__).resolve().parents[1]


def test_the_six_capabilities_exist():
    keys = [c.key for c in inspect(Config())]
    assert keys == ["notes", "calendar", "mic", "home", "lighter", "ai"]


def test_so_as_notas_sao_essential():
    """The whole positioning in one assertion: the core runs anywhere.

    If some day another integration gets marked essential, `ta doctor` starts
    exiting non-zero on a machine that only wants to capture notes — and the
    project stops being what the README promises.
    """
    essential = [c.key for c in inspect(Config()) if c.essential]
    assert essential == ["notes"]


def test_every_unavailable_capability_says_what_to_do():
    """A diagnosis with no fix only confirms the problem to whoever already saw it."""
    for c in inspect(Config()):   # an empty Config(): almost everything unavailable
        if not c.ok:
            assert c.fix, f"{c.key} does not say how to fix it"


def test_the_ai_declares_itself_optional():
    """Without it, a red line in the table reads as a broken installation."""
    ai = next(c for c in inspect(Config()) if c.key == "ai")
    assert not ai.ok
    assert "optional" in ai.fix.lower()


def test_the_summary_fits_on_one_line():
    """The calendar's reason carries the whole GLib error.

    Right in `doctor`, wrong in the command list, where it pushes everything else
    off the screen.
    """
    c = Capability(key="x", label="X", ok=False, reason="short. And here comes the long rest.")
    assert c.summary == "short"


# ── The bare environment, which is where the diagnosis matters most ──────────
def test_the_calendar_degrades_with_no_graphical_session(monkeypatch):
    """`GLib.GError: Cannot autolaunch D-Bus without X11 $DISPLAY`.

    The typelibs exist, but there is no bus — a server, a container, SSH with no
    graphical session, and CI. `_load` only caught `ImportError` and `ValueError`,
    so this error RAISED and took down `ta doctor` and `ta --help` exactly where
    they need to answer. Reproduced with an error that is neither of those two,
    which is what the old version let through.
    """
    # This test needs `gi` PRESENT, because the scenario it reproduces is "the
    # typelibs exist and the bus does not". CI's core job runs with no `gi` at all,
    # on purpose, and there is nothing to reproduce there — the calendar job is
    # what exercises this path.
    gi = pytest.importorskip("gi", reason="the scenario requires the typelibs installed")

    from ta.sensors.calendar import Calendar

    class GLibGError(Exception):
        pass

    cal = Calendar()

    def no_bus(*a, **k):
        raise GLibGError("Cannot autolaunch D-Bus without X11 $DISPLAY (0)")

    # `SourceRegistry.new_sync` is the exact point where GLib blows up.
    gi.require_version("EDataServer", "1.2")
    from gi.repository import EDataServer

    monkeypatch.setattr(EDataServer.SourceRegistry, "new_sync", no_bus)

    assert cal.available is False, "the calendar should degrade, not raise"
    assert "D-Bus" in cal.error

    # Against the CATALOGUE, not against a literal: the suite runs in `pt`, and
    # pinning the English sentence here would make the test fail because of the
    # language rather than because of the behaviour. What matters is that the
    # message reassures whoever reads it.
    from ta.i18n import t

    assert t("calendar.no_bus", erro="").rstrip(". ") .split(". ", 1)[-1] in cal.error


def test_one_broken_probe_does_not_take_the_others_down(monkeypatch):
    """A diagnosis that dies at the first problem is useless on a problem machine.

    The calendar proved it: with no graphical session it took the whole of
    `ta doctor` down with it, and that command exists precisely for environments
    like that one.
    """
    from ta import capabilities

    def bad_probe(cfg):
        raise RuntimeError("strange hardware")

    monkeypatch.setattr(capabilities, "PROBES", (capabilities._notes, bad_probe))
    caps = capabilities.inspect(Config())

    assert len(caps) == 2
    assert caps[0].ok is True, "the good probe was lost along with it"
    assert caps[1].ok is False
    assert "strange hardware" in caps[1].reason


def test_help_survives_a_broken_probe(monkeypatch):
    """And this is the one that matters.

    A `--help` that raises is far worse than an unmarked one.
    """
    from ta import capabilities

    monkeypatch.setattr(
        capabilities, "inspect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    epilogue = build_parser().epilog
    # Every group is still listed, only without the availability marker.
    for _, title, commands in HELP_GROUPS:
        assert title in epilogue
        assert commands.split()[0] in epilogue


def test_the_doctor_screen_is_all_in_one_language():
    """Metade traduzida e metade nao le como software quebrado.

    O `ta doctor` mostrava `language`/`config`/`daemon` e `Notes`/`Calendar` fixos
    em ingles, e o resumo pelo catalogo. Em `TA_LANG=pt` a MESMA tabela saia nos
    dois idiomas ao mesmo tempo.

    Nome de produto e a excecao declarada: `Home Assistant`, `Lighter` e `Gemini`
    sao iguais nos dois, porque traduzir nome proprio atrapalha quem vai procurar
    por ele.
    """
    from ta.capabilities import environment
    from ta.i18n import LANGS, MESSAGES

    for c in inspect(Config()):
        chave = f"cap.{c.key}"
        assert chave in MESSAGES, f"o rotulo de {c.key!r} nao passa pelo catalogo"
        assert set(MESSAGES[chave]) == set(LANGS)
        assert c.label == MESSAGES[chave][lang()]

    rotulos = [k for k, _ in environment(Config())]
    for r in rotulos:
        assert r in {MESSAGES[k][lang()] for k in
                     ("doctor.language", "doctor.config", "doctor.daemon")}, r


# ── The grouped --help ──────────────────────────────────────────────────────
def test_every_parser_command_is_in_a_group():
    """A new command nobody grouped disappears from `--help`, silently.

    The flat argparse list was removed on purpose, so the epilogue is the ONLY
    listing — and what is not in it does not exist for whoever reads.
    """
    registered = set(build_parser()._subparsers._group_actions[0].choices)
    grouped = {c for _, _, cmds in HELP_GROUPS for c in cmds.split()}
    assert registered - grouped == set(), "command in no group at all"


def test_os_comandos_grouped_existem_de_verdade():
    """The reverse: a group citing a command that no longer exists."""
    registered = set(build_parser()._subparsers._group_actions[0].choices)
    grouped = {c for _, _, cmds in HELP_GROUPS for c in cmds.split()}
    assert grouped - registered == set(), "a group cites a command that does not exist"


def test_luz_and_light_are_the_same_command():
    choices = build_parser()._subparsers._group_actions[0].choices
    assert choices["luz"].get_default("func") is choices["light"].get_default("func")


def test_help_points_at_doctor():
    assert "ta doctor" in build_parser().epilog


def test_each_grouped_command_has_a_known_capability():
    """`by_command` is what would tie a runtime error to its capability."""
    table = by_command(inspect(Config()))
    assert table["organize"].key == "ai"
    assert table["luz"].key == "home"
    assert table["note"].key == "notes"
