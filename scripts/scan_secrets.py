"""Look for credentials in the tree and, optionally, in the history.

This exists because it already happened. A `.env.bak-1786485474` created before
editing the real `.env`, plus a `git add -A`, carried a live `HA_TOKEN` and
`GEMINI_API_KEY` into five commits. `.gitignore` covered `.env` and not its
copies. It was caught by a manual sweep before any push and purged with
`filter-branch` — but "caught by a manual sweep" is another way of saying "caught
because somebody happened to look".

Two checks, because they fail in different ways:

  · **Tracked files.** What is in the tree right now. Fast, and the one that
    matters before a commit.
  · **History** (`--history`). Every blob reachable from any ref. A secret removed
    in a later commit is still in the pack, still in every clone, and still valid
    until somebody rotates it. This is the check that would have caught the real
    incident at the moment it happened.

Run it before pushing anything you are unsure about:

    python scripts/scan_secrets.py --history
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# Patterns for credentials this project actually handles, plus the few that turn up
# in any repository. Each is anchored on a vendor prefix rather than on entropy:
# entropy heuristics flag base64 test fixtures and minified assets, and a scanner
# that cries wolf gets switched off.
PATTERNS: dict[str, re.Pattern[str]] = {
    # Google / Gemini API key. `GEMINI_API_KEY` is one of this project's two.
    "Google API key": re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    # A Home Assistant long-lived access token is a JWT. This is the other one, and
    # it is the dangerous half: it commands the house, not just a model quota.
    "JWT (Home Assistant long-lived token)": re.compile(
        r"eyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
    ),
    "OpenAI key": re.compile(r"sk-[A-Za-z0-9]{20,}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),
    "AWS access key id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    # An assignment that looks like a real value. Deliberately last and deliberately
    # narrow: it requires an assignment, a plausible name, and 24+ characters that
    # are not obviously a placeholder.
    "assigned secret": re.compile(
        r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)\b\s*[=:]\s*"
        r"[\"']?([A-Za-z0-9_\-/+]{24,})[\"']?"
    ),
}

# Values that are examples, not secrets. Anything whose captured value contains one
# of these is a placeholder, and flagging it would train people to pass `--force`.
PLACEHOLDERS = (
    "xxx", "your", "seu-", "sua-", "changeme", "troque", "example", "exemplo",
    "placeholder", "fake", "dummy", "abc123", "0000", "aaaa", "...", "<", ">",
    "cole-", "paste", "here", "aqui", "test", "teste", "sample", "redacted",
    "not-a-secret", "nao-e-segredo", "token-de-teste",
)

# Files whose whole point is to show the SHAPE of a credential.
ALLOWED_PATHS = {
    ".env.example",
    "scripts/scan_secrets.py",       # the patterns themselves live here
    "tests/test_scan_secrets.py",    # and the fixtures that prove them
}

# Text only. A binary blob with a matching byte run is noise, and a screenshot is
# not where a token hides.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ico",
                 ".woff", ".woff2", ".ttf", ".zip", ".gz", ".db", ".sqlite"}


def _is_placeholder(value: str) -> bool:
    low = value.lower()
    return any(p in low for p in PLACEHOLDERS)


def findings(text: str, where: str) -> list[str]:
    """Every credential-looking thing in `text`, named by pattern and location."""
    out = []
    for name, pattern in PATTERNS.items():
        for m in pattern.finditer(text):
            # The captured group when there is one (the value), else the whole match.
            value = m.group(1) if m.groups() else m.group(0)
            if _is_placeholder(value):
                continue
            line = text.count("\n", 0, m.start()) + 1
            shown = value[:6] + "…" if len(value) > 8 else value
            out.append(f"{where}:{line}: {name} ({shown})")
    return out


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout


def scan_tracked() -> list[str]:
    """Every tracked text file, as it is right now."""
    out: list[str] = []
    for rel in _git("ls-files", "-z").split("\0"):
        if not rel or rel in ALLOWED_PATHS:
            continue
        path = REPO / rel
        if path.suffix.lower() in SKIP_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        out += findings(text, rel)
    return out


def scan_tracked_names() -> list[str]:
    """Files that must never be tracked at all, whatever is inside them.

    `.env.bak-1786485474` was valid UTF-8 with real values in it, so a content scan
    would have caught it too — but the NAME is the cheaper signal, it is decidable
    without reading anything, and it also catches the case where the content scan's
    patterns do not know a vendor's format yet.
    """
    bad = []
    for rel in _git("ls-files", "-z").split("\0"):
        if not rel:
            continue
        name = Path(rel).name
        if name.startswith(".env") and rel not in ALLOWED_PATHS:
            bad.append(f"{rel}: a secret file must not be tracked")
        if name.endswith((".pem", ".p12", ".pfx")) or name == "id_rsa":
            bad.append(f"{rel}: a key file must not be tracked")
    return bad


def scan_history() -> list[str]:
    """Every blob reachable from any ref, including ones no commit still points at.

    A secret deleted in a later commit is still in the pack and still in every
    clone. This walks the object database rather than the diffs, so a file added and
    removed in the same branch is still seen.
    """
    out: list[str] = []
    objects = _git("rev-list", "--objects", "--all").splitlines()
    blobs: dict[str, str] = {}
    for line in objects:
        sha, _, name = line.partition(" ")
        if name and Path(name).suffix.lower() not in SKIP_SUFFIXES:
            blobs[sha] = name

    if not blobs:
        return out

    # One `cat-file --batch` for everything: a subprocess per blob makes this take
    # minutes on a repo with any history at all.
    # `input` as bytes, and no `text=True`: the blobs are arbitrary bytes, and
    # decoding the whole batch as UTF-8 would raise on the first binary one.
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=REPO, input=("\n".join(blobs) + "\n").encode(),
        capture_output=True, check=True,
    )
    data, pos = proc.stdout, 0
    while pos < len(data):
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        header = data[pos:nl].decode("utf-8", "replace").split()
        pos = nl + 1
        if len(header) < 3 or header[1] != "blob":
            continue
        sha, size = header[0], int(header[2])
        body, pos = data[pos:pos + size], pos + size + 1
        name = blobs.get(sha, sha)
        if name in ALLOWED_PATHS:
            continue
        try:
            out += findings(body.decode("utf-8"), f"history:{name}@{sha[:8]}")
        except UnicodeDecodeError:
            continue
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--history", action="store_true",
        help="also walk every blob in the object database, not just the tree",
    )
    args = ap.parse_args(argv[1:])

    found = scan_tracked_names() + scan_tracked()
    scanned = "tracked files"
    if args.history:
        found += scan_history()
        scanned += " and the whole history"

    if not found:
        print(f"no credential found in {scanned}")
        return 0

    print(f"Possible credential in {scanned}:\n", file=sys.stderr)
    for f in sorted(set(found)):
        print(f"  · {f}", file=sys.stderr)
    print(
        "\nIf it is real: rotate it FIRST, then remove it. Rotating comes first "
        "because the value is already in every clone.\n"
        "If it is an example, make it look like one — see PLACEHOLDERS in "
        "scripts/scan_secrets.py.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
