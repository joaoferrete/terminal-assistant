"""Note capture: a deterministic parser.

This module is the critical path of ADR 0003: dumping a thought cannot wait for
the network. Nothing here does I/O, calls an LLM or consults a network clock.
Text with no marks at all is still a valid Note — the syntax is optional, always.

    #tag           theme
    !high          priority (high | medium | low)
    @friday        deadline -> the Task role
    @@22:00        firing   -> the Reminder role  (`!!` too, but it breaks in zsh)

Beyond the marks, the parser reads **dates and times written in plain language**:
"review the PR today" gets today's deadline, and "dentist on 17 September at
8:30" gets both a deadline and a reminder. This is deliberately deterministic and
uses no LLM: a deadline is on the critical path, and a deadline that depends on
the network is a deadline that fails on a plane.

Two rules that avoid surprises:

1. **An explicit mark always wins.** Plain language only fills what was left
   empty, so `@friday` in a text that also says "today" resolves to Friday.
2. **The text is not mutilated.** Unlike the marks, which are extracted, the
   plain-language expression stays where it is — "review the PR today" still
   reads as a sentence.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

# The CANONICAL value, the one that goes to the database. English, as `STATUSES`
# always was: a schema with `status='todo'` next to `priority='alta'` forces
# whoever reads the database to know two languages, and it would turn `TA_LANG`
# into a bug — told to answer in English the model returns "high", validation
# would reject it and the priority would vanish silently (amendment to ADR 0006).
PRIORITIES = ("high", "medium", "low")

# Accepted at CAPTURE, and forever. `!alta` was the syntax for the whole life of
# the project and is in the muscle memory of the people using it; breaking it
# would buy nothing. Language is a matter of input and display — the stored value
# is singular.
PRIORITY_ALIASES = {
    "alta": "high", "media": "medium", "média": "medium", "baixa": "low",
    "high": "high", "medium": "medium", "low": "low",
}


def resolve_priority(token: str) -> str | None:
    """Normalise what was typed into the canonical value. `None` if it is not one."""
    return PRIORITY_ALIASES.get(token.lower())

# A closed vocabulary for review to choose from. Closed on purpose: if the model
# could invent tags, every note would get a near-identical theme ("work",
# "professional", "job") and the board's visual grouping would lose its meaning —
# which is precisely why automatic tagging exists.
#
# You remain free to write `#anything` by hand: the list restricts the LLM, not
# you.
#
# NOTE ON THE VALUES: these are stored in the database and appear on the user's
# post-its, so they stay in Portuguese even though the code around them is
# English. Translating them would need a migration and would rewrite tags people
# chose themselves — the same rule as ADR 0006's amendment, seen from the other
# side: what is stored does not change for cosmetic reasons.
#
# `trabalho` and `pessoal` are the **axis**: review always marks one of the two,
# and it is what the board filters by. The others are themes, at most two.
AXIS = ("trabalho", "pessoal")

# A third axis: what the note IS. Comes from review's `intent`, and becomes a tag
# so it can be filtered and seen on the post-it like any other.
#
# `anotacao` is the case that justifies the axis existing: a record or a loose
# idea has no deadline and no urgency, and asking for their priority only
# clutters the board.
KINDS = ("tarefa", "compromisso", "anotacao")

SUGGESTED_TAGS = (
    "trabalho",
    "pessoal",
    "saude",
    "casa",
    "estudo",
    "financeiro",
    "compras",
    "familia",
    "ideia",
)


def _strip_accents(s: str) -> str:
    """To compare a word written with and without accents. Never touches Note text."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c)
    )

