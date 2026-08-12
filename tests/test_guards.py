"""The guards in `scripts/` are code, so they are tested like code.

A check nobody tests is a check that reports green. Each of these three has already
been wrong in a way that mattered:

  · the language scan judged accents, and a Portuguese sentence with no accent in it
    survived three manual sweeps;
  · the secret scan's first idea was entropy, which flags every base64 fixture;
  · the PR-title validator reported one problem per run, turning a three-mistake
    title into three pushes.

So the tests below assert both halves: that each guard **fires** on the thing it
exists for, and that it stays quiet on the correct code already in this repository.
The second half is the one that keeps a guard alive — a check with false positives
gets bypassed, and a bypassed check is worse than none because it looks like
coverage.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import pr_title  # noqa: E402
import scan_language  # noqa: E402
import scan_secrets  # noqa: E402

# ── The PR title ────────────────────────────────────────────────────────────
# Commits are deliberately NOT validated: a commit is a private draft until it is
# pushed, and a hook that rejects `wip` mid-thought costs more than it buys. A PR
# title survives the squash into `git log`, so that one is gated.
GOOD_TITLES = [
    "fix(board): o checkbox de concluídas voltou a aparecer em todas as visões",
    "feat(cli): ta doctor lista as seis capacidades com motivo e conserto",
    "docs: a regra de tradução ganha uma tabela em CONTRIBUTING",
    "refactor(daemon): renomeia os identificadores para inglês",
    "adr: a hora decide o fim da reunião, não a entrada",
    "ci: varredura de credenciais na árvore e no histórico",
    "fix(i18n)!: HORIZONS passa a gravar valores em inglês",
    "test(store): cobre a ordem por horizonte dentro da faixa",
]

BAD_TITLES = [
    # No type at all — the most common mistake, and the one that makes `git log`
    # unscannable.
    ("o checkbox voltou a aparecer", "type(scope)"),
    # A type that is not in the list. `feature` is the reflex; `feat` is the word.
    ("feature(board): mostra o contador", "not one of the types"),
    # A scope nobody can filter on.
    ("fix(mural): o contador estava cortado", "not one of the scopes"),
    # Says nothing.
    ("chore: ajustes", "says nothing"),
    ("fix: wip", "says nothing"),
    # Too short to be a sentence.
    ("fix(cli): conserta", "too short"),
    # Continues the line, so it does not start with a capital.
    ("fix(cli): Conserta o rótulo de prioridade do ta list", "starts with a capital"),
    # A full stop at the end of a title is a sentence that thinks it is a paragraph.
    ("docs: explica a regra de tradução.", "full stop"),
    ("", "empty"),
]


@pytest.mark.parametrize("title", GOOD_TITLES)
def test_a_good_title_passes(title):
    assert pr_title.problems(title) == [], title


@pytest.mark.parametrize(("title", "because"), BAD_TITLES)
def test_a_bad_title_is_rejected_for_the_right_reason(title, because):
    found = pr_title.problems(title)
    assert found, f"{title!r} should have been rejected"
    assert any(because in p for p in found), f"{title!r} → {found}"


def test_the_validator_accepts_its_own_vocabulary():
    """Every type and every scope in the lists has to parse.

    It did not: the scope class was `[a-z-]+`, so `i18n` — a scope from the
    validator's own list — was rejected as malformed. A gate that refuses its own
    vocabulary reads as broken, and what people do about a broken gate is stop
    using the part that trips it.
    """
    for kind in pr_title.TYPES:
        title = f"{kind}: uma frase com palavras suficientes aqui"
        assert pr_title.problems(title) == [], title

    for scope in pr_title.SCOPES:
        title = f"fix({scope}): uma frase com palavras suficientes aqui"
        assert pr_title.problems(title) == [], title


def test_every_problem_is_reported_at_once():
    """One problem per run turns a three-mistake title into three pushes.

    The first version returned on the first failure. That is cheap to write and
    expensive to use, because each round trip costs a push and a CI run.
    """
    found = pr_title.problems("feature(mural): Ajustes.")
    assert len(found) >= 3, found


def test_the_types_are_documented_in_contributing():
    """A gate whose vocabulary is only in the code teaches by rejection.

    Somebody reads the error, guesses, pushes again. The list has to be readable
    before the first attempt, which means it lives in CONTRIBUTING too — and the
    two cannot drift.
    """
    body = (REPO / "CONTRIBUTING.md").read_text()
    for kind in pr_title.TYPES:
        assert f"`{kind}`" in body, f"the type `{kind}` is in the code and not in the docs"


def test_the_scopes_are_documented_in_contributing():
    body = (REPO / "CONTRIBUTING.md").read_text()
    missing = {s for s in pr_title.SCOPES if f"`{s}`" not in body}
    assert not missing, f"scopes in the code and not in the docs: {sorted(missing)}"


# ── The secret scan ─────────────────────────────────────────────────────────
# The shapes below are SYNTHETIC: they match the vendor patterns and were typed at
# random for this test. A real one in here would be exactly the bug being guarded.
FAKE_SECRETS = [
    ("GEMINI_API_KEY=AIzaSyD9fK2mQ7xR4tL8vN1pB3wZ6cH5jY0aE7uI", "Google"),
    ("HA_TOKEN=eyJhbGciOiJIUzI1NiJ9.eyJpc3MiOiJhYmNkZWZnaGlqIn0.Zm9vYmFyYmF6cXV4",
     "JWT"),
    ("key = 'sk-proj9fK2mQ7xR4tL8vN1pB3wZ6cH5jY0aE7uI'", "OpenAI"),
    ("token: ghp_9fK2mQ7xR4tL8vN1pB3wZ6cH5jY0aE7uIbCd", "GitHub"),
    ("AWS_ACCESS_KEY_ID=AKIA5J2QW7RZXK4MN9PL", "AWS"),
    ("-----BEGIN OPENSSH PRIVATE KEY-----", "private key"),
    ("password = 'Xk9mQ2wR7tL4vN1pB3zC6hJ5'", "assigned secret"),
]


@pytest.mark.parametrize(("text", "kind"), FAKE_SECRETS)
def test_a_credential_shape_is_detected(text, kind):
    found = scan_secrets.findings(text, "x")
    assert found, f"{kind} was not detected"


# The placeholders that really appear in this repository's `.env.example` and docs.
# Every one of these MUST stay quiet, because the day the scan cries wolf is the day
# somebody adds `|| true` to the CI step.
PLACEHOLDERS = [
    "GEMINI_API_KEY=sua-chave-aqui",
    "HA_TOKEN=cole_o_token_aqui",
    "api_key = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxx'",
    'TOKEN = "a-test-token-not-a-secret"',
    "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    "token = 'your-token-here'",
    "GEMINI_API_KEY=",
]


@pytest.mark.parametrize("text", PLACEHOLDERS)
def test_a_placeholder_is_not_a_finding(text):
    assert scan_secrets.findings(text, "x") == [], text


def test_the_tree_has_no_credential():
    """The check itself, against the repository as it is.

    `test_security.py` already proves no `.env*` is tracked. This is the other
    half — a secret pasted into a doc, a test fixture or a comment, where no
    filename gives it away.
    """
    assert scan_secrets.scan_tracked_names() == []
    assert scan_secrets.scan_tracked() == []


def test_the_history_has_no_credential():
    """Every blob reachable from any ref, not just the current tree.

    This is the check that would have caught the real incident when it happened: a
    `.env.bak-*` added and later removed is still in the pack, still in every clone,
    and still valid until somebody rotates the value.

    It is a reachability scan, which is the right scope for a published repository —
    a clone receives reachable objects. It does NOT see objects that only the local
    reflog still points at, and it cannot: those are not what gets pushed.
    """
    assert scan_secrets.scan_history() == []


# ── The language scan ───────────────────────────────────────────────────────
def test_portuguese_prose_is_detected():
    """Two function words on a line, with no accent anywhere.

    This exact shape defeated three rounds of manual review, because the reviewing
    was done by counting accented characters and this sentence has none.
    """
    line = "# canceladas ficam de fora: revisar prazo de coisa encerrada nao paga"
    assert scan_language.offending_lines(line)


def test_english_prose_quoting_portuguese_is_not_a_finding():
    """`Lâmpada do quarto` inside an English sentence is the data under test.

    Judging quoted spans would mean a permanent false positive on half the test
    suite, and the fix people reach for is an allowlist entry — which turns a
    decision list into a mute button.
    """
    for line in (
        "# its `friendly_name` has to survive accents, and `Lâmpada` is what proves it",
        '    """`na terça` resolves to the next Tuesday; NOW is a Monday."""',
        '# `!alta` was the syntax for the project\'s whole life and still is',
    ):
        assert scan_language.offending_lines(line) == [], line


def test_notation_is_not_portuguese():
    """`ΔE`, `×` and `↩` are notation, and the board's comments are full of them."""
    for line in ("// the smallest distance between any two is DeltaE 10",
                 "  // `×` to delete, or `↩` in the trash. Last item in the bar",
                 "  /* nowrap: the palette is one visual unit */"):
        assert scan_language.offending_lines(line) == [], line


