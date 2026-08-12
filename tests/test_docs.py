"""The automation docs are code, and code in a document rots silently.

This test exists because a recipe that does not load is worse than no recipe:
whoever copies it takes the error home thinking the problem is theirs. It has
already caught a `when=` block that was missing its imports.
"""
import re
from pathlib import Path

import pytest

from ta.actuators.home import Home
from ta.actuators.lighter import Lighter
from ta.actuators.notify import Notifier
from ta.daemon import CalendarAdapter
from ta.engine import Context, load_rules

GUIDE = Path(__file__).resolve().parents[1] / "docs" / "automations.md"


def python_blocks() -> list[str]:
    return re.findall(r"```python\n(.*?)```", GUIDE.read_text(), re.S)


def test_the_guide_has_recipes():
    """A guard against the test passing because it found nothing to test."""
    assert len([b for b in python_blocks() if "@rule" in b]) >= 6


def test_every_recipe_in_the_guide_loads(tmp_path):
    recipes = [b for b in python_blocks() if "@rule" in b]
    for i, block in enumerate(recipes):
        (tmp_path / f"recipe_{i:02d}.py").write_text(block)

    rep = load_rules(tmp_path)
    assert rep.errors == [], f"a recipe from the guide does not load:\n{rep.errors}"
    assert len(rep.rules) >= len(recipes)


# ── The docs cite rules that really exist (ADR 0011) ────────────────────────
REPO = Path(__file__).resolve().parents[1]
DOCS_COM_REGRAS = (REPO / "README.md", GUIDE)


def real_rules() -> set[str]:
    return {r.name for r in load_rules(REPO / "examples" / "rules").rules}


def test_the_docs_cite_no_meeting_rule_that_does_not_exist():
    """The rule was renamed and the condition left; the docs lied for half a day.

    `README.md` and the guide showed `reuniao_tarde` with `when=after("16:00")`
    after the function became `meeting` and the hour left the trigger. Nobody
    noticed because nothing compared the two — `test_every_recipe_in_the_guide_loads`
    only proves the block is valid Python, and a wrong name loads perfectly.

    Only the meeting names are held to this: the other recipes are didactic and
    deliberately do not exist in `rules/`.
    """
    real = real_rules()
    cited: set[str] = set()
    for doc in DOCS_COM_REGRAS:
        cited |= set(re.findall(r"async def (\w+)\(ctx\)", doc.read_text()))

    ghosts = {n for n in cited if "meeting" in n} - real
    assert not ghosts, f"the docs cite rule(s) that do not exist: {sorted(ghosts)}"


def test_every_real_rule_shows_up_in_the_guide():
    """The other side: a new rule nobody documented is also a wrong document."""
    missing = {r for r in real_rules() if r not in GUIDE.read_text()}
    assert not missing, f"rule never mentioned in the guide: {sorted(missing)}"


# The methods the guide promises on `ctx.*`. Rename one and the guide lies.
@pytest.mark.parametrize(
    ("cls", "methods"),
    [
        (Home, ("switch_on", "turn_off", "state", "entities", "sensors", "light")),
        (Lighter, ("apply_profile", "enable", "toggle", "profiles")),
        (Notifier, ("send",)),
        # Both English names AND both Portuguese aliases: a rule on somebody's
        # disk calls `agora()`, and it lives outside this repository.
        (CalendarAdapter, ("now", "today", "agora", "hoje")),
    ],
)
def test_the_documented_api_exists(cls, methods):
    for m in methods:
        assert callable(getattr(cls, m, None)), f"{cls.__name__}.{m} does not exist"


def test_the_calendars_portuguese_aliases_point_at_the_same_thing():
    """`agora`/`hoje` must not become copies that age separately.

    A rule in somebody's `~/.config/ta/rules/` calls `ctx.calendar.agora()`, and
    that file lives outside this repository: no rename of ours reaches it. The pair
    has to be the SAME object, otherwise a fix on the English side never arrives on
    the Portuguese one and the old rule starts behaving differently from a new one.
    """
    assert CalendarAdapter.agora is CalendarAdapter.now
    assert CalendarAdapter.hoje is CalendarAdapter.today


def test_the_documented_ctx_fields_exist():
    fields = set(Context.__dataclass_fields__)
    assert {"now", "trigger", "home", "lighter", "notify", "calendar", "note", "extra"} <= fields


# ── Calendar account identity ───────────────────────────────────────────────
from ta.sensors.calendar import CalendarSource  # noqa: E402


def test_a_personal_account_is_recognised_by_its_provider():
    for email in ("eu@gmail.com", "x@outlook.com", "y@icloud.com"):
        s = CalendarSource(uid="u", name="Terminal Assistant", parent="p", local=False,
                           account=email)
        assert s.personal, email


def test_your_own_domain_is_treated_as_work():
    s = CalendarSource(uid="u", name="Terminal Assistant", parent="p", local=False,
                       account="eu@empresa-com-dominio.com")
    assert not s.personal