# ── Vocabulary, per language ────────────────────────────────────────────────
# One ACTIVE language at a time, chosen by `TA_LANG` (ADR 0013). Both together
# would make `@03/04` ambiguous — 3 April in Portuguese, 4 March in the American
# convention — and whichever side was chosen would be silently wrong for half the
# users. Silently is the word: it returns a valid, wrong date.
#
# Unaccented because nobody types accents in a hurry.
WEEKDAYS_BY_LANG = {
    "pt": {
        "segunda": 0, "seg": 0,
        "terca": 1, "ter": 1,
        "quarta": 2, "qua": 2,
        "quinta": 3, "qui": 3,
        "sexta": 4, "sex": 4,
        "sabado": 5, "sab": 5,
        "domingo": 6, "dom": 6,
    },
    "en": {
        "monday": 0, "mon": 0,
        "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
        "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6,
    },
}

MONTHS_BY_LANG = {
    "pt": {
        "janeiro": 1, "jan": 1,
        "fevereiro": 2, "fev": 2,
        "marco": 3, "mar": 3,
        "abril": 4, "abr": 4,
        "maio": 5,
        "junho": 6, "jun": 6,
        "julho": 7, "jul": 7,
        "agosto": 8, "ago": 8,
        "setembro": 9, "set": 9,
        "outubro": 10, "out": 10,
        "novembro": 11, "nov": 11,
        "dezembro": 12, "dez": 12,
    },
    "en": {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    },
}

# "today I learned X" is retrospective, not a deadline — and the regex alone does
# not know that, because the difference lives in the verb tense. The lists are
# curated and short on purpose: they catch the frequent cases with high precision
# instead of trying to enumerate a language's conjugation. Whatever slips through
# is corrected by the LLM's second pass, which understands intent (amendment to
# ADR 0003).
RETROSPECTIVE_BY_LANG = {
    "pt": (
        "aprendi", "aprendemos", "descobri", "vi", "fiz", "fizemos", "tive", "tivemos",
        "foi", "fui", "consegui", "conseguimos", "aconteceu", "rolou", "deu", "terminei",
        "acabei", "resolvi", "entendi", "notei", "percebi", "li", "ouvi", "falei",
    ),
    "en": (
        "learned", "learnt", "found", "figured", "discovered", "saw", "did", "was",
        "were", "had", "got", "went", "finished", "shipped", "fixed", "solved",
        "realized", "realised", "noticed", "read", "heard", "talked", "met", "wrote",
    ),
}

# Subjects that can appear between the time word and the retrospective verb:
# "hoje EU aprendi", "today I learned".
SUBJECTS_BY_LANG = {
    "pt": r"(?:eu\s+|a\s+gente\s+|n[óo]s\s+)?",
    "en": r"(?:i\s+|we\s+)?",
}


def _lang() -> str:
    """Imported late so `notes` does not depend on `config` at import time."""
    from .i18n import lang

    return lang()


def weekdays() -> dict[str, int]:
    return WEEKDAYS_BY_LANG[_lang()]


def months() -> dict[str, int]:
    return MONTHS_BY_LANG[_lang()]

