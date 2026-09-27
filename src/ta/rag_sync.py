"""The Satellite's half of the folder index (F7): which files go, and sending them.

Only text a person would want searched: documents and code, under a size limit,
outside the directories that are machinery (`.git`, `node_modules`, virtualenvs).
**Secrets never leave the laptop**: hidden files are skipped, and so is anything
whose name says it holds credentials — `.env`, keys, certificates. The server's
index is a place many searches read from; a leaked token there would be quoted
back into a chat.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

TEXT_SUFFIXES = frozenset({
    ".md", ".markdown", ".txt", ".rst", ".org", ".adoc",
    ".py", ".go", ".rs", ".java", ".kt", ".scala", ".js", ".ts", ".tsx", ".jsx",
    ".rb", ".php", ".c", ".h", ".cpp", ".hpp", ".cs", ".swift", ".sh", ".bash",
    ".sql", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".proto", ".tf", ".html", ".css",
})
SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "__pycache__", "dist",
                       "build", "target", ".idea", ".vscode", ".mypy_cache", ".ruff_cache",
                       ".pytest_cache", "vendor"})
SECRET_WORDS = ("secret", "credential", "password", "passwd", "token", "private_key",
                "id_rsa", "id_ed25519")
SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".keystore", ".jks"})
MAX_BYTES = 512 * 1024


def eligible(path: Path) -> bool:
    name = path.name.lower()
    # `.env`, `.envrc` are hidden; `env`, `env.local` are not, and are the same thing.
    if name.startswith(".") or name == "env" or name.startswith("env."):
        return False
    if path.suffix.lower() in SECRET_SUFFIXES or any(w in name for w in SECRET_WORDS):
        return False
    return path.suffix.lower() in TEXT_SUFFIXES


def walk(folder: Path) -> Iterator[Path]:
    for p in sorted(folder.iterdir()) if folder.is_dir() else ():
        if p.is_symlink():
            continue       # a link out of the folder would index what nobody chose
        if p.is_dir():
            if p.name not in SKIP_DIRS and not p.name.startswith("."):
                yield from walk(p)
        elif p.is_file() and eligible(p) and p.stat().st_size <= MAX_BYTES:
            yield p


def display(path: Path) -> str:
    home = Path.home()
    try:
        return "~/" + str(path.resolve().relative_to(home))
    except ValueError:
        return str(path.resolve())


def collect(folders: list[str]) -> dict[str, Path]:
    """Every eligible file in the configured folders, by the path the index shows."""
    found: dict[str, Path] = {}
    for f in folders:
        for p in walk(Path(f).expanduser()):
            found[display(p)] = p
    return found


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sync(folders: list[str], post) -> tuple[int, int]:
    """Send what changed. `post(route, body) -> dict` talks to the server.
    Returns (sent, forgotten)."""
    files = collect(folders)
    hashes = {k: sha(p) for k, p in files.items()}
    answer = post("/rag/manifest", {"files": hashes})
    sent = 0
    for key in answer.get("need", []):
        path = files.get(key)
        if path is None:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue          # not text after all
        post("/rag/file", {"path": key, "sha": hashes[key], "text": text})
        sent += 1
    return sent, answer.get("forgotten", 0)
