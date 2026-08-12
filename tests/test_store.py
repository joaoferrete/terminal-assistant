from datetime import date, datetime

import pytest

from ta import db, store
from ta.i18n import t

NOW = datetime(2026, 8, 10, 9, 0)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "t.db")


def test_capturing_a_loose_note(conn):
    n = store.add_note(conn, "ideia: usar cache", now=NOW)
    assert n.text == "ideia: usar cache"
    assert not n.is_task and not n.is_reminder and not n.is_done
    assert n.tags == []


def test_capture_with_attributes_and_tags(conn):
    n = store.add_note(conn, "ligar dentista @sexta #saude #urgente !alta", now=NOW)
    assert n.text == "ligar dentista"
    assert n.due == "2026-08-14"
    assert n.priority == "high"
    assert n.tags == ["saude", "urgente"]
    assert n.is_task


def test_sort_key_grows_in_capture_order(conn):
    a = store.add_note(conn, "primeira", now=NOW)
    b = store.add_note(conn, "segunda", now=NOW)
    assert b.sort_key > a.sort_key


def test_dragging_marks_pinned_by_user(conn):
    """What makes the user's hand beat the LLM permanently (ADR 0003)."""
    n = store.add_note(conn, "x", now=NOW)
    assert not n.pinned_by_user
    store.move_note(conn, n.id, sort_key=0.5, pos_x=120, pos_y=40)
    moved = store.get_note(conn, n.id)
    assert moved.pinned_by_user
    assert (moved.sort_key, moved.pos_x, moved.pos_y) == (0.5, 120, 40)


def test_list_hides_finished_notes_by_default(conn):
    a = store.add_note(conn, "aberta", now=NOW)
    b = store.add_note(conn, "fechada", now=NOW)
    store.mark_done(conn, b.id, now=NOW)
    assert [n.id for n in store.list_notes(conn)] == [a.id]
    assert len(store.list_notes(conn, include_done=True)) == 2


def test_due_today_includes_overdue(conn):
    store.add_note(conn, "atrasada @2026-08-01", now=NOW)
    store.add_note(conn, "hoje @hoje", now=NOW)
    store.add_note(conn, "futura @2026-12-25", now=NOW)
    store.add_note(conn, "sem prazo", now=NOW)
    due = store.due_today(conn, today=date(2026, 8, 10))
    assert [n.text for n in due] == ["atrasada", "hoje"]


def test_pending_reminders_respects_fired_at(conn):
    n = store.add_note(conn, "tomar remedio !!08:00", now=NOW)  # 08:00 < 09:00 -> amanha
    assert n.remind_at == "2026-08-11T08:00:00"
    # Nada pendente agora.
    assert store.pending_reminders(conn, now=NOW) == []
    # Depois da hora, pendente.
    later = datetime(2026, 8, 11, 8, 1)
    assert [x.id for x in store.pending_reminders(conn, now=later)] == [n.id]
    # Disparado, sai da fila — o scheduler não repete.
    store.mark_fired(conn, n.id, now=later)
    assert store.pending_reminders(conn, now=later) == []


def test_export_markdown(conn):
    store.add_note(conn, "ligar dentista @sexta #saude", now=NOW)
    b = store.add_note(conn, "feita", now=NOW)
    store.mark_done(conn, b.id, now=NOW)
    out = store.export_markdown(conn)
    assert "- [ ] ligar dentista" in out
    assert f"{t('export.due')} 2026-08-14" in out
    assert "#saude" in out
    assert "- [x] feita" in out


def test_tags_without_an_n_plus_one(conn):
    """list_notes has to fetch tags in a single query."""
    for i in range(5):
        store.add_note(conn, f"n{i} #a #b", now=NOW)
    notes = store.list_notes(conn)
    assert all(n.tags == ["a", "b"] for n in notes)


def test_status_is_born_todo(conn):
    assert store.add_note(conn, "x", now=NOW).status == "todo"


def test_set_status_validates_the_enum(conn):
    n = store.add_note(conn, "x", now=NOW)
    with pytest.raises(ValueError, match="invalid status"):
        store.set_status(conn, n.id, "quase_feito")