# ── Plain language ──────────────────────────────────────────────────────────
# The forms are STRUCTURALLY different between the languages, it is not a matter
# of swapping words: Portuguese says "17 de outubro" and "na sexta", English says
# "October 17" and "next Friday". Each language has its own set, compiled once.
#
# Accents go in as a character class rather than being stripped from the text:
# normalising would shift the indices, and mark extraction depends on them.
def _compile(lang: str) -> dict[str, re.Pattern]:
    months_alt = "|".join(sorted(MONTHS_BY_LANG[lang], key=len, reverse=True))
    days_alt = "|".join(sorted(WEEKDAYS_BY_LANG[lang], key=len, reverse=True))
    retro = "|".join(RETROSPECTIVE_BY_LANG[lang])
    subject = SUBJECTS_BY_LANG[lang]

    if lang == "pt":
        day_month = (
            r"(?<!\S)(?P<d1>\d{1,2})\s+de\s+(?P<m1>[a-zç~ãâáéêíóôõú]+)"
            r"(?:\s+de\s+(?P<year>\d{4}))?(?!\S)"
        )
        relative = r"(?<!\S)(depois\s+de\s+amanh[ãa]|amanh[ãa]|hoje)(?!\S)"
        weekday = (
            r"(?<!\S)(?:na|no|pr[óo]xim[ao])\s+"
            r"(segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)(?:-feira)?(?!\S)"
        )
        # A time only counts with a preposition (`às 8h`) or alongside a date.
        # Without that, "runs 8h on battery" would become a reminder — the most
        # likely false positive of all.
        time_prep = r"(?<!\S)[àa]s?\s+(\d{1,2})(?::(\d{2})|h(\d{2})?)?\s*(am|pm)?(?!\S)"
    else:
        # English accepts both orders because both are current: "October 17" and
        # "17 October". `17th` too, which Portuguese does not have.
        day_month = (
            r"(?<!\S)(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m1>" + months_alt + r")"
            r"|(?P<m2>" + months_alt + r")\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?)"
            r"(?:,?\s+(?P<year>\d{4}))?(?!\S)"
        )
        relative = r"(?<!\S)(day\s+after\s+tomorrow|tomorrow|today|tonight)(?!\S)"
        weekday = r"(?<!\S)(?:next|on|this)\s+(" + days_alt + r")(?!\S)"
        # `at 8`, `at 8:30`, `at 8pm`. The `pm` is what Portuguese does not need
        # and English cannot live without — and without it `8pm` matched NOTHING,
        # silently, because the `(?!\S)` lookahead failed on the `p`.
        time_prep = r"(?<!\S)at\s+(\d{1,2})(?::(\d{2}))?()\s*(am|pm)?(?!\S)"

    return {
        "day_month": re.compile(day_month, re.IGNORECASE),
        "relative": re.compile(relative, re.IGNORECASE),
        "weekday": re.compile(weekday, re.IGNORECASE),
        "time_prep": re.compile(time_prep, re.IGNORECASE),
        "retrospective": re.compile(
            r"\s+" + subject + r"(" + retro + r")(?!\w)", re.IGNORECASE
        ),
        # Bare `8h`, `8:30`, `8pm` — only valid alongside a date.
        "bare_time": re.compile(
            r"(?<!\S)(\d{1,2})(?::(\d{2})|h(\d{2})?)?\s*(am|pm)?(?!\S)"
            if lang == "en"
            else r"(?<!\S)(\d{1,2})(?::(\d{2})|h(\d{2})?)()(?!\S)",
            re.IGNORECASE,
        ),
    }


RE_BY_LANG = {lang: _compile(lang) for lang in ("pt", "en")}


def _re(name: str) -> re.Pattern:
    return RE_BY_LANG[_lang()][name]

# `@@HH:MM` is the reminder mark. The parallel with `@date` is intentional: `@` is
# the day, `@@` is the day with a time.
#
# The old mark was `!!HH:MM`, and it **does not work in zsh**: `!!` triggers
# history expansion as the line is read and the command dies whole with
# "zsh: no such word in event" — it never reaches the parser. Measured, not
# assumed. `!!` is still accepted because the board has no shell, and because
# breaking an already-written note buys nothing.
RE_REMIND = re.compile(r"(?<!\S)(?:@@|!!)(\d{1,2}):(\d{2})(?!\S)")
# The alternation comes from the alias table, not from `PRIORITIES`: whoever
# types has more valid forms than the database stores. Sorted longest-first
# because `media` is a prefix of `medium` — without that the alternation would
# match the prefix and only come back through backtracking, which works but
# depends on a detail of the regex engine instead of being written down.
RE_PRIORITY = re.compile(
    r"(?<!\S)!(" + "|".join(sorted(PRIORITY_ALIASES, key=len, reverse=True)) + r")(?!\S)",
    re.IGNORECASE,
)
RE_TAG = re.compile(r"(?<!\S)#([\w-]+)(?!\S)", re.UNICODE)
# `/` goes into the class to accept @25/12 — without it, `\w` stops at the `2` of
# `25` and the lookahead fails, silently discarding the whole mark.
RE_DUE = re.compile(r"(?<!\S)@([\w/-]+)(?!\S)", re.UNICODE)


