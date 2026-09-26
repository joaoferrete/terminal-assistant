"""Search by meaning over Satellite folders (F7).

The model is a fake that turns words into counts: what is under test is the
index, who may search it, and what never leaves the laptop.
"""
import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_security import TOKEN, build

from ta import board_access, db, rag, rag_sync
from ta.builtin_tools import ToolContext
from ta.grants import OWNER_PERMISSIONS
from ta.tools import Turn, registered
from ta.tools import run as run_tool

VOCAB = ["kafka", "consumer", "erro", "lag", "receita", "bolo", "farinha", "deploy"]


class WordEmbedder:
    """A bag of words over a tiny vocabulary: similar words, similar vectors."""

    def embed(self, texts):
        return [[float(t.lower().count(w)) + 0.001 for w in VOCAB] for t in texts]


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    c.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    return c


def index(conn, member, path, text):
    rag.index_file(conn, WordEmbedder(), member_id=member, path=path, sha="s", text=text)


def test_long_text_is_cut_into_overlapping_chunks_at_line_breaks():
    text = "\n".join(f"linha {i} " + "x" * 80 for i in range(40))
    parts = rag.chunks(text)
    assert len(parts) > 3 and all(len(p) <= rag.CHUNK for p in parts)
    assert parts[1].split("\n")[0] in parts[0], "neighbours overlap"


def test_the_closest_chunk_comes_first(conn):
    index(conn, 1, "~/notas/kafka.md", "O consumer do kafka estava com lag e erro")
    index(conn, 1, "~/notas/bolo.md", "receita de bolo com farinha")
    hits = rag.search(conn, WordEmbedder(), "erro no consumer kafka", member_id=1, is_owner=True)
    assert hits[0].path == "~/notas/kafka.md"


def test_a_member_searches_their_own_folders_and_the_owner_searches_all(conn):
    index(conn, 1, "~/owner.md", "deploy do kafka")
    index(conn, 2, "~/ana.md", "receita de bolo")
    ana = rag.search(conn, WordEmbedder(), "kafka bolo", member_id=2, is_owner=False)
    assert {h.path for h in ana} == {"~/ana.md"}
    owner = rag.search(conn, WordEmbedder(), "kafka bolo", member_id=1, is_owner=True)
    assert {h.path for h in owner} == {"~/owner.md", "~/ana.md"}


def test_reindexing_a_file_replaces_its_chunks(conn):
    index(conn, 1, "~/a.md", "kafka")
    index(conn, 1, "~/a.md", "bolo")
    assert conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0] == 1


def tool_ctx(conn, member, in_group=False):
    turn = Turn(member_id=member, conversation_id="c", in_group=in_group,
                permissions=OWNER_PERMISSIONS)
    return ToolContext(conn=conn, turn=turn, channel="telegram",
                       services={"embedder": WordEmbedder()})


def test_docs_search_cites_the_file_and_taints_the_turn(conn):
    index(conn, 1, "~/notas/kafka.md", "o erro de lag no consumer")
    ctx = tool_ctx(conn, 1)
    out = asyncio.run(run_tool(registered()["docs_search"], ctx.turn, ctx, {"query": "lag"}))
    assert "lag" in out.text and ctx.turn.sources[0].ref == "~/notas/kafka.md"
    assert ctx.turn.tainted, "a file holds text from many hands"


def test_docs_search_never_answers_in_a_group(conn):
    index(conn, 1, "~/segredo.md", "kafka")
    ctx = tool_ctx(conn, 1, in_group=True)
    out = asyncio.run(run_tool(registered()["docs_search"], ctx.turn, ctx, {"query": "kafka"}))
    assert "private" in out.text and ctx.turn.sources == []


# ── What leaves the laptop ──────────────────────────────────────────────────
@pytest.mark.parametrize(("name", "ok"), [
    ("notas.md", True), ("main.py", True), ("envio.md", True),
    (".env", False), ("env", False), ("env.local", False), ("server.pem", False),
    ("aws_credentials.yaml", False), ("id_rsa", False), ("foto.png", False),
])
def test_secrets_and_binaries_never_leave_the_laptop(name, ok):
    assert rag_sync.eligible(Path(name)) is ok


def test_machinery_directories_are_skipped(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("x")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD.md").write_text("x")
    assert [p.name for p in rag_sync.walk(tmp_path)] == ["a.md"]


def test_sync_sends_only_what_changed_and_forgets_what_is_gone(tmp_path, conn):
    (tmp_path / "a.md").write_text("kafka consumer")
    (tmp_path / "b.md").write_text("bolo")
    sent = []

    def post(route, body):
        if route == "/rag/manifest":
            known = rag.manifest(conn, 1)
            gone = [p for p in known if p not in body["files"]]
            rag.forget(conn, 1, gone)
            return {"need": [p for p, s in body["files"].items() if known.get(p) != s],
                    "forgotten": len(gone)}
        sent.append(body["path"])
        rag.index_file(conn, WordEmbedder(), member_id=1, path=body["path"], sha=body["sha"],
                       text=body["text"])
        return {"chunks": 1}

    assert rag_sync.sync([str(tmp_path)], post) == (2, 0)
    assert rag_sync.sync([str(tmp_path)], post) == (0, 0), "nothing changed, nothing sent"
    (tmp_path / "b.md").unlink()
    assert rag_sync.sync([str(tmp_path)], post) == (0, 1)


def test_the_routes_index_as_the_satellites_member(tmp_path):
    with TestClient(build(tmp_path, host="0.0.0.0", token=TOKEN)) as c:
        c.app.state.embedder = WordEmbedder()
        side = db.connect(tmp_path / "t.db")
        side.execute("INSERT INTO members (id, handle, is_owner, created_at)"
                     " VALUES (2, 'ana', 0, 'x')")
        ana = {"Authorization": f"Bearer {board_access.session_cookie(TOKEN, 2)}"}
        need = c.post("/rag/manifest", json={"files": {"~/a.md": "h1"}}, headers=ana).json()
        assert need["need"] == ["~/a.md"]
        r = c.post("/rag/file", json={"path": "~/a.md", "sha": "h1", "text": "receita de bolo"},
                   headers=ana)
        assert r.json() == {"chunks": 1}
    assert side.execute("SELECT member_id FROM rag_files").fetchone()[0] == 2
