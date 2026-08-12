"""`TA_LANG` governs the parser, the text, and the language the model writes in.

The English parser tests exist because the forms are **structurally** different,
not a swap of words: Portuguese says "17 de outubro" and "na sexta", English says
"October 17" and "next Friday" — plus AM/PM, which Portuguese does not need and
English cannot live without.

The test that matters most is the `@03/04` one: it pins down why the decision was
"one language at a time" rather than "both at once" (ADR 0013).
"""
from datetime import date, datetime

import pytest

from ta import i18n
from ta.notes import parse

# A Tuesday, so that "next friday" and "@sexta" have a predictable answer.
NOW = datetime(2026, 8, 11, 9, 0)


@pytest.fixture
def em(monkeypatch):
    """Pins the language and clears the caches on both sides."""

    def _pin(lang: str):
        monkeypatch.setenv("TA_LANG", lang)
        i18n.reset_cache()
        return lambda text: parse(text, now=NOW)

    yield _pin
    i18n.reset_cache()


# ── Precedence ──────────────────────────────────────────────────────────────
def test_ta_lang_beats_everything(monkeypatch):
    monkeypatch.setenv("TA_LANG", "en")
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    i18n.reset_cache()
    assert i18n.lang() == "en"
    assert i18n.lang_source() == "TA_LANG"


def test_the_system_locale_counts_when_nothing_was_configured(monkeypatch, tmp_path):
    """This is what makes nothing change for existing users: a pt_BR machine stays pt.

    It is also why the first run does not have to ask for the language — which
    would otherwise have to live in `ta init`, and that one requires an LLM.
    """
    monkeypatch.delenv("TA_LANG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LANG", "pt_BR.UTF-8")
    from ta import config

    config._user_config.cache_clear()
    i18n.reset_cache()
    assert i18n.lang() == "pt"
    assert i18n.lang_source() == "system locale"


def test_an_unsupported_locale_falls_to_the_default(monkeypatch, tmp_path):
    """`de_DE` does not become German by guesswork — it becomes English, and says so."""
    monkeypatch.delenv("TA_LANG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    from ta import config

    config._user_config.cache_clear()
    i18n.reset_cache()
    assert i18n.lang() == "en"


def test_an_invalid_ta_lang_warns(monkeypatch, caplog, tmp_path):
    """Whoever wrote `TA_LANG=de` made a choice; ignoring it silently misleads."""
    monkeypatch.setenv("TA_LANG", "de")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    i18n.reset_cache()
    with caplog.at_level("WARNING"):
        assert i18n.lang() == "en"
    assert "TA_LANG" in caplog.text


# ── The catalogue must not drift ────────────────────────────────────────────
def test_both_languages_have_the_same_keys():
    """Without this pin, a key only in `pt` becomes wrong text for anybody on `en`.

    The same defect the board's `HORIZON_LABEL` once had: a parallel table nobody
    compares against the other.
    """
    by_lang = {
        lang: {k for k, v in i18n.MESSAGES.items() if lang in v}
        for lang in i18n.LANGS
    }
    assert by_lang["pt"] == by_lang["en"]


def test_an_unknown_key_does_not_blow_up():
    """An ugly message beats a KeyError in the middle of a command that was working."""
    assert i18n.t("does.not.exist") == "does.not.exist"


# ── The ambiguity that motivated the decision ───────────────────────────────
def test_the_numeric_date_follows_the_language(em):
    """`@03/04` is 3 April in pt and 4 March in en. ADR 0013 in one assertion.

    Accepting both vocabularies at once would make this token return a VALID AND
    WRONG date for half the users — no error, no missing mark.
    """
    assert em("pt")("reunião @03/04").due == date(2027, 4, 3)
    assert em("en")("meeting @03/04").due == date(2027, 3, 4)


# ── The English parser ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("call the dentist @friday", date(2026, 8, 14)),
        ("call the dentist @fri", date(2026, 8, 14)),
        ("pay rent @tomorrow", date(2026, 8, 12)),
        ("standup @today", date(2026, 8, 11)),
        ("dentist on October 17", date(2026, 10, 17)),
        ("dentist 17 October", date(2026, 10, 17)),
        ("dentist October 17th", date(2026, 10, 17)),
        ("review next friday", date(2026, 8, 14)),
        ("review on monday", date(2026, 8, 17)),
    ],
)
def test_english_dates(em, text, expected):
    assert em("en")(text).due == expected