@dataclass
class ParsedNote:
    """The result of a capture. The roles are derived, never chosen."""

    text: str
    tags: list[str] = field(default_factory=list)
    priority: str | None = None
    due: date | None = None
    remind_at: datetime | None = None

    @property
    def is_task(self) -> bool:
        return self.due is not None

    @property
    def is_reminder(self) -> bool:
        return self.remind_at is not None


def _resolve_date(token: str, today: date) -> date | None:
    """Resolve a @date token. Returns None when it does not recognise it.

    Not recognising is a legitimate result: `@home` is not a date, and the mark
    goes back into the text rather than becoming an error.
    """
    t = token.lower()

    # The relative words do not collide between the languages, so they always
    # apply: `@today` on a Portuguese machine is unambiguous, and refusing it
    # would be pedantry. What canNOT apply in both is the numeric date, below.
    if t in ("hoje", "today"):
        return today
    if t in ("amanha", "amanhã", "tomorrow"):
        return today + timedelta(days=1)
    if t in ("ontem", "yesterday"):
        return today - timedelta(days=1)

    # Full ISO
    try:
        return date.fromisoformat(token)
    except ValueError:
        pass

    # Numeric date. The ORDER follows the active language, and it is the entire
    # reason the decision was "one language at a time": `03/04` is 3 April in
    # Portuguese and 4 March in the American convention. Accepting both would
    # make this token return a valid, wrong date, with no error (ADR 0013).
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", token):
        first, second, year = int(m[1]), int(m[2]), m[3]
        day, month = (second, first) if _lang() == "en" else (first, second)
        y = today.year if year is None else (2000 + int(year) if len(year) == 2 else int(year))
        try:
            candidate = date(y, month, day)
        except ValueError:
            return None
        # No explicit year and already past: the user means next year.
        if year is None and candidate < today:
            try:
                candidate = date(y + 1, month, day)
            except ValueError:
                return None
        return candidate

    # Weekday: always the next one, never today. "@friday" on a Friday means the
    # coming one — if they meant today, they would have written "@today".
    days = weekdays()
    if t in days:
        delta = (days[t] - today.weekday()) % 7
        return today + timedelta(days=delta or 7)

    return None


def _nl_date(raw: str, today: date) -> date | None:
    """A date written in the current language. None when there is not one."""
    if m := _re("day_month").search(raw):
        # Named groups in both languages, because English accepts both orders
        # ("October 17" and "17 October") and Portuguese only one. Coalescing
        # here is what keeps the rest of the body identical for both.
        g = m.groupdict()
        raw_day, raw_month, raw_year = (
            g.get("d1") or g.get("d2"), g.get("m1") or g.get("m2"), g.get("year")
        )
        month = months().get(_strip_accents(raw_month)) if raw_month else None
        if month is not None and raw_day:
            day, year = int(raw_day), int(raw_year) if raw_year else today.year
            try:
                found = date(year, month, day)
            except ValueError:
                found = None
            if found is not None:
                # No explicit year and already past: they meant next year.
                if raw_year is None and found < today:
                    try:
                        found = date(year + 1, month, day)
                    except ValueError:
                        return None
                return found

    if m := _re("relative").search(raw):
        # A retrospective verb right after the word? Then it is not a deadline:
        # "hoje aprendi X" and "today I learned X" are records, not tasks.
        if _re("retrospective").match(raw, m.end()):
            return None
        word = _strip_accents(re.sub(r"\s+", " ", m[1]))
        if word in ("hoje", "today", "tonight"):
            return today
        if word in ("amanha", "tomorrow"):
            return today + timedelta(days=1)
        if word in ("depois de amanha", "day after tomorrow"):
            return today + timedelta(days=2)

    if m := _re("weekday").search(raw):
        target = weekdays().get(_strip_accents(m[1]))
        if target is not None:
            delta = (target - today.weekday()) % 7
            return today + timedelta(days=delta or 7)

    return None