def test_the_code_is_english():
    """The guard against the repository, as it is.

    The four tests written in Portuguese *inside the PR that removed Portuguese*
    are the reason this runs in CI rather than in whoever remembers to run it.
    """
    found = scan_language.scan()
    assert found == [], "\n".join(found)


def test_every_allowed_file_still_exists():
    """An allowlist entry for a file that moved is a silent hole.

    The check would pass, the file would go unscanned, and nothing would say so.
    """
    for rel in scan_language.ALLOWED:
        assert (REPO / rel).exists(), f"{rel} is allowlisted and does not exist"


def test_every_allowed_file_carries_a_reason():
    """The list is a set of decisions. A blank reason is a mute button."""
    for rel, why in scan_language.ALLOWED.items():
        assert len(why.split()) >= 4, f"{rel} has no real reason: {why!r}"


# ── The guards run as commands, not only as imports ─────────────────────────
@pytest.mark.parametrize("script", ["scan_secrets.py", "scan_language.py"])
def test_the_script_exits_zero_on_this_repository(script):
    """Importing a module and running a program are different things.

    An `argparse` typo or a bad `sys.exit` is invisible to a test that only imports
    the functions, and CI runs the command.
    """
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / script)],
        capture_output=True, text=True, cwd=REPO,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_pr_title_script_exits_nonzero_on_a_bad_title():
    """And the exit code is what CI reads — green on a bad title would be silent."""
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "pr_title.py"), "ajustes"],
        capture_output=True, text=True, cwd=REPO,
    )
    assert r.returncode == 1
    assert "type(scope)" in r.stderr
