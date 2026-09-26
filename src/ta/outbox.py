"""The Satellite's capture queue (T6.2, invariant 5): `ta note` never waits.

When the server does not answer, a Note goes here — a JSON line with its text and
the moment it was typed — and is sent later with that moment, so "amanhã" written
on Monday still means Tuesday. Only capture queues: reading needs the server, and
a list shown from a stale copy would lie.

A plain file, 0600, next to the local data: it holds what somebody wrote.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path


def path() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ta" / "outbox.jsonl"


def add(text: str, *, now: datetime | None = None, where: Path | None = None) -> None:
    target = where or path()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"text": text,
                            "captured_at": (now or datetime.now()).isoformat(timespec="seconds")},
                           ensure_ascii=False) + "\n")


def pending(where: Path | None = None) -> list[dict]:
    target = where or path()
    if not target.exists():
        return []
    return [json.loads(line) for line in target.read_text().splitlines() if line.strip()]


def flush(send, where: Path | None = None) -> tuple[int, int]:
    """Send what is queued, in order, through `send(item)`. What fails stays, in
    order, for next time. Returns (sent, left)."""
    target = where or path()
    items = pending(target)
    if not items:
        return 0, 0
    left: list[dict] = []
    for i, item in enumerate(items):
        try:
            send(item)
        except Exception:
            left = items[i:]      # the server went away again: keep the rest as is
            break
    if left:
        tmp = target.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(json.dumps(x, ensure_ascii=False) + "\n" for x in left)
        tmp.replace(target)
    else:
        target.unlink()
    return len(items) - len(left), len(left)