def test_done_at_is_a_timestamp_not_a_state(conn):
    """Entering done stamps the instant; leaving clears it; cancelling never stamps."""
    n = store.add_note(conn, "x", now=NOW)
    store.set_status(conn, n.id, "done", now=NOW)
    assert store.get_note(conn, n.id).done_at == "2026-08-10T09:00:00"

    store.set_status(conn, n.id, "doing")
    assert store.get_note(conn, n.id).done_at is None

    store.set_status(conn, n.id, "cancelled")
    got = store.get_note(conn, n.id)
    assert got.done_at is None          # cancelled was not done
    assert got.is_terminal              # but it left the queue
    assert not got.is_done


def test_terminal_notes_leave_the_list_by_default(conn):
    a = store.add_note(conn, "aberta", now=NOW)
    b = store.add_note(conn, "feita", now=NOW)
    c = store.add_note(conn, "cancelada", now=NOW)
    d = store.add_note(conn, "em andamento", now=NOW)
    store.set_status(conn, b.id, "done", now=NOW)
    store.set_status(conn, c.id, "cancelled")
    store.set_status(conn, d.id, "doing")
    # doing and hold stay in the queue; done and cancelled leave.
    assert sorted(n.id for n in store.list_notes(conn)) == sorted([a.id, d.id])
    assert len(store.list_notes(conn, include_done=True)) == 4


def test_due_today_ignores_cancelled_notes(conn):
    n = store.add_note(conn, "cancelada @hoje", now=NOW)
    store.set_status(conn, n.id, "cancelled")
    assert store.due_today(conn, today=date(2026, 8, 10)) == []


# ── Display order: horizon, and priority within it (ADR 0010) ───────────────
# Every test below passes an explicit `today=`. One that leaned on `date.today()`
# would pass in August 2026 and fail in 2027.
TODAY = NOW.date()   # 2026-08-10


def _order(conn, *, include_done=False):
    return [
        n.text
        for n in store.by_urgency(
            store.list_notes(conn, include_done=include_done), today=TODAY
        )
    ]


@pytest.mark.parametrize(
    ("due", "band"),
    [
        ("2026-08-09", "overdue"),   # yesterday
        ("2026-08-10", "today"),
        ("2026-08-13", "week"),
        ("2026-08-17", "week"),    # day 7 exactly: inside, the bound is inclusive
        ("2026-08-18", "later"),   # day 8: outside
        (None, "later"),           # undated lives with the distant future
    ],
)
def test_the_horizon_bands(due, band):
    assert store.horizon(due, today=TODAY) == band


def test_a_deadline_today_beats_high_priority_next_week(conn):
    """The request that originated ADR 0010, as an executable sentence.

    Before, priority was the first criterion and the distant `!alta` sat on top.
    """
    store.add_note(conn, "RFC semana que vem !alta @2026-08-14", now=NOW)
    store.add_note(conn, "condominio hoje !media @2026-08-10", now=NOW)
    assert _order(conn) == ["condominio hoje", "RFC semana que vem"]


def test_overdue_comes_before_today(conn):
    store.add_note(conn, "hoje !alta @2026-08-10", now=NOW)
    store.add_note(conn, "atrasada !baixa @2026-08-01", now=NOW)
    assert _order(conn) == ["atrasada", "hoje"]


def test_undated_high_does_not_sit_behind_a_low_due_in_september(conn):
    """Why "undated" lives in `later` rather than in a band of its own at the end."""
    store.add_note(conn, "setembro !baixa @2026-09-01", now=NOW)
    store.add_note(conn, "sem data !alta", now=NOW)
    assert _order(conn) == ["sem data", "setembro"]


def test_priority_orders_within_the_band(conn):
    store.add_note(conn, "sem prio", now=NOW)
    store.add_note(conn, "baixa !baixa", now=NOW)
    store.add_note(conn, "alta !alta", now=NOW)
    store.add_note(conn, "media !media", now=NOW)
    finished = store.add_note(conn, "feita !alta", now=NOW)
    store.set_status(conn, finished.id, "done", now=NOW)

    # None of them has a deadline, so they all land in `later` and priority
    # decides. A terminal note goes to the end even at high priority.
    assert _order(conn, include_done=True) == ["alta", "media", "baixa", "sem prio", "feita"]


def test_dated_before_undated_within_the_same_band(conn):
    """Guards the `n.due is None` term in the key.

    Without it, `n.due or ""` maps the undated note to `""`, which sorts before any
    ISO date, and inside `later` a loose idea would jump ahead of a dated task.
    """
    store.add_note(conn, "sem prazo !alta", now=NOW)
    store.add_note(conn, "setembro !alta @2026-09-01", now=NOW)
    store.add_note(conn, "outubro !alta @2026-10-01", now=NOW)
    assert _order(conn) == ["setembro", "outubro", "sem prazo"]


