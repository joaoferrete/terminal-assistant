"""The daemon exposes 29 routes and has no authentication of its own.

While this was a one-machine assistant, `0.0.0.0` was a conscious and documented
choice — the board opens on your phone. Published, that same default hands every
note, the command over the house and the model key to any wifi neighbour of
whoever follows the step-by-step.

These tests pin both halves of the decision (ADR 0012): the default is loopback,
and leaving it requires a credential. They are tests of a default, and that is
exactly why they exist — a wrong default does not error, it works.
"""
from pathlib import Path

import pytest
from starlette.testclient import TestClient

# With no `__init__.py` in `tests/`, pytest puts the directory on the path and the
# import is direct. The doubles are the same ones `test_daemon` uses: a test talks
# to neither Evolution Data Server nor the user's `gsettings`.
from test_daemon import FakeCalendar, FakeLighter

from ta.config import DEFAULT_HOST, Config, ConfigError
from ta.daemon import create_app

TOKEN = "a-test-token-not-a-secret"
REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / "src" / "ta" / "web" / "board.html"


def build(tmp_path, **kw):
    return create_app(
        Config(ha_token="fake", gemini_api_key="fake", auto_review=False, **kw),
        db_path=tmp_path / "t.db",
        rules_dir=tmp_path / "no-rules",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )


# ── The default ─────────────────────────────────────────────────────────────
def test_the_default_is_loopback():
    """If somebody switches this back, the test is what shouts.

    It is not a preference: it is the difference between exposing an installer's
    notes to their network or not.
    """
    assert DEFAULT_HOST == "127.0.0.1"
    assert not Config().exposed


def test_loopback_requires_no_token():
    """Local use is exactly as it was. The change must not cost anybody a routine."""
    Config().check()   # does not raise
    assert Config().token is None


# ── Leaving loopback ────────────────────────────────────────────────────────
@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.0.10", "::"])
def test_an_exposed_bind_with_no_token_refuses_to_start(host, tmp_path):
    """It fails loudly and early, not later — nobody rereads a boot log."""
    with pytest.raises(ConfigError) as e:
        build(tmp_path, host=host)

    # The message has to state the fix, not just the problem.
    assert "TA_TOKEN" in str(e.value)
    assert "systemctl --user restart ta" in str(e.value)


def test_an_exposed_bind_with_a_token_starts(tmp_path):
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        r = c.get("/health", headers={"Authorization": f"Bearer {TOKEN}"})
        assert r.status_code == 200


# ── The middleware ──────────────────────────────────────────────────────────
# `TestClient` presents itself as `testclient`, which is not loopback — so it
# falls down the stranger path with no staging required. To exercise the local
# path, the peer is declared explicitly.
def test_a_stranger_with_no_credential_gets_401(tmp_path):
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        r = c.get("/notes")
        assert r.status_code == 401
        assert "TA_TOKEN" in r.json()["error"]


def test_a_stranger_with_the_wrong_token_gets_401(tmp_path):
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        r = c.get("/notes", headers={"Authorization": "Bearer quase-certo"})
        assert r.status_code == 401


def test_the_token_also_works_in_the_query_string(tmp_path):
    """It is how the board boots: `/board?token=…` loads the HTML, and the JS takes over."""
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        assert c.get(f"/board?token={TOKEN}").status_code == 200
        assert c.get("/board").status_code == 401


def test_a_local_client_passes_with_no_credential_even_with_the_daemon_open(tmp_path):
    """`ta note` on your own machine must not have to carry a secret.

    Whoever is here already has the `.env`; demanding a token from them buys no
    security, only friction. The risk the middleware covers is the network.
    """
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN),
                    client=("127.0.0.1", 51000)) as c:
        assert c.get("/notes").status_code == 200


def test_an_ipv4_mapped_peer_counts_as_local(tmp_path):
    """An IPv6 socket accepting IPv4 reports `::ffff:127.0.0.1`.

    Without handling that, the local CLI would start needing a token depending on
    the socket family — a failure that would only show up on somebody else's
    machine.
    """
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN),
                    client=("::ffff:127.0.0.1", 51000)) as c:
        assert c.get("/notes").status_code == 200


# ── The board ───────────────────────────────────────────────────────────────
# There is no JS harness here, so the pin is static — the same pattern
# `test_docs.py` uses for the horizon bands. It exists so the guard does not
# vanish in a refactor: without it, the board on a phone loads the HTML and every
# call 401s, which reads as "the board broke".
def test_the_board_sends_the_credential():
    body = BOARD.read_text()
    assert "Bearer ${token}" in body, "the board stopped sending the token"
    assert "searchParams.get(\"token\")" in body, "the board stopped booting from ?token="


def test_the_board_strips_the_token_from_the_address_bar():
    """Leaving the secret in the URL puts it in the history and in every pasted link."""
    body = BOARD.read_text()
    assert "searchParams.delete(\"token\")" in body
    assert "history.replaceState" in body


# ── What /health is allowed to tell ─────────────────────────────────────────
def test_health_never_returns_a_secret_value(tmp_path):
    """It reports the PRESENCE of a secret. The discipline is old and must not regress."""
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN),
                    client=("127.0.0.1", 51000)) as c:
        body = c.get("/health").text

    for secret in (TOKEN, "fake"):
        assert secret not in body


# ── No secret may enter the repository ──────────────────────────────────────
def test_the_gitignore_covers_env_backups():
    """`.env` was covered; `.env.bak-*` was not.

    A backup of `.env` taken before editing it, plus a `git add -A`, carried a real
    HA_TOKEN and GEMINI_API_KEY into the history. The publication sweep caught it
    and it was purged before any push, but the hole was in `.gitignore` and that is
    where it gets closed.

    Covering only `.env` protects the file and not its copies, which is exactly
    what somebody creates when they are about to touch a secret.
    """
    patterns = (REPO / ".gitignore").read_text()
    assert ".env" in patterns
    assert ".env.bak*" in patterns, "a .env backup is not covered"


def test_no_secret_file_is_tracked():
    """The direct check, so it does not depend on anybody remembering to sweep.

    `.env.example` is the one exception, and exists precisely to be versioned: it
    carries the key names and no values.
    """
    import subprocess

    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout.split()
    suspects = [f for f in out if f.startswith(".env") and f != ".env.example"]
    assert not suspects, f"a secret file is tracked: {suspects}"
