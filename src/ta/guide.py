"""The guide the agent answers "how do I…?" from (F10, D48, D49).

`docs/guide.md` is written by us and kept true by a test, so the agent adapts the
tone of real steps instead of inventing menu names and config keys. Each `##`
section is one topic.

The file lives in the repository, next to the rest of the documentation. Every
install this project documents is a checkout (`make install` is editable), so it is
always there; if it ever is not, the agent says the guide is missing rather than
guessing.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "docs" / "guide.md"


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower())
                   if not unicodedata.combining(c))


@lru_cache(maxsize=1)
def sections(path: Path = PATH) -> dict[str, str]:
    """Title → body, in the guide's order. Empty when the file is missing."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    title = None
    for line in path.read_text().splitlines():
        if line.startswith("## "):
            title = line[3:].strip()
            out[title] = ""
        elif title is not None:
            out[title] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def find(topic: str, path: Path = PATH) -> tuple[str, str] | None:
    """The section a topic names: by title, else the one whose title and body
    share the most words with it."""
    secs = sections(path)
    wanted = _fold(topic).strip()
    if not wanted or not secs:
        return None
    for title, body in secs.items():
        if _fold(title) == wanted:
            return title, body
    words = set(re.findall(r"\w{3,}", wanted))
    scored = sorted(((len(words & set(re.findall(r"\w{3,}", _fold(t)))) * 3
                      + len(words & set(re.findall(r"\w{3,}", _fold(b)))), t, b)
                     for t, b in secs.items()), reverse=True)
    best = scored[0] if scored else None
    return (best[1], best[2]) if best and best[0] > 0 else None
