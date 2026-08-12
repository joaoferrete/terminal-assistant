from datetime import date, timedelta

import pytest
from starlette.testclient import TestClient

from ta.config import Config
from ta.daemon import create_app
from ta.i18n import t
from ta.llm import NotePlacement, OrganizeResult


class FakeCalendar:
    """A fake calendar. A test never talks to the user's Evolution Data Server."""

    available = True
    error = None

    def today(self, day=None):
        return []

    def now(self, moment=None):
        return None

    def write_targets(self):
        return []

    def warm(self):
        return 0


class FakeLighter:
    """Without it, the test wrote to the real `org.gnome.shell.extensions.lighter`:
    `take_over` on boot and `hand_back` on shutdown, once per test."""

    available = True

    async def get(self, key):
        return "false"

    async def set(self, key, value):
        return True

    async def enable(self, on=True):
        return True

    async def toggle(self):
        return True

    async def apply_profile(self, name, *, enable=True):
        return True

    async def take_over(self):
        pass

    async def hand_back(self):
        pass


@pytest.fixture
def client(tmp_path):
    app = create_app(
        Config(ha_token="fake", gemini_api_key="fake", auto_review=False),
        db_path=tmp_path / "t.db",
        rules_dir=tmp_path / "no-rules",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        yield c


def test_health_reports_presence_not_value(client):
    """`/health` exists to answer "did the service read the .env?" without leaking it."""
    body = client.get("/health").json()
    assert body["ok"] is True
    assert body["ha"]["token_configured"] is True
    assert body["gemini"]["key_configured"] is True
    # The value never shows up.
    assert "fake" not in client.get("/health").text


def test_health_with_no_secrets(tmp_path):
    app = create_app(
        Config(auto_review=False),
        db_path=tmp_path / "t.db",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        body = c.get("/health").json()
    assert body["ha"]["token_configured"] is False
    assert body["gemini"]["key_configured"] is False


def test_capture_over_http_extracts_the_roles(client):
    r = client.post("/notes", json={"text": "ligar dentista @2026-12-25 #saude !alta"})
    assert r.status_code == 201
    n = r.json()
    assert n["text"] == "ligar dentista"
    assert n["roles"] == {"task": True, "reminder": False}
    assert n["due"] == "2026-12-25"
    assert n["tags"] == ["saude"]


def test_empty_text_is_400(client):
    assert client.post("/notes", json={"text": "   "}).status_code == 400


def test_moving_and_colouring_persists_and_marks_pinned(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    r = client.post(f"/notes/{note_id}/move", json={"pos_x": 300, "pos_y": 150, "color": "#bfdcf5"})
    n = r.json()
    assert n["pos"] == [300, 150]
    assert n["color"] == "#bfdcf5"
    assert n["pinned_by_user"] is True


def test_done_toggles_both_ways(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    assert client.post(f"/notes/{note_id}/done").json()["done"] is True
    assert client.post(f"/notes/{note_id}/done", json={"done": False}).json()["done"] is False


def test_list_hides_finished_notes_by_default(client):
    a = client.post("/notes", json={"text": "aberta"}).json()["id"]
    b = client.post("/notes", json={"text": "fechada"}).json()["id"]
    client.post(f"/notes/{b}/done")
    assert [n["id"] for n in client.get("/notes").json()["notes"]] == [a]
    assert len(client.get("/notes?done=1").json()["notes"]) == 2


# ── The display order arrives ready at the client (ADR 0010) ────────────────
# The deadlines here are RELATIVE to `date.today()`: the route reads the real
# clock, and a fixed ISO date would age out of its band on its own.
def _in_days(days):
    return (date.today() + timedelta(days=days)).isoformat()


def test_notes_arrives_sorted_by_urgency(client):
    """The test that proves the board receives the order rather than computing it.

    The distant `!alta` sits below today's `!media`: it was exactly the other way
    round when priority was the first criterion.
    """
    for text in (
        f"distante !alta @{_in_days(30)}",
        f"hoje !media @{_in_days(0)}",
        f"atrasada !baixa @{_in_days(-3)}",
        f"esta semana !alta @{_in_days(4)}",
        "sem prazo !baixa",
    ):
        client.post("/notes", json={"text": text})

    notes = client.get("/notes").json()["notes"]
    assert [n["text"] for n in notes] == [
        "atrasada", "hoje", "esta semana", "distante", "sem prazo",
    ]
    assert [n["horizon"] for n in notes] == [
        "overdue", "today", "week", "later", "later",
    ]


def test_today_arrives_with_overdue_first(client):
    client.post("/notes", json={"text": f"hoje !alta @{_in_days(0)}"})
    client.post("/notes", json={"text": f"atrasada !baixa @{_in_days(-5)}"})
    tasks = client.get("/today").json()["tasks"]
    assert [t["text"] for t in tasks] == ["atrasada", "hoje"]
    assert [t["horizon"] for t in tasks] == ["overdue", "today"]


def test_organize_writes_the_order_and_respects_what_was_dragged(client):
    """`/organize` had no test at all. It writes, and the hand wins (ADR 0003)."""
    a = client.post("/notes", json={"text": "primeira"}).json()["id"]
    b = client.post("/notes", json={"text": "segunda"}).json()["id"]
    # `b` was dragged by hand: the model does not touch it.
    client.post(f"/notes/{b}/move", json={"pos_x": 10, "pos_y": 10})

    class StubLLM:
        model = "a-fake-model"

        async def organize(self, notes, priorities):
            self.notes = notes
            return OrganizeResult(
                placements=[
                    NotePlacement(id=a, group="casa", rank=1, reason="x"),
                    NotePlacement(id=b, group="casa", rank=2, reason="y"),
                ],
                groups_in_order=["casa"],
            )

    stub = StubLLM()
    client.app.state.llm = stub
    r = client.post("/organize").json()

    assert r == {
        "placed": 1,                # only `a`: `b` is pinned by the user's hand
        "skipped_pinned": 1,
        "groups": ["casa"],
        "model": "a-fake-model",
    }
    # And the model saw each note's band, which is what stops it reordering by
    # deadline (ADR 0010).
    assert all("horizon" in n for n in stub.notes)


def test_the_board_is_served_at_the_root_and_at_board(client):
    for path in ("/", "/board"):
        r = client.get(path)
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]
        assert t("board.title") in r.text


def test_export_markdown_over_http(client):
    client.post("/notes", json={"text": "ligar dentista @2026-12-25"})
    body = client.get("/export").text
    assert "- [ ] ligar dentista" in body
    assert f"{t('export.due')} 2026-12-25" in body


def test_status_over_http_and_done_at_exposed(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    n = client.post(f"/notes/{note_id}/status", json={"status": "done"}).json()
    assert n["status"] == "done"
    assert n["terminal"] is True
    assert n["done_at"] is not None      # the stamp has to reach the client


def test_cancelling_is_terminal_but_not_done(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    n = client.post(f"/notes/{note_id}/status", json={"status": "cancelled"}).json()
    assert (n["status"], n["terminal"], n["done"]) == ("cancelled", True, False)
    assert n["done_at"] is None          # cancelled was not done


def test_an_invalid_status_is_400(client):
    note_id = client.post("/notes", json={"text": "x"}).json()["id"]
    r = client.post(f"/notes/{note_id}/status", json={"status": "quase_feito"})
    assert r.status_code == 400
    assert "quase_feito" in r.json()["error"]


def test_doing_and_hold_stay_in_the_queue(client):
    a = client.post("/notes", json={"text": "a"}).json()["id"]
    b = client.post("/notes", json={"text": "b"}).json()["id"]
    client.post(f"/notes/{a}/status", json={"status": "doing"})
    client.post(f"/notes/{b}/status", json={"status": "hold"})
    ids = [n["id"] for n in client.get("/notes").json()["notes"]]
    assert sorted(ids) == sorted([a, b])


class StubHome:
    """Records the calls instead of talking to Home Assistant."""

    def __init__(self, inventory=None):
        self.calls = []
        self.turned_off = []
        self.media_calls = []
        self.inventory = inventory or [
            {
                "entity_id": "light.lampada_do_quarto",
                "state": "on",
                "attributes": {"friendly_name": "Quarto"},
            }
        ]

    async def entities(self, *prefixes):
        return self.inventory

    async def turn_off(self, entity_id):
        self.turned_off.append(entity_id)

    async def media(self, entity_id, action):
        self.media_calls.append((entity_id, action))

    async def switch_on(self, entity_id, brightness_pct=None):
        self.calls.append((entity_id, brightness_pct))

    async def confirm(self, entity_id, expected, tries=12):
        return expected, True

    async def close(self):
        pass


def test_on_with_no_brightness_goes_to_full(client):
    """`ta on quarto` with no number means "turn the light on", not "turn it on dim"."""
    fake = StubHome()
    client.app.state.home = fake
    r = client.post("/home/light", json={"entity": "quarto"})
    assert r.status_code == 200
    assert fake.calls == [("light.lampada_do_quarto", 100)]


def test_on_with_an_explicit_brightness_respects_the_value(client):
    fake = StubHome()
    client.app.state.home = fake
    client.post("/home/light", json={"entity": "quarto", "brightness": 30})
    assert fake.calls == [("light.lampada_do_quarto", 30)]


def test_off_with_no_target_does_not_touch_the_child_lock(client):
    """`ta off`'s sweep is a group too, and the same rule holds."""
    fake = StubHome([
        {"entity_id": "light.lampada_do_quarto", "state": "on", "attributes": {}},
        {
            "entity_id": "switch.ventilador_socket_1",
            "state": "on",
            "attributes": {"device_class": "outlet"},
        },
        {"entity_id": "switch.ventilador_bloqueio_para_criancas", "state": "on", "attributes": {}},
    ])
    client.app.state.home = fake
    r = client.post("/home/off", json={})
    assert r.status_code == 200
    assert fake.turned_off == ["light.lampada_do_quarto", "switch.ventilador_socket_1"]


def test_media_with_no_echo_configured_explains_what_is_missing(client):
    """Without TA_ECHOS, HA returned a 400 with no useful body and the error arrived illegible."""
    r = client.post("/media", json={"action": "pause"})
    assert r.status_code == 400
    assert "TA_ECHOS" in r.json()["error"]


def test_media_refuses_a_target_that_is_not_a_media_player(client):
    r = client.post("/media", json={"entity": "light.lampada_do_quarto", "action": "play"})
    assert r.status_code == 400
    assert "media_player" in r.json()["error"]


def test_media_uses_the_configured_echo_when_the_target_is_omitted(tmp_path):
    app = create_app(
        Config(ha_token="fake", echo_entities=("media_player.echo_quarto",)),
        db_path=tmp_path / "t.db",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        fake = StubHome()
        c.app.state.home = fake
        r = c.post("/media", json={"action": "pause"})
        assert r.status_code == 200
        assert r.json()["entity_id"] == "media_player.echo_quarto"
        assert fake.media_calls == [("media_player.echo_quarto", "pause")]


# ── The LLM's second pass over a capture ────────────────────────────────────
# Tested as a unit, not through the route: the review runs loose in the
# background, and waiting on a background task inside TestClient is a recipe for
# a flaky test.
from types import SimpleNamespace  # noqa: E402

from ta import db, store  # noqa: E402
from ta.daemon import _review_capture  # noqa: E402


class StubLLMReview:
    """Returns a fixed review. A test never talks to Gemini."""

    configured = True

    def __init__(self, review):
        self.review = review
        self.calls = []

    async def review_capture(self, text, *, due, remind_at, priorities="", accounts=""):
        self.calls.append((text, due, remind_at, priorities, accounts))
        return self.review


class StubNotify:
    def __init__(self):
        self.warnings = []

    async def send(self, title, body="", *, urgency="normal", icon=None):
        self.warnings.append((title, body))
        return True


class Review(SimpleNamespace):
    """The minimum of the schema `_review_capture` consumes."""

    def __init__(self, **kw):
        super().__init__(
            intent=kw.get("intent", "anotacao"),
            due=kw.get("due", ""),
            remind_at=kw.get("remind_at", ""),
            priority=kw.get("priority", ""),
            tags=kw.get("tags", []),
            is_event=kw.get("is_event", False),
            title=kw.get("title", ""),
            start=kw.get("start", ""),
            end=kw.get("end", ""),
            account=kw.get("account", "pessoal"),
            confidence=kw.get("confidence", 0.9),
            reason=kw.get("reason", ""),
        )


PERSONAL = SimpleNamespace(uid="src-pessoal", name="Terminal Assistant", personal=True,
                            account="eu@gmail.com")
WORK = SimpleNamespace(uid="src-trabalho", name="Terminal Assistant", personal=False,
                             account="eu@empresa.co")


def _stub_app(tmp_path, review, *, targets=()):
    conn = db.connect(tmp_path / "t.db")
    cal = SimpleNamespace(write_targets=lambda: list(targets), create_event=lambda *a: "uid-1")
    import asyncio as _asyncio
    return SimpleNamespace(
        state=SimpleNamespace(
            conn=conn,
            llm=StubLLMReview(review),
            notify=StubNotify(),
            calendar=cal,
            in_review=set(),
            review_sem=_asyncio.Semaphore(4),
        )
    )


async def test_the_review_removes_a_deadline_the_regex_invented(tmp_path):
    """The regex marks a deadline by shape; the review undoes it when that was not the intent."""
    app = _stub_app(tmp_path, Review(intent="anotacao", due="", confidence=0.95,
                                       reason="é um registro, não um prazo"))
    note = store.add_note(app.state.conn, "hoje eu preciso disso")
    assert note.due is not None                       # the regex marked it

    await _review_capture(app, note.id)

    assert store.get_note(app.state.conn, note.id).due is None
    assert t("review.due_removed") in app.state.notify.warnings[0][1]


async def test_a_low_confidence_review_touches_nothing(tmp_path):
    """A wrong correction is worse than none: below the floor, it does not act."""
    app = _stub_app(tmp_path, Review(due="", confidence=0.3))
    note = store.add_note(app.state.conn, "revisar o PR hoje")
    before = note.due

    await _review_capture(app, note.id)

    assert store.get_note(app.state.conn, note.id).due == before
    assert app.state.notify.warnings == []


async def test_the_review_creates_an_event_in_the_dedicated_calendar(tmp_path):
    app = _stub_app(
        tmp_path,
        Review(intent="compromisso", is_event=True, title="nutricionista",
                start="2026-09-17T08:30", end="2026-09-17T09:30", confidence=0.95),
        targets=(PERSONAL,),
    )
    note = store.add_note(app.state.conn, "ir na nutricionista 17 de setembro as 8:30")

    await _review_capture(app, note.id)

    assert f"{t('review.event_created')}: nutricionista" in app.state.notify.warnings[0][1]
    link = app.state.conn.execute(
        "SELECT uid, source_uid FROM calendar_links WHERE note_id = ?", (note.id,)
    ).fetchone()
    assert (link["uid"], link["source_uid"]) == ("uid-1", "src-pessoal")


async def test_with_no_dedicated_calendar_it_creates_the_event_nowhere_else(tmp_path):
    """With no "Terminal Assistant" calendar, it does not write to the main one (ADR 0007)."""
    app = _stub_app(
        tmp_path,
        Review(is_event=True, title="x", start="2026-09-17T08:30", confidence=0.99),
        targets=(),
    )
    note = store.add_note(app.state.conn, "compromisso qualquer")

    await _review_capture(app, note.id)

    assert app.state.conn.execute("SELECT COUNT(*) c FROM calendar_links").fetchone()["c"] == 0


async def test_an_invalid_date_from_the_model_does_not_blow_up(tmp_path):
    app = _stub_app(
        tmp_path,
        Review(is_event=True, title="x", start="semana que vem", confidence=0.99),
        targets=(PERSONAL,),
    )
    note = store.add_note(app.state.conn, "algo")

    await _review_capture(app, note.id)   # must not raise

    assert app.state.conn.execute("SELECT COUNT(*) c FROM calendar_links").fetchone()["c"] == 0


def test_review_switched_off_schedules_nothing(tmp_path):
    """`TA_AUTO_REVIEW=0` has to leave the regex path untouched."""
    app = create_app(
        Config(gemini_api_key="fake", auto_review=False),
        db_path=tmp_path / "t.db",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        c.post("/notes", json={"text": "x"})
        assert c.app.state.reviews == set()



async def test_a_work_event_goes_to_the_work_account(tmp_path):
    """It routed by `parent`, an opaque hash: everything landed in one calendar, silently."""
    app = _stub_app(
        tmp_path,
        Review(is_event=True, title="1:1 com a lead", start="2026-08-11T14:00",
                account="trabalho", confidence=0.95),
        targets=(PERSONAL, WORK),
    )
    note = store.add_note(app.state.conn, "1:1 com a lead na terça às 14h")

    await _review_capture(app, note.id)

    link = app.state.conn.execute("SELECT source_uid FROM calendar_links").fetchone()
    assert link["source_uid"] == "src-trabalho"


async def test_a_personal_event_goes_to_the_personal_account(tmp_path):
    app = _stub_app(
        tmp_path,
        Review(is_event=True, title="nutricionista", start="2026-09-17T08:30",
                account="pessoal", confidence=0.95),
        targets=(WORK, PERSONAL),   # order reversed: it must not be "the first one"
        )
    note = store.add_note(app.state.conn, "nutricionista 17 de setembro às 8:30")

    await _review_capture(app, note.id)

    link = app.state.conn.execute("SELECT source_uid FROM calendar_links").fetchone()
    assert link["source_uid"] == "src-pessoal"


def test_review_all_queues_and_answers_how_many(client):
    """The board's button hits this. With no LLM configured it returns a legible 400."""
    client.post("/notes", json={"text": "a"})
    client.post("/notes", json={"text": "b"})
    r = client.post("/review-all")
    assert r.status_code == 400            # auto_review=False in the fixture
    assert "TA_AUTO_REVIEW" in r.json()["error"]


def test_review_status_counts_the_queue(client):
    client.post("/notes", json={"text": "a"})
    body = client.get("/review-status").json()
    assert body == {"pending": 1, "running": 0}


def test_review_all_without_a_gemini_key(tmp_path):
    app = create_app(
        Config(),
        db_path=tmp_path / "t.db",
        calendar=FakeCalendar(),
        lighter=FakeLighter(),
        background=False,
    )
    with TestClient(app) as c:
        r = c.post("/review-all")
        assert r.status_code == 400
        assert "GEMINI_API_KEY" in r.json()["error"]


async def test_a_review_in_flight_is_not_queued_twice(tmp_path):
    """Four captures in a row reviewed the SAME note four times, with different
    answers. `in_review` is the guard."""
    from ta.daemon import _schedule_review

    conn = db.connect(tmp_path / "t.db")
    note = store.add_note(conn, "x")
    app = SimpleNamespace(
        state=SimpleNamespace(
            conn=conn,
            llm=SimpleNamespace(configured=True),
            auto_review=True,
            reviews=set(),
            in_review={note.id},        # already in flight
        )
    )
    _schedule_review(app, note)
    assert app.state.reviews == set()   # nothing new was created


async def test_a_plain_note_gets_no_priority(tmp_path):
    """A record or a loose idea is not chaseable: a priority on them only clutters the board."""
    app = _stub_app(tmp_path, Review(intent="anotacao", priority="alta", confidence=0.9))
    note = store.add_note(app.state.conn, "hoje descobri o bug do domínio")

    await _review_capture(app, note.id)

    n = store.get_note(app.state.conn, note.id)
    assert n.priority is None
    assert "anotacao" in n.tags


async def test_a_plain_note_removes_a_machine_set_priority(tmp_path):
    """Reclassifying as a plain note has to undo the previous pass's priority."""
    app = _stub_app(tmp_path, Review(intent="anotacao", confidence=0.9))
    note = store.add_note(app.state.conn, "x")
    store.set_priority(app.state.conn, note.id, "media")

    await _review_capture(app, note.id)

    assert store.get_note(app.state.conn, note.id).priority is None


async def test_a_priority_typed_by_the_user_is_untouchable(tmp_path):
    """`!alta` is yours. Not even a reclassification erases it."""
    app = _stub_app(tmp_path, Review(intent="anotacao", priority="baixa", confidence=0.99))
    note = store.add_note(app.state.conn, "revisar isso !alta")
    assert note.priority_by_user

    await _review_capture(app, note.id)

    assert store.get_note(app.state.conn, note.id).priority == "high"


async def test_a_topic_typed_by_the_user_survives_and_gains_the_axes(tmp_path):
    """A topic is yours, an axis is structure.

    The first version treated your tags as all-or-nothing: writing `#app` made the
    note lose its area and type, and it vanished from both board filters. It
    happened to a real note.
    """
    app = _stub_app(
        tmp_path,
        Review(intent="anotacao", account="pessoal", tags=["estudo"], confidence=0.99),
    )
    note = store.add_note(app.state.conn, "fiz um novo app hoje #app")
    assert note.tags_by_user and note.tags == ["app"]

    await _review_capture(app, note.id)

    tags = store.get_note(app.state.conn, note.id).tags
    assert "app" in tags                         # your topic stays
    assert {"pessoal", "anotacao"} <= set(tags)  # and the axes come in
    assert "estudo" not in tags                  # the model's topic does NOT replace yours


async def test_area_and_type_always_come_in_as_tags(tmp_path):
    """A note with neither area nor type is invisible in the board filters."""
    app = _stub_app(
        tmp_path,
        Review(intent="tarefa", account="trabalho", tags=[], confidence=0.9),
    )
    note = store.add_note(app.state.conn, "revisar o PR")

    await _review_capture(app, note.id)

    assert store.get_note(app.state.conn, note.id).tags == ["tarefa", "trabalho"]


async def test_a_topic_invented_by_the_model_is_discarded(tmp_path):
    """A closed vocabulary: "profissional" and "job" would become synonyms of "trabalho"."""
    app = _stub_app(
        tmp_path,
        Review(intent="tarefa", tags=["profissional", "job", "estudo"], confidence=0.9),
    )
    note = store.add_note(app.state.conn, "x")

    await _review_capture(app, note.id)

    assert store.get_note(app.state.conn, note.id).tags == ["estudo", "pessoal", "tarefa"]


async def test_at_most_two_topics_beyond_the_axes(tmp_path):
    app = _stub_app(
        tmp_path,
        Review(intent="tarefa", tags=["saude", "casa", "compras", "estudo"], confidence=0.9),
    )
    note = store.add_note(app.state.conn, "x")

    await _review_capture(app, note.id)

    tags = store.get_note(app.state.conn, note.id).tags
    assert len(tags) == 4          # area + type + 2 topics
    assert {"pessoal", "tarefa"} <= set(tags)


async def test_the_review_creates_no_duplicate_event(tmp_path):
    """Re-tagging three times created THREE identical calendar events: the
    INSERT OR REPLACE swapped the link and orphaned the previous event."""
    app = _stub_app(
        tmp_path,
        Review(intent="compromisso", is_event=True, title="nutricionista",
                start="2026-09-17T08:30", confidence=0.95),
        targets=(PERSONAL,),
    )
    note = store.add_note(app.state.conn, "nutricionista 17 de setembro às 8:30")

    await _review_capture(app, note.id)
    await _review_capture(app, note.id)      # a second pass, as in "review all"
    await _review_capture(app, note.id)

    n = app.state.conn.execute("SELECT COUNT(*) c FROM calendar_links").fetchone()["c"]
    assert n == 1
    # And the "event created" notice goes out exactly once.
    created = [a for a in app.state.notify.warnings if t("review.event_created") in a[1]]
    assert len(created) == 1


def test_purge_refuses_a_note_that_is_not_in_the_trash(client):
    """409 rather than surprise-deleting something the user thought was safe."""
    nid = client.post("/notes", json={"text": "viva"}).json()["id"]
    r = client.request("DELETE", f"/notes/{nid}/purge")
    assert r.status_code == 409
    assert t("api.note_not_in_trash", id=nid) == r.json()["error"]
    assert client.get("/notes").json()["notes"][0]["id"] == nid


def test_purging_a_note_in_the_trash_works(client):
    nid = client.post("/notes", json={"text": "x"}).json()["id"]
    client.request("DELETE", f"/notes/{nid}")
    r = client.request("DELETE", f"/notes/{nid}/purge")
    assert r.status_code == 200 and r.json() == {"purged": 1, "id": nid}
    assert client.get("/notes?deleted=1").json()["notes"] == []


def test_emptying_the_trash_requires_confirmation(client):
    nid = client.post("/notes", json={"text": "x"}).json()["id"]
    client.request("DELETE", f"/notes/{nid}")

    r = client.request("DELETE", "/trash", json={})
    assert r.status_code == 400
    assert r.json()["error"] == t("api.confirm_required")
    assert len(client.get("/notes?deleted=1").json()["notes"]) == 1   # nothing was deleted

    r = client.request("DELETE", "/trash", json={"confirmed": True})
    assert r.json() == {"purged": 1}
    assert client.get("/notes?deleted=1").json()["notes"] == []