def test_a_terminal_note_stays_at_the_end_even_when_overdue(conn):
    """Guards `is_terminal` as the FIRST term, ahead of horizon.

    Without it, a note finished last week — deadline in the past, therefore
    `overdue` — would climb to the top of the board.
    """
    store.add_note(conn, "aberta sem prazo", now=NOW)
    finished = store.add_note(conn, "feita e vencida !alta @2026-08-01", now=NOW)
    store.set_status(conn, finished.id, "done", now=NOW)
    assert _order(conn, include_done=True) == ["aberta sem prazo", "feita e vencida"]


def test_within_the_band_sort_key_still_decides(conn):
    """ADR 0003's promise under test: what was stored still holds.

    Two notes identical in band and priority — only `sort_key` separates them.
    """
    a = store.add_note(conn, "primeira", now=NOW)
    b = store.add_note(conn, "segunda", now=NOW)
    assert _order(conn) == ["primeira", "segunda"]
    store.move_note(conn, b.id, sort_key=a.sort_key - 1)
    assert _order(conn) == ["segunda", "primeira"]


def test_export_shows_non_binary_state(conn):
    a = store.add_note(conn, "andando", now=NOW)
    b = store.add_note(conn, "cancelada", now=NOW)
    store.set_status(conn, a.id, "doing")
    store.set_status(conn, b.id, "cancelled")
    out = store.export_markdown(conn)
    assert "[~] andando" in out
    assert "[/] cancelada" in out


def test_an_empty_colour_returns_to_the_priority_default(conn):
    """`color=""` clears; `color=None` leaves it alone. They are different requests."""
    nid = store.add_note(conn, "x !alta", now=NOW).id
    store.move_note(conn, nid, color="#bfdcf5")
    assert store.get_note(conn, nid).color == "#bfdcf5"

    store.move_note(conn, nid, pos_x=10)          # a None colour does not erase it
    assert store.get_note(conn, nid).color == "#bfdcf5"

    store.move_note(conn, nid, color="")          # now it does
    assert store.get_note(conn, nid).color is None


# ── The review queue ────────────────────────────────────────────────────────
def test_a_new_note_is_born_pending_review(conn):
    nid = store.add_note(conn, "x", now=NOW).id
    assert store.pending_review(conn) == [nid]


def test_marking_reviewed_removes_it_from_the_queue(conn):
    nid = store.add_note(conn, "x", now=NOW).id
    store.mark_reviewed(conn, nid, now=NOW)
    assert store.pending_review(conn) == []


def test_the_queue_ignores_whatever_blew_the_ceiling(conn):
    """With no ceiling, a note that always fails would be retried forever."""
    nid = store.add_note(conn, "x", now=NOW).id
    for _ in range(5):
        store.count_review_attempt(conn, nid)
    assert store.pending_review(conn, max_attempts=5) == []
    assert store.pending_review(conn, max_attempts=6) == [nid]


def test_the_queue_has_a_limit_and_a_stable_order(conn):
    """A week offline does not become a burst: it drains slowly, oldest first."""
    ids = [store.add_note(conn, f"n{i}", now=NOW).id for i in range(5)]
    assert store.pending_review(conn, limit=2) == ids[:2]


def test_review_all_takes_open_notes_and_ignores_terminal_ones(conn):
    open_ = store.add_note(conn, "aberta", now=NOW).id
    finished = store.add_note(conn, "feita", now=NOW).id
    cancelled_ = store.add_note(conn, "cancelada", now=NOW).id
    for i in (open_, finished, cancelled_):
        store.mark_reviewed(conn, i, now=NOW)
    store.set_status(conn, finished, "done", now=NOW)
    store.set_status(conn, cancelled_, "cancelled", now=NOW)

    assert store.queue_all_for_review(conn) == 1
    assert store.pending_review(conn) == [open_]


def test_review_all_resets_the_attempts(conn):
    """An explicit user request is a new order, not a continuation of giving up."""
    nid = store.add_note(conn, "x", now=NOW).id
    for _ in range(5):
        store.count_review_attempt(conn, nid)
    store.mark_reviewed(conn, nid, now=NOW)

    store.queue_all_for_review(conn)
    assert store.pending_review(conn, max_attempts=5) == [nid]


