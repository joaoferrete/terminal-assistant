"""Validate a pull request title against this repository's convention.

Commits are **not** validated, deliberately: a commit is a private draft until it
is pushed, and a hook that rejects `wip` while you are still thinking costs more
than it buys. A PR title is different — it is the line that ends up in `git log`
after a squash, in the release notes, and in the list somebody scans looking for
the change that broke their machine. That one is worth a gate.

Run it locally before opening:

    python scripts/pr_title.py "fix(board): o checkbox de concluídas voltou a aparecer"

The rules are deliberately few. Each one exists because its absence produces a
title that a person cannot act on.
"""

from __future__ import annotations

import re
import sys

# The type says what KIND of change it is, in one word, before you read the rest.
#
# `adr` is not in the Conventional Commits list and is here because this project
# treats a decision record as a deliverable of its own: an ADR is neither `docs`
# (it is not describing the code, it is deciding) nor `feat` (nothing shipped).
TYPES = {
    "feat": "new behaviour somebody can use",
    "fix": "a defect, with a symptom somebody could hit",
    "docs": "documentation only",
    "test": "tests only, no behaviour change",
    "refactor": "same behaviour, different shape (a rename lives here)",
    "perf": "measurably faster or lighter, with the measurement in the body",
    "build": "packaging, dependencies, the Makefile",
    "ci": "the workflows and the guards that run in them",
    "chore": "the leftovers — use it when nothing else fits, not as a default",
    "adr": "an architecture decision record",
}

# Scopes are optional but must come from this list when present, so that
# `git log --oneline | grep board` is a reliable filter rather than a guess. They
# map to the subsystems the capability registry already names, plus the two
# cross-cutting concerns.
SCOPES = {
    "notes", "board", "cli", "daemon", "db", "store",
    "home", "calendar", "mic", "lighter", "ai",
    "i18n", "docs", "ci", "security", "rules",
}

# `type(scope)!: subject`, where `!` marks a breaking change.
#
# The scope class allows DIGITS, and that is not decoration: `[a-z-]+` rejected
# `i18n`, a scope from the list right above. A validator that refuses its own
# vocabulary teaches people the gate is broken, and the fix they reach for is to
# stop using scopes at all.
PATTERN = re.compile(
    r"^(?P<type>[a-z]+)"
    r"(?:\((?P<scope>[a-z0-9-]+)\))?"
    r"(?P<breaking>!)?"
    r": (?P<subject>.+)$"
)

MAX_LENGTH = 88

# A subject that says nothing. The list is short on purpose: it catches the
# reflexes, not every possible vague phrase. A gate that tries to measure
# descriptiveness in general would reject good titles, which teaches people to
# work around it.
VAGUE = {
    "wip", "update", "updates", "fix", "fixes", "fixed", "changes", "change",
    "melhorias", "melhoria", "ajustes", "ajuste", "correcoes", "correcao",
    "correções", "correção", "refactor", "cleanup", "limpeza", "misc",
    "stuff", "coisas", "varios", "vários", "atualizacoes", "atualizações",
    "small fixes", "minor fixes", "pequenos ajustes", "diversos",
}


def problems(title: str) -> list[str]:
    """Every problem with the title, not just the first.

    Returning a list matters: reporting one error per run turns a three-mistake
    title into three round trips through CI, and each one costs a push.
    """
    found: list[str] = []
    title = title.strip()

    if not title:
        return ["the title is empty"]

    m = PATTERN.match(title)
    if m is None:
        found.append(
            "it does not follow `type(scope): subject` — for example, "
            "`fix(board): o checkbox de concluídas voltou a aparecer`"
        )
        # Without a parse there is nothing else to say that would be true.
        return found

    kind, scope, subject = m["type"], m["scope"], m["subject"].strip()

    if kind not in TYPES:
        found.append(
            f"`{kind}` is not one of the types: {', '.join(sorted(TYPES))}"
        )

    if scope is not None and scope not in SCOPES:
        found.append(
            f"`{scope}` is not one of the scopes: {', '.join(sorted(SCOPES))}. "
            "Leave the scope out if none fits."
        )

    if subject.endswith("."):
        found.append("the subject must not end in a full stop")

    if subject[:1].isupper() and not subject.split()[0].isupper():
        # `TA_LANG` and `HTTP` are fine; `Conserta` is not, because the subject
        # continues the sentence `type: ` already started.
        found.append(
            f"the subject starts with a capital (`{subject.split()[0]}`) — it "
            "continues the line rather than starting one"
        )

    if subject.lower() in VAGUE or subject.lower().rstrip("s") in VAGUE:
        found.append(
            f"`{subject}` says nothing a reader can act on. Say what changed "
            "and, if it was a defect, what the symptom was."
        )

    words = [w for w in re.split(r"\s+", subject) if w]
    if len(words) < 3:
        found.append(
            f"the subject has {len(words)} word(s) — too short to be a sentence. "
            "This repository's history is meant to be readable."
        )

    if len(title) > MAX_LENGTH:
        found.append(
            f"the title is {len(title)} characters, over the {MAX_LENGTH} limit — "
            "the rest belongs in the body, where it has room to be complete"
        )

    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0] if argv else 'pr_title.py'} \"<title>\"", file=sys.stderr)
        return 2

    title = argv[1]
    found = problems(title)
    if not found:
        print(f"ok  {title}")
        return 0

    print(f"The pull request title needs work:\n\n    {title}\n", file=sys.stderr)
    for p in found:
        print(f"  · {p}", file=sys.stderr)
    print(
        "\nThe convention, with the reasoning, is in CONTRIBUTING.md "
        "(\"Opening a pull request\").",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
