from datetime import date, datetime

import pytest

from ta.notes import parse

# Monday, 10 August 2026, 09:00. The suite runs in `pt` (see `conftest`), so the
# input strings below are Portuguese on purpose: they are the parser under test.
NOW = datetime(2026, 8, 10, 9, 0)


def p(raw):
    return parse(raw, now=NOW)


def test_text_with_no_mark_is_still_valid():
    """The syntax is optional. This is the most important case in the module."""
    n = p("ideia: usar cache no pooler")
    assert n.text == "ideia: usar cache no pooler"
    assert not n.is_task and not n.is_reminder
    assert n.tags == [] and n.priority is None


def test_it_extracts_tag_priority_and_deadline():
    n = p("ligar pro dentista #saude !alta @sexta")
    assert n.text == "ligar pro dentista"
    assert n.tags == ["saude"]
    assert n.priority == "high"
    assert n.due == date(2026, 8, 14)  # the following Friday
    assert n.is_task


# ── Priority: input in both languages, stored in one ────────────────────────
@pytest.mark.parametrize(
    ("mark", "canonical"),
    [
        ("!alta", "high"), ("!media", "medium"), ("!média", "medium"), ("!baixa", "low"),
        ("!high", "high"), ("!medium", "medium"), ("!low", "low"),
        ("!ALTA", "high"), ("!High", "high"),
    ],
)
def test_the_mark_accepts_both_languages_and_stores_one(mark, canonical):
    """`!alta` was the syntax for the project's whole life and still is.

    Language is a matter of input and display; the stored value is canonical,
    otherwise the database becomes bilingual and whoever reads it needs both.
    """
    assert p(f"ligar dentista {mark}").priority == canonical


def test_media_does_not_swallow_medium():
    """`media` is a prefix of `medium`, and the alternation is ordered for it.

    Without that order, matching the prefix and backing out depends on the regex
    engine's backtracking — it works, but by accident rather than in writing.
    """
    assert p("x !medium").priority == "medium"
    assert p("x !medium").text == "x"


def test_an_unknown_priority_is_not_a_mark():
    """`!urgente` is not a priority, so it goes back into the text rather than erroring.

    The same discipline as the `@casa` that is not a date.
    """
    n = p("resolver isso !urgente")
    assert n.priority is None
    assert n.text == "resolver isso !urgente"


def test_a_reminder_is_not_confused_with_a_priority():
    """`!!22:00` has to be read before `!`, or leftovers stay in the text."""
    n = p("tomar remedio !!22:00")
    assert n.text == "tomar remedio"
    assert n.remind_at == datetime(2026, 8, 10, 22, 0)
    assert n.priority is None
    assert n.is_reminder and not n.is_task


def test_a_reminder_at_a_past_hour_moves_to_tomorrow():
    n = p("dormir !!07:00")  # it is 09:00 now
    assert n.remind_at == datetime(2026, 8, 11, 7, 0)


def test_the_current_weekday_means_the_next_one():
    """@segunda on a Monday means the next one — for today there is @hoje."""
    assert p("x @segunda").due == date(2026, 8, 17)
    assert p("x @hoje").due == date(2026, 8, 10)
    assert p("x @amanha").due == date(2026, 8, 11)


def test_date_formats():
    assert p("x @2026-12-25").due == date(2026, 12, 25)
    assert p("x @25/12").due == date(2026, 12, 25)
    assert p("x @25/12/2027").due == date(2027, 12, 25)


def test_a_yearless_date_already_past_moves_to_next_year():
    assert p("x @01/01").due == date(2027, 1, 1)


def test_a_token_that_is_not_a_date_stays_in_the_text():
    """@casa is not a deadline, and must neither error nor disappear."""
    n = p("comprar lampada @casa")
    assert n.due is None
    assert "@casa" in n.text


def test_an_invalid_time_is_ignored():
    n = p("x !!99:99")
    assert n.remind_at is None
    assert "!!99:99" in n.text


def test_multiple_tags_deduplicated_and_sorted():
    n = p("x #b #a #a")
    assert n.tags == ["a", "b"]


def test_a_mark_in_the_middle_of_the_text():
    n = p("ligar #saude pro dentista @sexta agora")
    assert n.text == "ligar pro dentista agora"
    assert n.tags == ["saude"]


