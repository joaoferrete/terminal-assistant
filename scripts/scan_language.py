"""Find Portuguese where the code should be English.

The rule this enforces is in CONTRIBUTING.md, and it is one question: does a person
or a database ever see this string? Identifiers, comments and docstrings are read
by nobody outside the repository, so they are English. Stored tags, group names and
the marks people type stay Portuguese forever.

**Why this is a script and not a habit.** The translation pass was verified by
hand, three times, by counting accented characters — and

    canceladas ficam de fora: revisar prazo de coisa encerrada gasta chamada de
    modelo por nada

has no accent in it and survived all three rounds. Then, in the very PR that
removed Portuguese from the code, four brand-new tests were written in Portuguese:
the sweep covered the files being translated, and a new file is in no list.

So the detection is by **function word**, not by accent, and it runs in CI rather
than in whoever remembers to run it.

## What it deliberately does not do

It does not try to identify the language of prose. It looks for short Portuguese
words that essentially never appear in English, needing two distinct ones on the
same line, or one accented Portuguese letter pattern. That is enough to catch a
sentence and cheap enough to never argue with a proper noun.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Function words that are Portuguese and are not English words. `no`, `a`, `e`,
# `da` and `os` are excluded on purpose: they collide with English or with
# identifiers, and a guard with false positives gets silenced.
#
# A wrapped list literal, and it has to stay wrapped: `ruff --fix` turns a
# `"…".split()` into a single 700-character literal that then fails E501, so the two
# rules want opposite things and only the hand-wrapped form satisfies both.
FUNCTION_WORDS = [
    "nao", "que", "para", "com", "uma", "nem", "mas", "entao", "porque",
    "quando", "onde", "qual", "quais", "quem", "isso", "isto", "esse", "essa",
    "aquele", "aquela", "pelo", "pela", "pelos", "pelas", "nele", "nela",
    "ficam", "fica", "ficar", "deixa", "deixar", "faz", "fazer", "sempre",
    "nunca", "tudo", "todos", "todas", "muito", "apenas", "cada", "pode",
    "podem", "precisa", "precisam", "ainda", "depois", "antes", "sobre",
    "entre", "desde", "ate", "assim", "tambem", "mesmo", "mesma", "outro",
    "outra", "outros", "seria", "fosse", "estao", "estava", "eram", "sendo",
    "foi", "ser", "tem", "havia", "teria", "coisa", "jeito", "modo", "forma",
    "serve", "vira", "volta", "vale", "conta",
]

WORD_RE = re.compile(
    r"(?<![\w-])(" + "|".join(FUNCTION_WORDS) + r")(?![\w-])", re.IGNORECASE
)

# An accented letter adjacent to an ASCII letter. That shape is Portuguese prose;
# a bare `×`, `→` or `ΔE` is notation and must not trip anything.
ACCENT_RE = re.compile(r"[a-zA-Z][àáâãäçéêëíîïóôõöúûü]|[àáâãäçéêëíîïóôõöúûü][a-zA-Z]")

# Where Portuguese is the CORRECT answer, each with the reason. This list is the
# interface of this check: adding to it is a decision, so it is small and explained.
ALLOWED = {
    # The catalogue holds both languages side by side by design.
    "src/ta/i18n.py": "the message catalogue is bilingual on purpose",
    # The prompts are one tuned artefact; retuning them is its own change.
    "src/ta/llm.py": "the model prompts are a declared gap (CONTRIBUTING)",
    # Its headings end up inside the stored document.
    "src/ta/priorities.py": "the template text is stored in the user's document",
    # The Portuguese vocabulary the parser matches against IS the feature.
    "src/ta/notes.py": "the Portuguese date and priority vocabulary is the parser",
    # This file quotes Portuguese in order to explain what it looks for.
    "scripts/scan_language.py": "it quotes what it looks for",
}


# Inline code and quoted strings are stripped before a line is judged. English
# prose in this repository quotes Portuguese constantly, and correctly: `@sexta`,
# `!alta`, `Lâmpada do quarto` and `na terça` are the DATA under test, and a
# sentence explaining them is doing its job. Judging the quotes would mean either a
# permanent false positive or an allowlist entry for half the test suite — and an
# allowlist that grows to cover correct code stops being a decision and becomes a
# habit of switching the check off.
QUOTED_RE = re.compile(r"`[^`]*`|\"[^\"\n]*\"|'[^'\n]*'")


def offending_lines(text: str) -> list[tuple[int, str]]:
    """Lines that read as Portuguese prose, by number."""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        bare = QUOTED_RE.sub(" ", line)
        hits = {m.group(1).lower() for m in WORD_RE.finditer(bare)}
        if len(hits) >= 2 or ACCENT_RE.search(bare):
            out.append((i, line.strip()))
    return out


def _prose_of_python(path: Path) -> str:
    """Only the comments and docstrings — the parts that must be English.

    Parsing rather than reading the whole file matters: a Portuguese string
    LITERAL is usually data (a stored tag, a parser vocabulary) and is allowed,
    while a Portuguese comment never is. Reading the raw file would conflate them
    and force half the repository into the allowlist.
    """
    source = path.read_text(encoding="utf-8")
    pieces: list[str] = []

    # Comments, via the tokenizer: a `#` inside a string is not a comment.
    import io
    import tokenize

    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                pieces.append(tok.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pieces.append(source)

    # Docstrings, via the AST.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "\n".join(pieces)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node)
            if doc:
                pieces.append(doc)
    return "\n".join(pieces)


def _prose_of_html(path: Path) -> str:
    """`/* … */`, `// …` and `<!-- … -->` from the board."""
    source = path.read_text(encoding="utf-8")
    found = re.findall(r"/\*.*?\*/|//[^\n]*|<!--.*?-->", source, re.S)
    return "\n".join(found)


def scan() -> list[str]:
    out: list[str] = []
    targets = [
        *sorted((REPO / "src").rglob("*.py")),
        *sorted((REPO / "tests").rglob("*.py")),
        *sorted((REPO / "examples").rglob("*.py")),
        *sorted((REPO / "scripts").rglob("*.py")),
        *sorted((REPO / "src").rglob("*.html")),
    ]
    for path in targets:
        rel = str(path.relative_to(REPO))
        if rel in ALLOWED:
            continue
        prose = _prose_of_html(path) if path.suffix == ".html" else _prose_of_python(path)
        for line, text in offending_lines(prose):
            out.append(f"{rel}: {text[:96]}" + (f"  (prose line {line})" if line else ""))
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list-allowed", action="store_true",
                    help="print the files where Portuguese is correct, and why")
    args = ap.parse_args(argv[1:])

    if args.list_allowed:
        for path, why in sorted(ALLOWED.items()):
            print(f"  {path}\n      {why}")
        return 0

    found = scan()
    if not found:
        print("no Portuguese in comments or docstrings outside the allowed files")
        return 0

    print("Portuguese where the code should be English:\n", file=sys.stderr)
    for f in found:
        print(f"  · {f}", file=sys.stderr)
    print(
        f"\n{len(found)} line(s). The rule, with the table, is in CONTRIBUTING.md "
        '("Language").\nIf Portuguese is genuinely correct here, add the file to '
        "ALLOWED in scripts/scan_language.py **with the reason** — that list is a "
        "set of decisions, not a mute button.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
