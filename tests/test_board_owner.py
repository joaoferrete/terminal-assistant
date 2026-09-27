"""The board says whose it is and who added what (F10, D50), and shows the
automations (T10.9)."""
import json

from starlette.testclient import TestClient
from test_daemon import FakeCalendar, FakeLighter

from ta import board_access, chat_rules, db, routines
from ta.config import Config
from ta.daemon import create_app

TOKEN = "a-test-token-not-a-secret"


def client(tmp_path):
    app = create_app(Config(ha_token="fake", auto_review=False, host="0.0.0.0", token=TOKEN),
                     db_path=tmp_path / "t.db", rules_dir=tmp_path / "none",
                     calendar=FakeCalendar(), lighter=FakeLighter(), background=False)
    return TestClient(app)


def test_me_names_the_board_and_the_household(tmp_path):
    side = db.connect(tmp_path / "t.db")
    side.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    side.execute("UPDATE members SET persona = ? WHERE id = 2", (json.dumps({"name": "Ana"}),))
    with client(tmp_path) as c:
        c.cookies.set(board_access.COOKIE, board_access.session_cookie(TOKEN, 2))
        me = c.get("/me").json()
    assert me["member_id"] == 2 and me["name"] == "Ana" and me["names"]["2"] == "Ana"


def test_every_note_says_whose_it_is(tmp_path):
    with client(tmp_path) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        note = c.post("/notes", json={"text": "comprar pão"}).json()
    assert note["owner_id"] == 1


def test_automations_are_the_viewers_own_and_the_households(tmp_path):
    side = db.connect(tmp_path / "t.db")
    side.execute("INSERT INTO members (id, handle, is_owner, created_at) VALUES (2, 'ana', 0, 'x')")
    routines.create(side, owner_id=2, name="só da ana", steps=[], phrases=[])
    routines.create(side, owner_id=2, name="cinema", steps=[], phrases=["modo cinema"],
                    household=True)
    chat_rules.create(side, owner_id=1, name="corredor", trigger_kind="state",
                      trigger_entity="binary_sensor.porta", trigger_to="on",
                      action_tool="home_on", action_args={"target": "corredor"})
    with client(tmp_path) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        auto = c.get("/automations").json()
    assert [r["name"] for r in auto["routines"]] == ["cinema"], "Ana's private one stays hers"
    assert auto["rules"][0]["describe"].startswith("quando binary_sensor.porta")