def test_it_does_not_confuse_a_mark_glued_to_a_word():
    """`a#b` and `email@dominio` are not marks."""
    n = p("mandar email@dominio.com sobre C#coisa")
    assert n.tags == []
    assert n.due is None
    assert n.text == "mandar email@dominio.com sobre C#coisa"


# ── Dates and times in plain Portuguese (no mark) ───────────────────────────
def test_a_deadline_in_plain_portuguese():
    """`Revisar o PR do Ana hoje` has to become a task due today, with no `@`."""
    n = parse("Revisar o PR do Ana hoje", now=NOW)
    assert n.due == date(2026, 8, 10)
    assert n.is_task
    # The text is NOT mutilated: the word is part of the sentence.
    assert n.text == "Revisar o PR do Ana hoje"


def test_day_and_month_with_a_time_become_a_deadline_and_a_reminder():
    n = parse("ir na nutricionista 17 de setembro as 8:30", now=NOW)
    assert n.due == date(2026, 9, 17)
    assert n.remind_at == datetime(2026, 9, 17, 8, 30)


def test_a_weekday_with_a_preposition():
    """`na terça` resolves to the next Tuesday; NOW is a Monday."""
    n = parse("reunião com o cliente na terça às 14h", now=NOW)
    assert n.due == date(2026, 8, 11)
    assert n.remind_at == datetime(2026, 8, 11, 14, 0)


def test_a_month_already_past_moves_to_next_year():
    assert parse("aniversário 3 de fevereiro", now=NOW).due == date(2027, 2, 3)


def test_an_explicit_mark_beats_natural_language():
    """`@sexta` beats `hoje` in the same text: explicit wins."""
    n = parse("revisar isso hoje @sexta", now=NOW)
    assert n.due == date(2026, 8, 14)


def test_a_bare_time_with_no_date_is_not_a_reminder():
    """`rodar 8h de bateria` is not an appointment — a preposition is required."""
    n = parse("rodar 8h de bateria", now=NOW)
    assert n.remind_at is None and n.due is None


def test_a_bare_number_becomes_nothing():
    for text in ("ler o PR 42 do time", "comprar 2 de leite"):
        n = parse(text, now=NOW)
        assert (n.due, n.remind_at) == (None, None), text


def test_a_retrospective_sentence_gets_no_deadline():
    """`hoje aprendi X` is a report, not a deadline. It was the likeliest false positive."""
    for text in (
        "hoje aprendi sobre WAL no sqlite",
        "hoje eu aprendi a usar pointer events",
        "hoje foi difícil",
        "hoje descobri o bug do domínio",
        "amanhã vi o resultado",
    ):
        assert parse(text, now=NOW).due is None, text


def test_the_retrospective_guard_does_not_swallow_a_real_deadline():
    """The guard must not be so wide that it kills `hoje eu preciso revisar`."""
    for text in ("hoje eu preciso revisar o PR", "terminar isso hoje", "dentista amanhã"):
        assert parse(text, now=NOW).due is not None, text


# ── The reminder mark: @@ instead of !! ─────────────────────────────────────
def test_the_double_at_is_the_reminder_mark():
    """`!!` triggers history expansion in zsh and the command dies before it gets
    here: `zsh: no such word in event`. Measured, not assumed."""
    n = parse("tomar remedio @@22:00", now=NOW)
    assert n.remind_at == datetime(2026, 8, 10, 22, 0)
    assert n.is_reminder
    assert n.text == "tomar remedio"


def test_the_double_bang_is_still_accepted():
    """There is no shell in the board, and breaking already-written notes buys nothing."""
    assert parse("tomar remedio !!22:00", now=NOW).remind_at == datetime(2026, 8, 10, 22, 0)


def test_the_double_at_coexists_with_the_date_at():
    n = parse("reuniao @sexta @@14:30", now=NOW)
    assert n.due == date(2026, 8, 14)
    assert n.remind_at == datetime(2026, 8, 10, 14, 30)
    assert n.text == "reuniao"


def test_an_invalid_time_stays_in_the_text():
    n = parse("@@25:99 hora invalida", now=NOW)
    assert n.remind_at is None
    assert "@@25:99" in n.text


def test_an_email_is_not_confused_with_a_mark():
    n = parse("mandar pro email@dominio.com", now=NOW)
    assert (n.due, n.remind_at) == (None, None)
    assert n.text == "mandar pro email@dominio.com"
