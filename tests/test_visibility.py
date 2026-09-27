"""Who sees which Note (D6, invariant 3), across every read path at once.

A single forgotten path is a leak, so the paths are tested together — the same
shape as the `deleted_at` test, which covers the five places a deleted Note must
vanish from in one go.
"""
from datetime import date, datetime

import pytest
from starlette.testclient import TestClient
from test_security import TOKEN, build

from ta import board_access, db, store
from ta.members import OWNER_ID, SYSTEM, Viewer

NOW = datetime(2026, 9, 25, 9, 0)
TODAY = date(2026, 9, 25)


@pytest.fixture
def house(tmp_path):
    """The Owner, a housemate, a household List, and one Note of each kind."""
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    conn.execute("INSERT INTO lists (id, name, scope, owner_id, created_at)"
                 " VALUES (1, 'compras', 'household', 1, 'x')")
    mine = store.add_note(conn, "revisar PR @2026-09-25", now=NOW, owner_id=OWNER_ID)
    hers = store.add_note(conn, "presente surpresa @2026-09-25", now=NOW, owner_id=2)
    shared = store.add_note(conn, "leite @2026-09-25", now=NOW, owner_id=2)
    conn.execute("UPDATE notes SET list_id = 1 WHERE id = ?", (shared.id,))
    return conn, mine.id, hers.id, shared.id


def ids(notes):
    return sorted(n.id for n in notes)


def test_every_read_path_shows_a_member_their_own_plus_the_household(house):
    conn, mine, hers, shared = house
    ana = Viewer(2)

    assert ids(store.list_notes(conn, viewer=ana)) == sorted([hers, shared])
    assert ids(store.due_today(conn, viewer=ana, today=TODAY)) == sorted([hers, shared])
    export = store.export_markdown(conn, viewer=ana)
    assert "presente surpresa" in export and "leite" in export
    assert "revisar PR" not in export
    with pytest.raises(KeyError):
        store.get_note(conn, mine, viewer=ana)
    # "Review everything" requeues only what she can see.
    assert store.queue_all_for_review(conn, viewer=ana) == 2


def test_in_a_group_only_the_household_is_visible(house):
    """Invariant 3: a group reply never carries anybody's private Notes —
    not even the asker's own."""
    conn, mine, hers, shared = house
    for member in (OWNER_ID, 2):
        in_group = Viewer(member, in_group=True)
        assert ids(store.list_notes(conn, viewer=in_group)) == [shared]
        assert ids(store.due_today(conn, viewer=in_group, today=TODAY)) == [shared]


def test_the_system_sees_everything(house):
    conn, *all_ids = house
    assert ids(store.list_notes(conn, viewer=SYSTEM)) == sorted(all_ids)


def test_forgetting_the_viewer_fails_loudly(house):
    """No default: a new read path that forgets it must break, not mean everyone."""
    conn, *_ = house
    with pytest.raises(TypeError):
        store.list_notes(conn)
    with pytest.raises(TypeError):
        store.list_notes(conn, viewer=None)


def test_emptying_the_trash_only_reaches_what_you_own(house):
    conn, mine, hers, shared = house
    for nid in (mine, hers, shared):
        store.soft_delete(conn, nid, now=NOW)
    assert store.purge_all(conn, viewer=Viewer(2)) == 2      # hers and the shared one she wrote
    assert [n.id for n in store.list_notes(conn, viewer=SYSTEM, deleted=True)] == [mine]


# ── Through the daemon, with a housemate's session cookie ───────────────────
@pytest.fixture
def as_ana(tmp_path):
    app = build(tmp_path, host="0.0.0.0", token=TOKEN)
    with TestClient(app) as c:
        # A second connection to the same file: the daemon's belongs to its loop.
        side = db.connect(tmp_path / "t.db")
        side.execute("INSERT INTO members (id, handle, is_owner, created_at)"
                     " VALUES (2, 'ana', 0, 'x')")
        owner_note = store.add_note(side, "segredo do dono").id
        c.cookies.set(board_access.COOKIE, board_access.session_cookie(TOKEN, 2))
        c.side = side
        yield c, owner_note


def test_a_housemate_does_not_see_the_owners_notes_on_the_board(as_ana):
    c, owner_note = as_ana
    texts = [n["text"] for n in c.get("/notes").json()["notes"]]
    assert "segredo do dono" not in texts
    assert "segredo do dono" not in c.get("/export").text


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/notes/{id}/move", {"pos_x": 1, "pos_y": 1}),
        ("post", "/notes/{id}/done", None),
        ("post", "/notes/{id}/status", {"status": "doing"}),
        ("delete", "/notes/{id}", None),
        ("post", "/notes/{id}/restore", None),
        ("delete", "/notes/{id}/purge", None),
    ],
)
def test_a_housemate_cannot_change_the_owners_note_by_id(as_ana, method, path, body):
    """Three of these used to write before checking anything at all."""
    c, owner_note = as_ana
    url = path.format(id=owner_note)
    r = getattr(c, method)(url, json=body) if body else getattr(c, method)(url)
    assert r.status_code == 404
    note = store.get_note(c.side, owner_note, viewer=SYSTEM)
    assert note.status == "todo" and note.pos_x is None and not note.is_deleted


def test_an_unknown_id_is_404_not_500(tmp_path):
    with TestClient(build(tmp_path)) as c:
        for r in (c.post("/notes/999/move", json={"pos_x": 1}), c.post("/notes/999/done"),
                  c.post("/notes/999/status", json={"status": "doing"})):
            assert r.status_code == 404


def test_a_note_captured_by_the_bot_belongs_to_its_sender(tmp_path):
    from ta.daemon import _capture

    with TestClient(build(tmp_path)) as c:
        db.connect(tmp_path / "t.db").execute(
            "INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')"
        )

        async def capture():
            return _capture(c.app, "comprar pão", owner_id=2)

        note = c.portal.call(capture)
    assert note.owner_id == 2