def test_set_tags_replaces_and_deduplicates(conn):
    nid = store.add_note(conn, "x", now=NOW).id
    store.set_tags(conn, nid, ["trabalho", "estudo", "trabalho"])
    assert store.get_note(conn, nid).tags == ["estudo", "trabalho"]
    store.set_tags(conn, nid, ["pessoal"])
    assert store.get_note(conn, nid).tags == ["pessoal"]


# ── Soft delete ─────────────────────────────────────────────────────────────
def test_a_deleted_note_leaves_every_listing(conn):
    """Deleting has to vanish from EVERY read path, not just the board."""
    # `!!10:00` with NOW at 09:00 is due today; `!!09:00` would fall to tomorrow.
    alive = store.add_note(conn, "viva @2026-08-10 !!10:00", now=NOW).id
    dead = store.add_note(conn, "morta @2026-08-10 !!10:00", now=NOW).id
    store.soft_delete(conn, dead, now=NOW)

    later = datetime(2026, 8, 10, 11, 0)
    assert [n.id for n in store.list_notes(conn)] == [alive]
    assert [n.id for n in store.due_today(conn, today=date(2026, 8, 10))] == [alive]
    assert [n.id for n in store.pending_reminders(conn, now=later)] == [alive]
    assert store.pending_review(conn) == [alive]
    assert store.queue_all_for_review(conn) == 1


def test_the_trash_returns_only_the_deleted(conn):
    alive = store.add_note(conn, "viva", now=NOW).id
    dead = store.add_note(conn, "morta", now=NOW).id
    store.soft_delete(conn, dead, now=NOW)

    assert [n.id for n in store.list_notes(conn, deleted=True)] == [dead]
    assert [n.id for n in store.list_notes(conn)] == [alive]


def test_restore_brings_it_back(conn):
    nid = store.add_note(conn, "x", now=NOW).id
    store.soft_delete(conn, nid, now=NOW)
    assert store.get_note(conn, nid).is_deleted

    store.restore(conn, nid)
    assert not store.get_note(conn, nid).is_deleted
    assert [n.id for n in store.list_notes(conn)] == [nid]


def test_deleting_and_cancelling_are_independent_axes(conn):
    """`cancelled` means "I decided not to" and stays on the kanban; deleted leaves everything.

    A sixth status would lose that distinction — you can delete a finished note.
    """
    nid = store.add_note(conn, "x", now=NOW).id
    store.set_status(conn, nid, "done", now=NOW)
    store.soft_delete(conn, nid, now=NOW)

    n = store.get_note(conn, nid)
    assert n.status == "done" and n.is_deleted
    assert store.list_notes(conn, include_done=True) == []
    assert [x.id for x in store.list_notes(conn, deleted=True)] == [nid]


# ── Purge ───────────────────────────────────────────────────────────────────
def test_purge_only_reaches_what_is_in_the_trash(conn):
    """The whole safety of purge: there is no path that skips the soft delete."""
    alive = store.add_note(conn, "viva", now=NOW).id

    assert store.purge(conn, alive) is False
    assert store.get_note(conn, alive).text == "viva"     # still there

    store.soft_delete(conn, alive, now=NOW)
    assert store.purge(conn, alive) is True
    with pytest.raises(KeyError):
        store.get_note(conn, alive)


def test_purge_takes_the_tags_with_it(conn):
    """`ON DELETE CASCADE` only works with `foreign_keys = ON`, which `connect` sets."""
    nid = store.add_note(conn, "x #casa #saude", now=NOW).id

    def tag_count():
        return conn.execute(
            "SELECT COUNT(*) c FROM tags WHERE note_id = ?", (nid,)
        ).fetchone()["c"]

    assert tag_count() == 2

    store.soft_delete(conn, nid, now=NOW)
    store.purge(conn, nid)

    assert tag_count() == 0


def test_emptying_the_trash_does_not_touch_the_living(conn):
    alive_ids = [store.add_note(conn, f"viva{i}", now=NOW).id for i in range(3)]
    dead_ids = [store.add_note(conn, f"morta{i}", now=NOW).id for i in range(2)]
    for i in dead_ids:
        store.soft_delete(conn, i, now=NOW)

    assert store.purge_all(conn) == 2
    assert sorted(n.id for n in store.list_notes(conn)) == sorted(alive_ids)
    assert store.list_notes(conn, deleted=True) == []


def test_emptying_an_empty_trash_is_zero(conn):
    store.add_note(conn, "viva", now=NOW)
    assert store.purge_all(conn) == 0