def _nl_time(raw: str, *, requires_preposition: bool) -> time | None:
    """A time written in the current language.

    `requires_preposition` is the guard against false positives: with no date in
    the text, only an "às"/"at" turns a number into a time. "runs 8h on battery"
    is not an appointment, and neither is "8 hours of battery".
    """
    m = _re("time_prep").search(raw)
    if m is None and not requires_preposition:
        m = _re("bare_time").search(raw)
        # A bare number is NEVER a time. Without this guard, "dentist on October
        # 17" gained a reminder at 17:00: the day of the month was re-read as an
        # hour, because the date had already been found and the preposition
        # stopped being required. Portuguese did not suffer from this — `8h` and
        # `8:30` require an `h` or a `:` in the form — and English needs the rule
        # written down because `8pm` forces the suffix to be optional in the regex.
        if m is not None and not (m[2] or m[3] or (m.re.groups >= 4 and m[4])):
            return None
    if m is None:
        return None

    hh = int(m[1])
    mm = int(m[2] or m[3] or 0)

    # AM/PM. Portuguese does not use it and the group always comes back empty;
    # English cannot live without it, and without handling it `8pm` matched
    # NOTHING — the `(?!\S)` lookahead failed on the `p` and the mark was
    # silently discarded, which is the worst possible failure mode.
    suffix = (m[4] or "").lower() if m.re.groups >= 4 else ""
    if suffix == "pm" and hh < 12:
        hh += 12
    elif suffix == "am" and hh == 12:
        hh = 0   # 12am is midnight, not noon

    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)


def parse(raw: str, *, now: datetime | None = None) -> ParsedNote:
    """Extract attributes from the raw text.

    `now` is injectable so tests do not depend on the day they run.
    """
    now = now or datetime.now()
    today = now.date()

    tags: list[str] = []
    priority: str | None = None
    due: date | None = None
    remind_at: datetime | None = None
    consumed: list[tuple[int, int]] = []

    if m := RE_REMIND.search(raw):
        hh, mm = int(m[1]), int(m[2])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            at = datetime.combine(today, time(hh, mm))
            # A time already past means tomorrow: nobody schedules into the past.
            remind_at = at if at > now else at + timedelta(days=1)
            consumed.append(m.span())

    if m := RE_PRIORITY.search(raw):
        # What was typed may be `!alta` or `!high`; what gets stored is singular.
        priority = resolve_priority(m[1])
        consumed.append(m.span())

    for m in RE_TAG.finditer(raw):
        tags.append(m[1].lower())
        consumed.append(m.span())

    for m in RE_DUE.finditer(raw):
        if (resolved := _resolve_date(m[1], today)) is not None:
            due = resolved
            consumed.append(m.span())
        # A non-date token (@home) stays in the text on purpose.

    # Plain language comes last, and only where the explicit mark said nothing.
    # The text is NOT altered: the expression is part of the sentence.
    if due is None:
        due = _nl_date(raw, today)

    if remind_at is None:
        clock = _nl_time(raw, requires_preposition=due is None)
        if clock is not None:
            base = due or today
            remind_at = datetime.combine(base, clock)
            # With no date in the text, a time already past is tomorrow — the
            # same rule as `!!HH:MM`, so the two paths do not diverge.
            if due is None and remind_at <= now:
                remind_at += timedelta(days=1)

    # Remove the marks back to front, so the indices stay valid.
    text = raw
    for start, end in sorted(consumed, reverse=True):
        text = text[:start] + text[end:]

    return ParsedNote(
        text=re.sub(r"\s{2,}", " ", text).strip(),
        tags=sorted(set(tags)),
        priority=priority,
        due=due,
        remind_at=remind_at,
    )