@pytest.mark.parametrize(
    ("text", "hour"),
    [
        ("meeting tomorrow at 8", 8),
        ("meeting tomorrow at 8am", 8),
        ("meeting tomorrow at 8pm", 20),
        ("meeting tomorrow at 12pm", 12),
        ("meeting tomorrow at 12am", 0),
    ],
)
def test_am_pm(em, text, hour):
    """AM/PM did not exist in the parser: `8pm` matched NOTHING, silently.

    The `(?!\\S)` lookahead failed on the `p` and the mark was dropped with no word.
    """
    n = em("en")(text)
    assert n.remind_at is not None, "the time was not recognised"
    assert n.remind_at.hour == hour


def test_an_english_retrospective_is_not_a_deadline(em):
    """"today I learned X" is a record, not a task.

    The regex gets the shape right and would get the intent wrong without the
    curated list.
    """
    n = em("en")("today I learned about WAL mode")
    assert n.due is None
    assert n.is_task is False


def test_a_bare_english_number_is_not_a_reminder(em):
    """"8 hours of battery" is the likeliest false positive of them all."""
    assert em("en")("laptop runs 8h of battery").remind_at is None


def test_english_priority(em):
    assert em("en")("ship the thing !high").priority == "high"


# ── Portuguese has not regressed ────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ligar dentista @sexta", date(2026, 8, 14)),
        ("nutricionista 17 de setembro", date(2026, 9, 17)),
        ("revisar na quinta", date(2026, 8, 13)),
        ("pagar aluguel @amanha", date(2026, 8, 12)),
    ],
)
def test_portuguese_dates_are_unchanged(em, text, expected):
    assert em("pt")(text).due == expected


def test_the_portuguese_retrospective_is_unchanged(em):
    assert em("pt")("hoje aprendi sobre WAL").due is None


# ── The board receives the catalogue ready-made ─────────────────────────────
def _board(monkeypatch, tmp_path, lang):
    from starlette.testclient import TestClient
    from test_daemon import FakeCalendar, FakeLighter

    from ta.config import Config
    from ta.daemon import create_app

    monkeypatch.setenv("TA_LANG", lang)
    i18n.reset_cache()
    app = create_app(
        Config(auto_review=False), db_path=tmp_path / "t.db",
        rules_dir=tmp_path / "no-rules", calendar=FakeCalendar(),
        lighter=FakeLighter(), background=False,
    )
    with TestClient(app) as c:
        return c.get("/board").text


def test_the_catalogue_is_injected_into_the_html(monkeypatch, tmp_path):
    """Injected rather than fetched by a route: the language does not change mid-page.

    A second request would only add latency and a moment spent drawing raw keys
    before the catalogue arrived.
    """
    html = _board(monkeypatch, tmp_path, "pt")
    assert "/*__I18N__*/{}" not in html, "the marker was not substituted"
    assert "próximos 7 dias" in html


def test_the_board_changes_language(monkeypatch, tmp_path):
    assert "próximos 7 dias" in _board(monkeypatch, tmp_path, "pt")
    assert "next 7 days" in _board(monkeypatch, tmp_path, "en")


def test_the_marker_survives_in_the_file_on_disk():
    """The file has to keep opening straight in a browser while you work on it.

    That is why the marker is a JS comment followed by `{}`, rather than a
    placeholder that would leave the file syntactically invalid outside the daemon.
    """
    from ta.daemon import BOARD_HTML, I18N_MARKER

    assert I18N_MARKER in BOARD_HTML.read_text()


def test_the_day_of_the_month_does_not_become_a_time(em):
    """"dentist on October 17" used to get a reminder at 17:00.

    The date had already been found, so the preposition stopped being required, and
    the `17` of the day was reread as an hour. Portuguese never suffered from this
    because `8h` and `8:30` require an `h` or a `:`; English needs the rule written
    down, because `8pm` forces the suffix to be optional in the regex.
    """
    n = em("en")("dentist on October 17")
    assert n.due == date(2026, 10, 17)
    assert n.remind_at is None, "the day of the month became a time"


def test_a_real_time_next_to_the_date_still_counts(em):
    """The guard must not cost the legitimate case."""
    n = em("en")("dentist on October 17 at 8:30")
    assert n.due == date(2026, 10, 17)
    assert n.remind_at.hour == 8 and n.remind_at.minute == 30


def test_the_suite_does_not_depend_on_the_machine_locale():
    """No test may depend on the language of whoever runs it.

    The 17 Portuguese parser tests passed on the author's machine (`pt_BR.UTF-8`)
    and failed on CI, which sets no `LANG` — the default becomes `en` and `@sexta`
    stops being a date. CI caught it on the very first run.

    This test is the guard: if `conftest` stops pinning the language, it shouts
    here instead of the whole suite failing in one environment and not the other,
    pointing at the wrong place.
    """
    from conftest import SUITE_LANGUAGE

    assert i18n.lang() == SUITE_LANGUAGE
    assert i18n.lang_source() == "TA_LANG", "the language is being inherited, not pinned"
