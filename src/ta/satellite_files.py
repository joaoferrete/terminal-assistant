"""Files on a Satellite, on request: list, read, send (F9, D41).

This runs on the **laptop**, and every decision about what may leave it is made
here, from the laptop's own `[files] folders`. The server only asks: it cannot
name a folder outside that list, and a path that climbs out of it (`..`, a
symlink) is refused after resolving, not by looking at the string.

Read-only. There is no operation that writes, moves or deletes, so there is
nothing for a tainted turn to trick the agent into.
"""

from __future__ import annotations

import base64
from pathlib import Path

from .rag_sync import SECRET_SUFFIXES, SECRET_WORDS, SKIP_DIRS, display

MAX_LIST = 200
MAX_READ_BYTES = 1024 * 1024        # a text file larger than this is not "read" in a chat
MAX_READ_CHARS = 20_000             # what the model gets of it
MAX_SEND_BYTES = 20 * 1024 * 1024   # Telegram takes 50 MB from a bot; the JSON hop doubles it


class Refused(Exception):
    """Said back to the server as the answer's error: the agent tells the Member."""


def _secret(p: Path) -> bool:
    # The same filter the folder index uses (T7.3), plus hidden names: F7's
    # `eligible` also requires a text suffix, which would stop a PDF being sent.
    name = p.name.lower()
    return (name.startswith(".") or name == "env" or name.startswith("env.")
            or p.suffix.lower() in SECRET_SUFFIXES or any(w in name for w in SECRET_WORDS))


def roots(folders: list[str]) -> list[Path]:
    return [r for r in (Path(f).expanduser().resolve() for f in folders) if r.is_dir()]


def resolve(raw: str, folders: list[str]) -> Path:
    """The path the server named, if it is inside an allowed folder once resolved.

    Accepts what `list` shows (`~/Documentos/x.pdf`) or a path relative to the
    first folder. Every component between the root and the file is checked, so
    `~/Documentos/.ssh/config` is refused even though the folder is allowed.
    """
    allowed = roots(folders)
    if not allowed:
        raise Refused("no folder is shared on this computer ([files] folders)")
    text = raw.strip()
    candidate = Path(text).expanduser() if text.startswith(("~", "/")) else allowed[0] / text
    real = candidate.resolve()
    root = next((r for r in allowed if real == r or real.is_relative_to(r)), None)
    if root is None:
        raise Refused(f"{text} is outside the shared folders")
    for part in real.relative_to(root).parts:
        if part in SKIP_DIRS or _secret(Path(part)):
            raise Refused(f"{text} is hidden or looks like a secret")
    if not real.exists():
        raise Refused(f"{text} does not exist")
    return real


def list_(raw: str, folders: list[str]) -> dict:
    if not raw.strip():
        return {"entries": [{"path": display(r), "dir": True} for r in roots(folders)]}
    folder = resolve(raw, folders)
    if not folder.is_dir():
        raise Refused(f"{raw} is not a folder")
    entries = []
    for p in sorted(folder.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        if p.is_symlink() or p.name in SKIP_DIRS or _secret(p):
            continue
        entries.append({"path": display(p), "dir": p.is_dir(),
                        "size": 0 if p.is_dir() else p.stat().st_size})
        if len(entries) >= MAX_LIST:
            break
    return {"entries": entries}


def read(raw: str, folders: list[str]) -> dict:
    p = resolve(raw, folders)
    if not p.is_file():
        raise Refused(f"{raw} is not a file")
    if p.stat().st_size > MAX_READ_BYTES:
        raise Refused(f"{raw} is too large to read here; ask to send it instead")
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise Refused(f"{raw} is not a text file; ask to send it instead") from None
    return {"path": display(p), "text": text[:MAX_READ_CHARS],
            "truncated": len(text) > MAX_READ_CHARS}


def send(raw: str, folders: list[str]) -> dict:
    p = resolve(raw, folders)
    if not p.is_file():
        raise Refused(f"{raw} is not a file")
    if p.stat().st_size > MAX_SEND_BYTES:
        raise Refused(f"{raw} is larger than {MAX_SEND_BYTES // (1024 * 1024)} MB")
    return {"name": p.name, "data": base64.b64encode(p.read_bytes()).decode()}


OPS = {"list": list_, "read": read, "send": send}


def handle(request: dict, folders: list[str]) -> dict:
    """One question from the server → its answer. Never raises: a refusal is an
    answer too, and a crash would leave the agent waiting for the timeout."""
    op = OPS.get(request.get("op", ""))
    if op is None:
        return {"error": f"unknown operation {request.get('op')!r}"}
    try:
        return op(str(request.get("path", "")), folders)
    except Refused as e:
        return {"error": str(e)}
    except OSError as e:
        return {"error": f"could not open it ({type(e).__name__})"}