def test_an_unknown_account_is_not_personal():
    """With no email it does not claim personal — the wrong default costs more here."""
    s = CalendarSource(uid="u", name="x", parent="p", local=False, account="")
    assert not s.personal


# ── The `ta init` profile reaches the review prompt ─────────────────────────
# This test exists because "are we passing the context?" can only be answered by
# looking at the real prompt: the data being in the database does not prove it
# arrived there.
import asyncio  # noqa: E402

from ta.llm import LLM  # noqa: E402


class SpyLLM(LLM):
    """Intercepts `_structured` to inspect the assembled prompt."""

    def __init__(self):
        super().__init__("a-fake-key")
        self.prompt = ""

    async def _structured(self, prompt, schema, system=""):
        self.prompt = prompt
        from types import SimpleNamespace

        return SimpleNamespace(
            intent="anotacao", due="", remind_at="", priority="", tags=[],
            is_event=False, title="", start="", end="", account="pessoal",
            confidence=0.0, reason="",
        )


PROFILE = "## Trabalho\nbackend de um sistema de pagamentos (Kafka, Postgres, Go)"


def test_the_priorities_profile_enters_the_review_prompt():
    spy = SpyLLM()
    asyncio.run(
        spy.review_capture(
            "revisar o consumer do Kafka",
            due=None,
            remind_at=None,
            priorities=PROFILE,
            accounts="pessoal: gmail.com, trabalho: empresa.com",
        )
    )
    assert "sistema de pagamentos" in spy.prompt
    assert "Kafka, Postgres, Go" in spy.prompt


def test_only_the_account_domain_goes_to_the_model():
    """The domain decides the routing; the whole email would be more data than needed."""
    spy = SpyLLM()
    asyncio.run(
        spy.review_capture(
            "x", due=None, remind_at=None,
            accounts="pessoal: gmail.com, trabalho: empresa.com",
        )
    )
    assert "empresa.com" in spy.prompt
    assert "eu@empresa.com" not in spy.prompt


def test_with_no_profile_the_prompt_gains_no_empty_section():
    spy = SpyLLM()
    asyncio.run(spy.review_capture("x", due=None, remind_at=None))
    assert "Contexto de quem escreveu" not in spy.prompt


def test_the_cli_and_export_markers_are_the_same():
    """Diverging would make the same note look different in `ta list` and `ta export`."""
    from ta.cli import STATUS_MARK as cli_marks
    from ta.store import STATUS_MARK as export_marks

    assert cli_marks == export_marks


# ── The board does not reimplement the order (ADR 0010) ─────────────────────
BOARD = Path(__file__).resolve().parents[1] / "src" / "ta" / "web" / "board.html"


def test_every_band_has_a_label_in_both_languages():
    """Since `TA_LANG`, the catalogue is the single source of band labels.

    Without this pin, renaming or adding a band in Python breaks nothing: the
    divider simply loses its label, silently, and in only one language.
    """
    from ta.i18n import LANGS, MESSAGES
    from ta.store import HORIZONS

    for band in HORIZONS:
        entry = MESSAGES.get(f"horizon.{band}")
        assert entry, f"band {band!r} has no label in the catalogue"
        assert set(entry) == set(LANGS), f"horizon.{band} is missing a language"


def test_the_board_reads_the_bands_from_the_catalogue():
    """And not from a table of its own — which would be a translation with no test."""
    body = BOARD.read_text()
    assert 'tr(`horizon.${h}`)' in body, "the board went back to naming bands itself"
    assert "const I18N = /*__I18N__*/{}" in body, "the board lost its injection point"


def test_the_board_does_not_reimplement_the_order():
    """The order arrives ready from `GET /notes`; a client sort is the rule twice."""
    assert "PRIO_RANK" not in BOARD.read_text()


def test_the_board_warns_when_the_daemon_is_old():
    """Taking the sort off the client created a dependency on the server version.

    The board is served `no-store` and updates immediately; the Python routes only
    after a restart. Against an old daemon no `horizon` arrives, the order comes
    raw from `sort_key` and the screen is **wrong while looking right** — which is
    what really happened. With no JS harness, this static pin is what stops the
    guard being removed in the next refactor.
    """
    from ta.i18n import LANGS, MESSAGES

    body = BOARD.read_text()
    assert '"horizon" in allNotes[0]' in body, "the board lost its version guard"
    assert 'tr("daemon.outdated")' in body, "the guard lost its message"

    # The message moved to the catalogue, so that is where the fix command has to
    # be — in both languages. A guard that detects and does not say what to do
    # leaves the person exactly where they were.
    for lang in LANGS:
        assert "systemctl --user restart ta" in MESSAGES["daemon.outdated"][lang]


def test_the_notice_stays_outside_the_board():
    """In the free-form view the post-its are `position: absolute` inside `#board`.

    The first version of the notice was inserted INSIDE it and came out illegible
    behind the first post-it — seen on screen, not in a test. Outside `#board` it
    pushes the board down instead of being covered.
    """
    body = BOARD.read_text()
    assert body.index('id="notice"') < body.index('id="board"')
