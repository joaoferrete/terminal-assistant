"""The docs became ten files pointing at each other. A broken link is silent.

The single-owner rule — each fact lives in exactly one file, and everything else
links — only works while the links work. A `docs/aliases.md` renamed to
`shortcuts.md` leaves six pages pointing at nothing, and nothing warns you:
GitHub renders the link happily, it only 404s for whoever clicks it.

It guards the other end too: a new command nobody documented disappears from the
only listing there is, because the flat argparse list was removed on purpose.
"""
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

# Every markdown file in the project, except what is local and unversioned.
DOCS = sorted(
    p for p in REPO.rglob("*.md")
    if ".venv" not in p.parts and p.name != "ROADMAP.md"
)

# `[text](target)`, ignoring images and link references.
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)]+)\)")


def test_there_is_documentation():
    """A guard against this suite passing because it found no files at all."""
    assert len(DOCS) >= 15, [p.name for p in DOCS]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_no_broken_relative_link(doc: Path):
    broken = []
    for target in LINK.findall(doc.read_text()):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        # An anchor inside the file itself: the target is the page, not the
        # heading — checking the heading would mean reimplementing GitHub's slug
        # generation, which would get it wrong more often than right.
        path = target.split("#")[0]
        if not path:
            continue
        if not (doc.parent / path).exists():
            broken.append(target)

    assert not broken, f"{doc.relative_to(REPO)} points at nothing: {broken}"


def test_the_readme_images_exist():
    """A broken image in the README is the first thing a visitor sees."""
    body = (REPO / "README.md").read_text()
    for target in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", body):
        if target.startswith("http"):
            continue
        assert (REPO / target).exists(), f"missing image: {target}"


def test_every_command_shows_up_in_the_documentation():
    """A command in no document does not exist for whoever is reading.

    The flat `argparse` listing was dropped in favour of the grouped epilogue, so
    the documentation is the only complete listing there is.
    """
    from ta.cli import build_parser

    commands = set(build_parser()._subparsers._group_actions[0].choices)
    text = "\n".join(p.read_text() for p in DOCS)

    missing = {c for c in commands if f"ta {c}" not in text}
    assert not missing, f"command never mentioned in the docs: {sorted(missing)}"


def test_the_cited_adrs_exist():
    """A renumbered or renamed ADR breaks the explanation, not just the link."""
    adr = {p.name for p in (REPO / "docs" / "adr").glob("*.md")}
    text = "\n".join(p.read_text() for p in DOCS)

    cited = set(re.findall(r"(\d{4}-[a-z0-9-]+\.md)", text))
    assert cited - adr == set(), f"ADR cited but missing: {sorted(cited - adr)}"
