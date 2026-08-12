"""CI has to run everything, and be the same thing you run locally.

Two failure modes, both silent:

  · **A test that never runs.** A file named `tests_notes.py`, a directory with no
    `test_` prefix, a class pytest does not collect — the suite goes green with a
    hole in it, and the hole is invisible because nothing reports "0 collected from
    this file".
  · **CI running a different command from yours.** `make test` passing and CI
    failing is annoying. The reverse — CI passing on a narrower command — is
    dangerous, because it means the green tick is answering a question nobody asked.

There is no assertion here about the number of tests. A count is a number somebody
updates without reading, and it fails on every honest addition. These check
structure instead.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CI = REPO / ".github" / "workflows" / "ci.yml"
WORKFLOW = CI.read_text()
MAKEFILE = (REPO / "Makefile").read_text()


def test_every_python_file_in_tests_is_collected():
    """A file that pytest does not collect is a hole nothing reports.

    `conftest.py` is the one exception: it is configuration, not tests.
    """
    on_disk = {
        p.name for p in (REPO / "tests").rglob("*.py")
        if p.name != "conftest.py" and not p.name.startswith("__")
    }
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout

    missing = {name for name in on_disk if name not in collected}
    assert not missing, (
        f"in tests/ and never collected: {sorted(missing)}. "
        "A file pytest ignores is a test suite that lies about its coverage."
    )


def test_no_python_file_in_tests_is_misnamed():
    """`tests_store.py` and `test-store.py` are collected by nothing.

    Caught here rather than by the collection check above, because this one names the
    actual mistake instead of reporting the consequence.
    """
    wrong = [
        p.name for p in (REPO / "tests").rglob("*.py")
        if p.name != "conftest.py"
        and not p.name.startswith("__")
        and not re.fullmatch(r"test_[a-z0-9_]+\.py", p.name)
    ]
    assert not wrong, f"named so that pytest skips them: {wrong}"


def test_ci_runs_the_whole_suite_in_both_jobs():
    """Both environments run every test, not a subset.

    The two jobs are the opposite halves of the portability claim: `core` has no `gi`
    at all, `calendar` has the typelibs. A `pytest tests/test_notes.py` in either one
    would turn "the core is portable" back into a marketing sentence.
    """
    runs = re.findall(r"pytest[^\n]*", WORKFLOW)
    assert len(runs) >= 2, f"expected a pytest run per job, found {runs}"
    for run in runs:
        assert " tests/" not in run and "::" not in run, (
            f"CI runs a subset: {run!r}"
        )
        assert "-k " not in run, f"CI filters tests: {run!r}"
        assert "--ignore" not in run, f"CI skips a path: {run!r}"


def test_ci_and_the_makefile_lint_the_same_paths():
    """A path linted locally and not in CI is a path that drifts.

    `scripts/` was exactly that: it was born outside both lists, and the guards that
    live in it would have gone unlinted while being the thing enforcing the rules.
    """
    def paths(text: str) -> set[str]:
        m = re.search(r"ruff check ([^\n]*)", text)
        return set(m.group(1).split()) if m else set()

    assert paths(MAKEFILE), "the Makefile does not lint anything"
    assert paths(MAKEFILE) == paths(WORKFLOW), (
        f"make lint: {sorted(paths(MAKEFILE))}\n"
        f"CI lint:   {sorted(paths(WORKFLOW))}"
    )


def test_lint_covers_every_python_directory():
    """Including the one added last week.

    A directory of Python that nothing lints is where the next `F821` hides — and an
    `F821` from a mechanical rename is precisely the class of bug ruff caught twice
    in this project's history.
    """
    linted = set(re.search(r"ruff check ([^\n]*)", MAKEFILE).group(1).split())
    with_python = {
        d.name for d in REPO.iterdir()
        if d.is_dir() and not d.name.startswith(".") and list(d.rglob("*.py"))
    }
    # `src` is linted as `src`; the package inside it is not a separate entry.
    assert with_python <= linted, f"has Python and is not linted: {sorted(with_python - linted)}"


@pytest.mark.parametrize(
    "guard", ["scan_secrets.py", "scan_language.py", "pr_title.py"]
)
def test_every_guard_script_runs_in_ci(guard):
    """A guard in `scripts/` that CI never calls is a guard that does not exist.

    It is easy to write one, test it, and forget the workflow line — and the result
    looks identical to having it, because the tests are green.
    """
    assert guard in WORKFLOW, f"scripts/{guard} is never run by CI"


def test_the_pr_title_check_only_runs_on_pull_requests():
    """There is no title to check on a push to `main`.

    Running it there would either fail every push or need a branch full of `if`, and
    both teach people to ignore the job.
    """
    block = WORKFLOW[WORKFLOW.index("pr_title.py") - 1200:]
    assert "pull_request" in WORKFLOW
    assert "github.event.pull_request.title" in block, (
        "the check does not read the actual PR title"
    )


def test_the_templates_exist_and_ask_for_what_halves_the_work():
    """`ta doctor` output in a bug report answers most of the first round trip.

    Six capabilities, each with a reason and a fix — that is usually the whole
    diagnosis, and asking for it in the template costs the reporter one command.
    """
    bug = (REPO / ".github" / "ISSUE_TEMPLATE" / "bug.md").read_text()
    assert "ta doctor" in bug, "the bug template does not ask for `ta doctor`"

    pr = (REPO / ".github" / "pull_request_template.md").read_text()
    for expected in ("make lint", "make test"):
        assert expected in pr, f"the PR template does not mention {expected}"


def test_the_pr_template_still_asks_for_the_screen():
    """The one checklist item this project paid for five times.

    A clipped counter, an invisible swatch, a warning hidden behind a post-it,
    dark-on-dark text, and a delete button that had escaped its card. Every one of
    them passed "returns 200 and contains the string".
    """
    pr = (REPO / ".github" / "pull_request_template.md").read_text()
    assert "looked at the screen" in pr or "look at the screen" in pr


# ── The rules for agents ────────────────────────────────────────────────────
def test_the_agent_rules_exist_and_have_one_owner():
    """`AGENTS.md` is the file; `CLAUDE.md` points at it.

    One file, because Claude Code, Copilot and Cursor reading three copies of the
    same rules means three copies that drift — and the one that drifts is always the
    one the next agent happens to read.
    """
    agents = REPO / "AGENTS.md"
    claude = (REPO / "CLAUDE.md").read_text()

    assert agents.exists()
    assert "AGENTS.md" in claude, "CLAUDE.md does not point at AGENTS.md"
    assert len(claude.splitlines()) < 15, (
        "CLAUDE.md is growing rules of its own — they belong in AGENTS.md"
    )


def test_the_agent_rules_link_rather_than_duplicate():
    """Same single-owner rule the documentation follows.

    The type table, the scope list and the reasoning live in CONTRIBUTING. If they
    were copied here, the copy would be the one an agent reads after the original
    changed.
    """
    agents = (REPO / "AGENTS.md").read_text()
    assert "CONTRIBUTING.md" in agents, "AGENTS.md does not link to its owner"

    duplicated = [t for t in ("| `feat` |", "| `fix` |", "| `refactor` |") if t in agents]
    assert not duplicated, f"the type table is duplicated in AGENTS.md: {duplicated}"


def test_every_command_the_agent_rules_prescribe_exists():
    """An agent that runs what this file says must not hit `No such file`.

    The three scripts and the two make targets are named as things to run before
    finishing. A stale name here produces an agent that reports the check as run.
    """
    agents = (REPO / "AGENTS.md").read_text()

    for script in re.findall(r"scripts/(\w+\.py)", agents):
        assert (REPO / "scripts" / script).exists(), f"AGENTS.md names a missing {script}"

    for target in re.findall(r"make (\w+)", agents):
        assert re.search(rf"^{target}:", MAKEFILE, re.M), (
            f"AGENTS.md tells agents to run `make {target}`, which the Makefile lacks"
        )
