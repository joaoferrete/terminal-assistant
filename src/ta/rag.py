"""Search by meaning over a Member's Satellite folders (F7).

The Satellite sends the text of the files in the folders it was told to index
(`[rag] folders`); the server cuts them into chunks, embeds them locally and keeps
the vectors in SQLite. The agent searches them with `docs_search`.

Decided with the user on 2026-09-26:

- **Embeddings are local**, on the server, with a small multilingual model in
  ONNX (fastembed, `paraphrase-multilingual-MiniLM-L12-v2`, ~220 MB). Code and
  work documents never leave the house — the same spirit as local speech (D2).
- **Who sees what:** a Member searches their own folders; the **Owner** searches
  every Satellite's; nobody searches from a group (invariant 3).

fastembed is an optional extra (`.[rag]`), like faster-whisper: a server that does
not index anything should not carry an embedding runtime.
"""

from __future__ import annotations

import array
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime

log = logging.getLogger("ta.rag")

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
CHUNK = 900          # characters per chunk, split at a paragraph or a line
OVERLAP = 150
MAX_FILE = 512 * 1024


def available() -> bool:
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return True


class Embedder:
    """The model, loaded on first use: loading costs seconds and the RAM, and a
    server nobody indexes anything on should pay neither."""

    def __init__(self, model: str = MODEL) -> None:
        self.model_name = model
        self._model = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(model_name=self.model_name)
        return [list(map(float, v)) for v in self._model.embed(texts)]


def chunks(text: str) -> list[str]:
    """Pieces of about CHUNK characters, cut at a paragraph or a line when one is
    near, overlapping a little so a sentence at a boundary is findable from both."""
    text = text.strip()
    if len(text) <= CHUNK:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(start + CHUNK, len(text))
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end))
            if cut > start + CHUNK // 2:
                end = cut
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        start = max(end - OVERLAP, start + 1)
    return out


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


# ── Keeping the index ───────────────────────────────────────────────────────
def manifest(conn: sqlite3.Connection, member_id: int) -> dict[str, str]:
    return dict(conn.execute("SELECT path, sha FROM rag_files WHERE member_id = ?",
                             (member_id,)))


def forget(conn: sqlite3.Connection, member_id: int, paths: list[str]) -> int:
    return sum(conn.execute("DELETE FROM rag_files WHERE member_id = ? AND path = ?",
                            (member_id, p)).rowcount for p in paths)


def embed_file(embedder, text: str) -> tuple[list[str], list[list[float]]]:
    """The slow half — cut and embed — kept apart from the database, so the daemon
    can run it in a thread and write on its own loop: its SQLite connection
    belongs to the loop's thread, and the first version of the route crossed it."""
    pieces = chunks(text[:MAX_FILE])
    return pieces, (embedder.embed(pieces) if pieces else [])


def index_file(conn: sqlite3.Connection, embedder, *, member_id: int, path: str, sha: str,
               text: str, now: datetime | None = None) -> int:
    """Replace one file's chunks. Returns how many chunks it now has."""
    pieces, vectors = embed_file(embedder, text)
    return store_file(conn, member_id=member_id, path=path, sha=sha, pieces=pieces,
                      vectors=vectors, now=now)


def store_file(conn: sqlite3.Connection, *, member_id: int, path: str, sha: str,
               pieces: list[str], vectors: list[list[float]],
               now: datetime | None = None) -> int:
    at = (now or datetime.now()).isoformat(timespec="seconds")
    conn.execute("DELETE FROM rag_files WHERE member_id = ? AND path = ?", (member_id, path))
    cur = conn.execute("INSERT INTO rag_files (member_id, path, sha, updated_at)"
                       " VALUES (?, ?, ?, ?)", (member_id, path, sha, at))
    conn.executemany(
        "INSERT INTO rag_chunks (file_id, ordinal, text, vector) VALUES (?, ?, ?, ?)",
        [(cur.lastrowid, i, piece, _pack(v)) for i, (piece, v) in
         enumerate(zip(pieces, vectors, strict=True))])
    return len(pieces)


# ── Searching it ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Hit:
    path: str
    text: str
    score: float
    member_id: int


def search(conn: sqlite3.Connection, embedder, query: str, *, member_id: int,
           is_owner: bool, limit: int = 5) -> list[Hit]:
    """The closest chunks among what this Member may search: their own files, or
    every Satellite's for the Owner. The caller never passes a group Viewer —
    `docs_search` refuses in groups before it gets here."""
    q = embedder.embed([query])[0]
    sql = ("SELECT f.path, c.text, c.vector, f.member_id FROM rag_chunks c"
           " JOIN rag_files f ON f.id = c.file_id")
    rows = conn.execute(sql if is_owner else sql + " WHERE f.member_id = ?",
                        () if is_owner else (member_id,)).fetchall()
    scored = [Hit(r[0], r[1], _cosine(q, _unpack(r[2])), r[3]) for r in rows]
    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:limit]
