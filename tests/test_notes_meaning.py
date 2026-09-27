"""Notes by meaning (F10, D52b): "aquela coisa do carro" finds "trocar o óleo",
among what the asker may see only, and each Note is embedded once per text."""
import asyncio

from ta import builtin_tools, db, store
from ta.grants import OWNER_PERMISSIONS
from ta.tools import Turn, registered
from ta.tools import run as run_tool

AXES = {"carro": 0, "óleo": 0, "oficina": 0, "pão": 1, "padaria": 1, "leite": 1}


class FakeEmbedder:
    """Two concepts: the car, and the bakery. Enough to tell meaning from words."""

    def __init__(self):
        self.embedded = []

    def embed(self, texts):
        self.embedded += texts
        out = []
        for text in texts:
            v = [0.01, 0.01]
            for word, axis in AXES.items():
                if word in text.lower():
                    v[axis] += 1
            out.append(v)
        return out


def ctx_for(conn, embedder, member_id=1):
    turn = Turn(member_id=member_id, conversation_id="c", in_group=False,
                permissions=OWNER_PERMISSIONS)
    return builtin_tools.ToolContext(conn=conn, turn=turn, channel="t",
                                     services={"embedder": embedder})


def test_meaning_finds_what_no_word_shares(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    oil = store.add_note(conn, "trocar o óleo sexta", owner_id=1)
    store.add_note(conn, "comprar pão", owner_id=1)
    c = ctx_for(conn, FakeEmbedder())
    out = asyncio.run(run_tool(registered()["notes_search"], c.turn, c,
                               {"query": "aquela coisa do carro"}))
    assert f"#{oil.id}" in out.text and "pão" not in out.text


def test_only_the_askers_notes_and_each_embedded_once(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    store.add_note(conn, "levar o carro na oficina", owner_id=2)
    emb = FakeEmbedder()
    c = ctx_for(conn, emb)
    out = asyncio.run(run_tool(registered()["notes_search"], c.turn, c, {"query": "carro"}))
    assert "oficina" not in out.text, "Ana's private note never reaches the Owner's search"
    store.add_note(conn, "revisão do carro", owner_id=1)
    asyncio.run(run_tool(registered()["notes_search"], c.turn, c, {"query": "motor do carro"}))
    asyncio.run(run_tool(registered()["notes_search"], c.turn, c, {"query": "motor do carro"}))
    assert emb.embedded.count("revisão do carro") == 1, "cached until the text changes"
