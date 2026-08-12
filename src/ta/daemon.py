"""The daemon: one asyncio process.

It serves the API the CLI consumes and the board, and runs the trigger engine —
scheduler, microphone watcher and state watcher — in the same loop. It exists
because time and machine-state triggers do not survive in a CLI that runs and
dies.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from . import capabilities, engine, i18n, priorities, store
from . import notes as notes_mod
from .actuators.home import Home, HomeError, StateWatcher
from .actuators.lighter import Lighter
from .actuators.notify import Notifier
from .config import (
    LOOPBACK,
    Config,
    ConfigError,
    _commandable,
    config_dir,
    resolve_entity,
    resolve_targets,
)
from .db import connect
from .llm import LLM, LLMUnavailable
from .scheduler import Scheduler, lateness_label, lateness_of
from .sensors.calendar import Calendar
from .sensors.mic import MicWatcher

log = logging.getLogger("ta")

# `ta on bedroom` with no number turns the light up to full. A lower default
# would make the shortest command the weakest one, which is not what anybody
# means when they type "turn on the light".
DEFAULT_BRIGHTNESS = 100

BOARD_HTML = Path(__file__).parent / "web" / "board.html"

# The marker the board route swaps for the message catalogue. Written as a JS
# comment so the file still opens directly in a browser during development,
# rather than becoming invalid syntax.
I18N_MARKER = "/*__I18N__*/{}"

# Example rules, versioned as documentation. They are NOT loaded: they target one
# specific house's inventory, and loading that on somebody else's boot would be
# the rule failing silently against a non-existent entity.
EXAMPLE_RULES = Path(__file__).resolve().parents[2] / "examples" / "rules"


def user_rules_dir() -> Path:
    """Where the user's Rules live: `~/.config/ta/rules/`.

    The name is not `rules_dir` because `create_app` has a parameter by that name,
    and the shadowing would silently turn the call into something else.

    It falls back to `<repo>/rules` when the new destination does not exist yet,
    and that is on purpose: whoever already had rules there cannot lose them over
    this change. `ta doctor` copies — never moves — and from then on XDG wins
    (ADR 0014).
    """
    new = config_dir() / "rules"
    if new.is_dir():
        return new
    legacy = Path(__file__).resolve().parents[2] / "rules"
    if legacy.is_dir():
        log.info("rules read from %s (legacy); `ta doctor` migrates them to %s", legacy, new)
        return legacy
    return new


# An IPv6 socket accepting IPv4 reports the peer as `::ffff:127.0.0.1`. Without
# this, the local CLI would start needing a token purely because of the socket
# family.
LOOPBACK_PEERS = (*LOOPBACK, "::ffff:127.0.0.1")


def _peer_local(request: Request) -> bool:
    return bool(request.client) and request.client.host in LOOPBACK_PEERS


class TokenAuth(BaseHTTPMiddleware):
    """Require `TA_TOKEN` from anyone arriving from another machine.

    A loopback client passes with no credential, on purpose: whoever is already on
    this machine has the `.env`, and requiring a token from them would make
    `ta note` carry a secret without buying any safety. What this middleware
    covers is the **network** — the case where the daemon was opened with
    `TA_HOST` and a Wi-Fi neighbour can reach the routes.

    It is only installed when there is a token. On pure loopback the daemon has no
    middleware at all, and the local path is exactly as it was (ADR 0012).

    The token is also accepted in the query, not only in the header, because the
    board has to boot somehow: `http://<ip>:7777/board?token=…` loads the HTML,
    and from there the JS stores the value and sends it in the header.
    """

    def __init__(self, app, token: str) -> None:
        super().__init__(app)
        self._token = token

    def _presented(self, request: Request) -> str:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return request.query_params.get("token", "")

    async def dispatch(self, request: Request, call_next):
        if not _peer_local(request):
            # `compare_digest` instead of `==`: comparing a secret with an early
            # exit leaks the correct prefix through response timing.
            presented = self._presented(request)
            if not presented or not hmac.compare_digest(presented, self._token):
                log.warning(
                    "401 from %s on %s", request.client.host if request.client else "?",
                    request.url.path,
                )
                return JSONResponse(
                    {"error": i18n.t("auth.missing_credential")},
                    status_code=401,
                )
        return await call_next(request)


def _note_json(n: store.Note, *, today: date | None = None) -> dict:
    """Serialise a Note for the client.

    `horizon` goes along and is derived here, on the server: it depends on the
    clock, and a second definition of "the next 7 days" living in the board's JS
    would be a definition no test compares with this one (ADR 0010). Whoever
    serialises a list passes `today` computed ONCE, so a long payload does not
    cross midnight halfway through.
    """
    return {
        "id": n.id,
        "text": n.text,
        "created_at": n.created_at,
        "due": n.due,
        "horizon": store.horizon(n.due, today=today),
        "remind_at": n.remind_at,
        "done": n.is_done,
        # The instant it entered `done`. Distinct from `status`: state and
        # timestamp are different things.
        "done_at": n.done_at,
        "priority": n.priority,
        "group": n.group_name,
        "sort_key": n.sort_key,
        "pos": [n.pos_x, n.pos_y] if n.pos_x is not None else None,
        "color": n.color,
        "pinned_by_user": n.pinned_by_user,
        "status": n.status,
        "terminal": n.is_terminal,
        "tags": n.tags,
        "deleted_at": n.deleted_at,
        # Derived roles, made explicit so the client does not recompute the rule.
        "roles": {"task": n.is_task, "reminder": n.is_reminder},
    }


def _event_json(e) -> dict:
    return {
        "summary": e.summary,
        "start": e.start.isoformat(timespec="minutes"),
        "end": e.end.isoformat(timespec="minutes"),
        "all_day": e.all_day,
        "calendar": e.calendar,
    }


# ── Engine context ──────────────────────────────────────────────────────────
class CalendarAdapter:
    """The facade Rules receive as `ctx.calendar`.

    Thin on purpose: a Rule must not know that Evolution, DBus or `gi` exist.
    """

    def __init__(self, cal: Calendar) -> None:
        self._cal = cal

    async def agora(self) -> dict | None:
        ev = await asyncio.to_thread(self._cal.now)
        return _event_json(ev) if ev else None

    async def hoje(self) -> list[dict]:
        evs = await asyncio.to_thread(self._cal.today)
        return [_event_json(e) for e in evs]


def _make_context(app: Starlette, trigger: engine.Trigger, **extra) -> engine.Context:
    return engine.Context(
        trigger=trigger,
        now=datetime.now(),
        home=app.state.home,
        lighter=app.state.lighter,
        notify=app.state.notify,
        calendar=app.state.cal_adapter,
        **extra,
    )


# ── Routes ──────────────────────────────────────────────────────────────────
async def health(request: Request) -> JSONResponse:
    """Diagnóstico. Reporta *presença* de segredo, nunca o valor.

    Existe porque `systemctl show` não expõe o que veio de `EnvironmentFile`, e
    "o serviço está lendo o .env?" é a primeira pergunta quando algo do HA ou do
    Gemini falha.
    """
    app = request.app
    cfg: Config = app.state.config
    caps = capabilities.inspect(cfg)
    return JSONResponse(
        {
            # Era `True` literal. Um diagnóstico que responde "ok" mesmo com o
            # núcleo quebrado não é diagnóstico — é decoração.
            "ok": all(c.ok for c in caps if c.essential),
            "lang": {"code": i18n.lang(), "source": i18n.lang_source()},
            # `reason` e `fix` vão junto: o `Calendar` já calculava um motivo com
            # o conserto embutido, e esta rota jogava fora, expondo só o booleano.
            # Quem lê `available: false` fica sabendo o quê, não o que fazer.
            "capabilities": [
                {"key": c.key, "ok": c.ok, "reason": c.reason, "fix": c.fix}
                for c in caps
            ],
            "ha": {"url": cfg.ha_url, "token_configured": bool(cfg.ha_token)},
            "gemini": {
                "key_configured": bool(cfg.gemini_api_key),
                "model": app.state.llm.model,
            },
            "calendar": {"available": app.state.calendar.available},
            "mic": {"available": app.state.mic.available, "active": app.state.mic.active},
            "lighter": {"available": app.state.lighter.available},
            "notify": {"available": app.state.notify.available},
            "rules": {
                "loaded": [r.name for r in app.state.rules],
                "errors": [f for f, _ in app.state.rule_errors],
            },
        }
    )


async def notes_create(request: Request) -> JSONResponse:
    body = await request.json()
    raw = (body.get("text") or "").strip()
    if not raw:
        return JSONResponse({"error": "texto vazio"}, status_code=400)
    note = store.add_note(request.app.state.conn, raw)
    # A revisão sai em background e o 201 volta agora: captura não espera rede
    # (ADR 0003). O que ela mudar aparece no mural no próximo reload.
    _schedule_review(request.app, note)
    return JSONResponse(_note_json(note), status_code=201)


# Piso de confiança para agir sozinho. Abaixo disto a revisão não faz nada: uma
# correção errada é pior que nenhuma, e evento fantasma na agenda de trabalho é
# visível para colegas (emenda do ADR 0007).
MIN_CONFIDENCE = 0.7

# Attempt ceiling per Note. A dropped network is temporary and deserves a retry;
# an answer that never validates does not, and with no ceiling the queue would
# become a loop.
MAX_REVIEW_ATTEMPTS = 5

# Concurrent reviews. "Review everything" over 40 notes would open 40 calls at
# once and hit a rate limit; queued four at a time it takes a few seconds longer
# and does not fail.
CONCURRENT_REVIEWS = 4


def _schedule_review(
    app: Starlette, note: store.Note | None = None, *, limit: int = 10
) -> None:
    """Fire the review of the new Note and drain the backlog queue.

    The queue exists because review fires once, at capture: anything captured
    with no network would never be reviewed. Each new capture with network pays
    for some of the ones left behind too — it is the most natural trigger,
    because it means you are using the app and probably have network now.
    """
    if not app.state.auto_review or not app.state.llm.configured:
        return

    ids = [note.id] if note is not None else []
    # `pending_review` already excludes the ones over the attempt ceiling, and
    # the Note that just came into being is pending too — hence the filter.
    ids += [i for i in store.pending_review(app.state.conn, limit=limit) if i not in ids]

    # Without this filter, four captures in a row re-queued the same pending
    # notes: #5 was reviewed four times, and two passes over the same note gave
    # DIFFERENT deadlines. `reviewed_at` is only written at the end, so the queue
    # does not protect against concurrency — this set is what does.
    ids = [i for i in ids if i not in app.state.in_review]

    for note_id in ids:
        app.state.in_review.add(note_id)
        task = asyncio.create_task(_review_capture(app, note_id), name=f"review-{note_id}")
        # Keep the reference: an ownerless task can be collected before it ends.
        app.state.reviews.add(task)
        task.add_done_callback(app.state.reviews.discard)


async def _review_capture(app: Starlette, note_id: int) -> None:
    """Fix what the regex could not know, and create the event if appropriate.

    It runs detached: any failure here is logged and dies with it. The Note is
    already stored, and the worst case of this function is not happening.
    """
    try:
        async with app.state.review_sem:
            await _review_one(app, note_id)
    finally:
        # Leave the "in flight" set whatever happens, otherwise a failure would
        # lock the Note out of any future retry.
        app.state.in_review.discard(note_id)


async def _review_one(app: Starlette, note_id: int) -> None:
    try:
        note = store.get_note(app.state.conn, note_id)
    except KeyError:
        return  # deleted before review got to it

    # Count the attempt BEFORE trying: if the process dies halfway, the Note does
    # not keep retrying forever on the next boot.
    attempts = store.count_review_attempt(app.state.conn, note_id)

    try:
        # A broad Exception on purpose: this function is optional by design, and
        # nothing it does is worth taking down the daemon or losing the Note.
        targets = await asyncio.to_thread(app.state.calendar.write_targets)
        r = await app.state.llm.review_capture(
            note.text,
            due=note.due,
            remind_at=note.remind_at,
            priorities=priorities.current(app.state.conn) or "",
            # Only the domain, not the address: it is what decides the routing,
            # and sending the whole email outside would be extra data for the
            # same result.
            accounts=", ".join(
                f"{'pessoal' if a.personal else 'trabalho'}: "
                f"{a.account.rsplit('@', 1)[-1] if '@' in a.account else '?'}"
                for a in targets
            ),
        )
    except Exception as e:
        # Not marked reviewed: it stays queued for the next capture with network.
        if attempts >= MAX_REVIEW_ATTEMPTS:
            store.mark_reviewed(app.state.conn, note_id)
            log.warning(
                "review of #%s gave up after %d attempts: %s", note_id, attempts, e
            )
        else:
            log.info(
                "review of #%s failed (attempt %d/%d), staying queued: %s",
                note_id, attempts, MAX_REVIEW_ATTEMPTS, e,
            )
        return

    # From here on the model answered: it leaves the queue even if nothing changes.
    store.mark_reviewed(app.state.conn, note_id)

    if r.confidence < MIN_CONFIDENCE:
        log.info("review of #%s ignored: confidence %.2f", note_id, r.confidence)
        return

    changes: list[str] = []

    new_due = _iso_date(r.due)
    new_remind = _iso_datetime(r.remind_at)
    if (new_due, new_remind) != (
        date.fromisoformat(note.due) if note.due else None,
        datetime.fromisoformat(note.remind_at) if note.remind_at else None,
    ):
        store.set_schedule(app.state.conn, note_id, due=new_due, remind_at=new_remind)
        if note.due and new_due is None:
            changes.append(i18n.t("review.due_removed"))
        elif new_due:
            changes.append(f"{i18n.t('export.due')} {new_due.isoformat()}")

    # Priority and tags only apply if **you** did not write them. `by_user` is
    # what separates "I typed `!high`" from "review put high there on an earlier
    # pass": the first is untouchable, the second is revisable.
    kind = r.intent if r.intent in notes_mod.KINDS else "anotacao"

    if not note.priority_by_user:
        # A plain note has no priority, by definition: a record or a loose idea
        # is not chaseable, and asking for its urgency only clutters the board.
        # If a machine-set priority was there, it goes.
        #
        # `resolve_priority` rather than `in PRIORITIES`: the model may return
        # `alta` as readily as `high`, and both forms mean the same thing. A
        # value that is neither becomes a log line — before this it silently
        # became `None`, and the symptom was the note coming back from review
        # with no priority and nothing saying why.
        new_priority = notes_mod.resolve_priority(r.priority) if kind != "anotacao" else None
        if r.priority and new_priority is None and kind != "anotacao":
            log.warning("review returned a priority outside the enum: %r", r.priority)
        if new_priority != note.priority:
            store.set_priority(app.state.conn, note_id, new_priority)
            changes.append(
                f"{i18n.t('review.priority')} {new_priority}" if new_priority
                else i18n.t("review.priority_removed")
            )

    # Three axes, all as tags so they can be filtered and seen on the post-it:
    # area (work/personal), kind (task/appointment/note) and up to two themes.
    #
    # Area and kind go in ALWAYS, including when you wrote tags by hand. They are
    # structure, not theme: without them the note disappears from the area and
    # kind filters, and "invisible in the filter" is worse than "badly
    # classified". The first version of this treated your tags as all-or-nothing
    # and produced exactly that hole in a real note.
    area = "trabalho" if r.account == "trabalho" else "pessoal"
    axes = [area, kind]

    if note.tags_by_user:
        # Your themes stay untouched; only the missing axes are added.
        new_tags = list(dict.fromkeys([*note.tags, *axes]))
    else:
        themes = [
            x for x in dict.fromkeys(r.tags) if x in notes_mod.SUGGESTED_TAGS and x not in axes
        ][:2]
        new_tags = [*axes, *themes]

    if sorted(new_tags) != sorted(note.tags):
        store.set_tags(app.state.conn, note_id, new_tags)
        changes.append("tags " + " ".join(f"#{x}" for x in sorted(new_tags)))

    if r.is_event and r.start:
        uid = await _create_event_automatically(app, note_id, r, targets)
        if uid:
            changes.append(f"{i18n.t('review.event_created')}: {r.title}")

    if changes:
        # Invisible autonomy is worse than none: if the app touched your note or
        # wrote to your calendar, you find out immediately.
        await app.state.notify.send(
            i18n.t("review.note_reviewed"),
            f"#{note_id}: {', '.join(changes)}" + (f" — {r.reason}" if r.reason else ""),
            urgency="low",
        )
        log.info("review of #%s: %s", note_id, "; ".join(changes))


async def _create_event_automatically(app: Starlette, note_id: int, r, targets) -> str | None:
    """Create the event in the right account's dedicated calendar.

    The user chose to give up the confirmation step (amendment to ADR 0007). The
    other two guards remain: **never guests**, because that would send email to
    real people, and **only in the "Terminal Assistant" calendar**, which is what
    keeps everything deletable in one go.
    """
    # Does this Note already have an event? Then do not create another.
    #
    # `calendar_links` has a PRIMARY KEY on `note_id` and the INSERT was OR
    # REPLACE, which swapped the link and left the previous event **orphaned in
    # the calendar**. A note re-tagged three times produced three identical
    # appointments, and the database only knew about the last. Duplicating an
    # appointment is worse than not updating a title.
    existing = app.state.conn.execute(
        "SELECT uid FROM calendar_links WHERE note_id = ?", (note_id,)
    ).fetchone()
    if existing is not None:
        log.info("note #%s already has an event (%s): not creating another",
                 note_id, existing["uid"][:16])
        return None

    if not targets:
        log.warning("no 'Terminal Assistant' calendar: event for #%s not created", note_id)
        return None

    # Routing by `parent` did not work: it is an opaque Online Accounts hash, so
    # the comparison was always false and EVERY event landed in the same
    # calendar, regardless of whether the LLM said work or personal. A silent
    # error: the event appeared, just in the wrong account.
    wants_personal = r.account != "trabalho"
    chosen = next((a for a in targets if a.personal == wants_personal), targets[0])
    try:
        start_at = datetime.fromisoformat(r.start)
        end_at = datetime.fromisoformat(r.end) if r.end else start_at + timedelta(hours=1)
    except ValueError:
        log.info("review of #%s returned an invalid date: %r", note_id, r.start)
        return None

    uid = await asyncio.to_thread(
        app.state.calendar.create_event, chosen.uid,
        r.title or i18n.t("review.untitled"), start_at, end_at,
    )
    if uid:
        app.state.conn.execute(
            "INSERT OR REPLACE INTO calendar_links (note_id, uid, source_uid, created_at)"
            " VALUES (?, ?, ?, ?)",
            (note_id, uid, chosen.uid, datetime.now().isoformat(timespec="seconds")),
        )
    return uid


def _iso_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _iso_datetime(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


async def review_all(request: Request) -> JSONResponse:
    """Devolve todas as Notes abertas à fila de revisão e começa a drenar.

    Existe porque as Notes anteriores à revisão nunca passaram por ela, e porque
    editar as Priorities muda o que a revisão decidiria — reetiquetar tudo é o
    jeito de aplicar o contexto novo ao que já estava escrito.
    """
    app = request.app
    if not app.state.llm.configured:
        return JSONResponse({"error": "GEMINI_API_KEY não configurada"}, status_code=400)
    if not app.state.auto_review:
        return JSONResponse(
            {"error": "revisão desligada (TA_AUTO_REVIEW=0)"}, status_code=400
        )

    n = store.queue_all_for_review(app.state.conn)
    _schedule_review(app, limite=200)
    return JSONResponse({"queued": n, "running": len(app.state.in_review)})


async def review_status(request: Request) -> JSONResponse:
    """Quantas faltam. O mural usa isto para mostrar progresso."""
    conn = request.app.state.conn
    return JSONResponse(
        {
            "pending": conn.execute(
                "SELECT COUNT(*) FROM notes WHERE reviewed_at IS NULL"
            ).fetchone()[0],
            "running": len(request.app.state.in_review),
        }
    )


async def notes_list(request: Request) -> JSONResponse:
    include_done = request.query_params.get("done") == "1"
    # `deleted=1` devolve SÓ as apagadas: é a lixeira, não um "inclui também".
    deleted = request.query_params.get("deleted") == "1"
    notes = store.list_notes(
        request.app.state.conn, include_done=include_done, deleted=deleted
    )
    # A ordem de exibição sai daqui, não do cliente: as três visões do mural, o
    # `ta list` e qualquer outro consumidor recebem a mesma ordem sem cada um
    # reimplementá-la (ADR 0010).
    hoje = date.today()
    notes = store.by_urgency(notes, today=hoje)
    return JSONResponse({"notes": [_note_json(n, today=hoje) for n in notes]})


async def notes_delete(request: Request) -> JSONResponse:
    """Apaga de forma reversível. O evento na agenda, se houver, fica.

    Apagar a anotação sobre um compromisso não desmarca o compromisso — quem
    apaga um post-it não está cancelando a consulta no dentista.
    """
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    try:
        store.get_note(conn, note_id)
    except KeyError:
        return JSONResponse({"error": f"nota {note_id} não existe"}, status_code=404)
    store.soft_delete(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id)))


async def notes_purge(request: Request) -> JSONResponse:
    """Apaga em definitivo uma nota **que já está na lixeira**.

    O 409 quando ela não está é deliberado: recusar é melhor que apagar de
    surpresa algo que o usuário achava seguro.
    """
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    try:
        nota = store.get_note(conn, note_id)
    except KeyError:
        return JSONResponse({"error": f"nota {note_id} não existe"}, status_code=404)
    if not nota.is_deleted:
        return JSONResponse(
            {"error": f"nota {note_id} não está na lixeira. Apague primeiro."},
            status_code=409,
        )
    store.purge(conn, note_id)
    return JSONResponse({"purged": 1, "id": note_id})


async def trash_purge(request: Request) -> JSONResponse:
    """Esvazia a lixeira. Sem volta, e por isso exige `confirmed`."""
    body = await request.json() if await request.body() else {}
    if not body.get("confirmed"):
        return JSONResponse(
            {"error": "confirmação explícita é obrigatória: isto não tem volta"},
            status_code=400,
        )
    n = store.purge_all(request.app.state.conn)
    return JSONResponse({"purged": n})


async def notes_restore(request: Request) -> JSONResponse:
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    try:
        store.get_note(conn, note_id)
    except KeyError:
        return JSONResponse({"error": f"nota {note_id} não existe"}, status_code=404)
    store.restore(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id)))


async def notes_move(request: Request) -> JSONResponse:
    note_id = int(request.path_params["note_id"])
    body = await request.json()
    store.move_note(
        request.app.state.conn,
        note_id,
        sort_key=body.get("sort_key"),
        pos_x=body.get("pos_x"),
        pos_y=body.get("pos_y"),
        group_name=body.get("group"),
        color=body.get("color"),
    )
    return JSONResponse(_note_json(store.get_note(request.app.state.conn, note_id)))


async def notes_done(request: Request) -> JSONResponse:
    """Alterna conclusão. Aceita `{"done": false}` para desmarcar."""
    note_id = int(request.path_params["note_id"])
    done = True
    if await request.body():
        done = bool((await request.json()).get("done", True))
    conn = request.app.state.conn
    store.mark_done(conn, note_id) if done else store.mark_undone(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id)))


async def notes_status(request: Request) -> JSONResponse:
    """Muda o estado da Note. É o que o arrastar entre colunas do kanban chama."""
    note_id = int(request.path_params["note_id"])
    status = (await request.json()).get("status", "")
    conn = request.app.state.conn
    try:
        store.set_status(conn, note_id, status)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(_note_json(store.get_note(conn, note_id)))


async def export(request: Request) -> PlainTextResponse:
    return PlainTextResponse(store.export_markdown(request.app.state.conn))


async def board(request: Request) -> HTMLResponse:
    """Serve o mural, sempre relido do disco e nunca cacheado.

    `no-store` não é zelo excessivo: o arquivo muda junto com o código, e o
    Chrome cacheia HTML sem header por heurística própria. Sem isto, uma edição
    no mural só aparecia depois de recarga forçada — e eu perdi tempo achando que
    o CSS estava errado quando era só cache.
    """
    # O catálogo é INJETADO no HTML, e não buscado por uma rota: o idioma não muda
    # durante a vida da página, então uma segunda requisição só acrescentaria
    # latência e um modo de falha (o mural desenhado antes de o catálogo chegar,
    # mostrando chaves cruas por um instante).
    #
    # O JS não tem tabela paralela — mesma disciplina do `HORIZON_LABEL`. Uma
    # segunda tradução vivendo no cliente seria uma que nenhum teste compara.
    html = BOARD_HTML.read_text(encoding="utf-8").replace(
        I18N_MARKER, json.dumps(i18n.catalogo(), ensure_ascii=False), 1
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


async def today(request: Request) -> JSONResponse:
    """O Digest: compromissos da agenda + Notes cobráveis.

    Determinístico e instantâneo — o LLM nunca entra aqui (ADR 0003). A prosa
    gerada é enfeite opcional, pedida à parte.
    """
    app = request.app
    param = request.query_params.get("date")
    dia = date.fromisoformat(param) if param else None
    # O aquecimento leva ~30s no boot, e ler a agenda antes de ele terminar
    # bloqueia pelo mesmo tempo. Esperar aqui e DIZER que esperou é melhor que o
    # CLI estourar o timeout e o usuário achar que o daemon morreu.
    aquecendo = False
    warm = getattr(app.state, "warm_task", None)
    if warm is not None and not warm.done():
        aquecendo = True
        with contextlib.suppress(Exception):
            await warm

    eventos = await asyncio.to_thread(app.state.calendar.today, dia)
    # `dia` e não `date.today()`: com `--date`, a faixa tem de ser contada contra
    # o dia pedido, senão tudo o que ele devolve vira `vencida`. Aqui só aparecem
    # `vencida` e `hoje`, porque `due_today` filtra `due <= dia`.
    hoje = dia or date.today()
    tarefas = store.by_urgency(store.due_today(app.state.conn, today=dia), today=hoje)
    # Clima entra no Digest porque foi pedido, e degrada a None em silêncio: o
    # Digest não deve falhar porque o HA está fora do ar.
    clima = None
    with contextlib.suppress(HomeError):
        clima = (await app.state.home.sensors())["weather"]
    return JSONResponse(
        {
            "date": hoje.isoformat(),
            "weather": clima,
            "calendar_available": app.state.calendar.available,
            "calendar_error": app.state.calendar.error,
            "calendar_warming": aquecendo,
            "events": [_event_json(e) for e in eventos],
            "tasks": [_note_json(n, today=hoje) for n in tarefas],
        }
    )


async def home_light(request: Request) -> JSONResponse:
    """Liga um ou vários alvos. O termo pode ser entity_id, grupo ou ambiente."""
    body = await request.json()
    termo = (body.get("entity") or "").strip()
    if not termo:
        return JSONResponse({"error": "falta o entity"}, status_code=400)
    home = request.app.state.home
    try:
        alvos = resolve_targets(termo, await home.entities("light.", "switch."))
        if not alvos:
            return JSONResponse(
                {"error": f"nada casou com {termo!r}. Veja `ta entities`."}, status_code=404
            )
        # Sem brilho explícito, ligar uma luz quer dizer ligar por inteiro. O
        # domínio `switch` ignora o valor (veja `Home.switch_on`), então o padrão
        # não muda nada para a tomada.
        brilho = int(body["brightness"]) if "brightness" in body else DEFAULT_BRIGHTNESS
        # 0 apaga; qualquer outro valor (ou nenhum) liga.
        esperado = "off" if brilho == 0 else "on"
        resultados = []
        for entity in alvos:
            await home.switch_on(entity, brilho)
            estado, confirmado = await home.confirm(entity, esperado)
            resultados.append(
                {"entity_id": entity, "state": estado, "confirmed": confirmado}
            )
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"matched": termo, "results": resultados})


async def home_off(request: Request) -> JSONResponse:
    body = await request.json() if await request.body() else {}
    alvo = body.get("entity")
    home = request.app.state.home
    try:
        if alvo:
            entidades = resolve_targets(alvo, await home.entities("light.", "switch."))
            if not entidades:
                return JSONResponse(
                    {"error": f"nada casou com {alvo!r}. Veja `ta entities`."}, status_code=404
                )
        else:
            # Sem alvo, apaga tudo que está aceso. Nunca lista fixa no código: o
            # inventário é do HA (ADR 0001).
            entidades = [
                e["entity_id"]
                for e in await home.entities("light.", "switch.")
                if e["state"] == "on" and _commandable(e)
            ]
        resultados = []
        for entity in entidades:
            await home.turn_off(entity)
            estado, confirmado = await home.confirm(entity, "off")
            resultados.append({"entity_id": entity, "state": estado, "confirmed": confirmado})
        await request.app.state.lighter.enable(False)
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"turned_off": [r["entity_id"] for r in resultados], "results": resultados})


async def sensors_route(request: Request) -> JSONResponse:
    """Clima, roteador e consumo da tomada. Curado — ver Home.sensors()."""
    try:
        return JSONResponse(await request.app.state.home.sensors())
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


async def home_entities(request: Request) -> JSONResponse:
    try:
        ents = await request.app.state.home.entities("light.", "switch.", "media_player.")
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(
        {"entities": [{"entity_id": e["entity_id"], "state": e["state"]} for e in ents]}
    )


async def media(request: Request) -> JSONResponse:
    """Mídia nos Echo. Zero código de Alexa: são `media_player` do HA (ADR 0009)."""
    body = await request.json()
    cfg = request.app.state.config
    pedido = (body.get("entity") or "").strip()
    # Sem alvo explícito, o alvo é o Echo configurado. Uma casa com um Echo não
    # deveria ter que repetir o entity_id em todo comando.
    entity = resolve_entity(pedido) if pedido else next(iter(cfg.echo_entities), "")
    if not entity:
        return JSONResponse(
            {
                "error": "nenhum Echo configurado. Defina TA_ECHOS no .env com o "
                "entity_id do media_player, ou passe o alvo no comando."
            },
            status_code=400,
        )
    if not entity.startswith("media_player."):
        # Sem isto o HA devolve um 400 sem corpo útil e o erro chega ilegível.
        return JSONResponse(
            {"error": f"{entity!r} não é um media_player. Veja `ta entities`."},
            status_code=400,
        )
    home = request.app.state.home
    try:
        if "volume" in body:
            await home.volume(entity, int(body["volume"]))
        elif "play" in body:
            await home.play_media(entity, str(body["play"]))
        elif "action" in body:
            await home.media(entity, str(body["action"]))
        elif "announce" in body:
            await home.announce(entity, str(body["announce"]))
        else:
            return JSONResponse({"error": "nada a fazer"}, status_code=400)
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "entity_id": entity})


async def lighter_route(request: Request) -> JSONResponse:
    body = await request.json() if await request.body() else {}
    lg = request.app.state.lighter
    if not lg.available:
        return JSONResponse({"error": "Lighter não está instalada"}, status_code=502)
    if "profile" in body:
        ok = await lg.apply_profile(str(body["profile"]))
    elif body.get("toggle"):
        ok = await lg.toggle()
    else:
        ok = await lg.enable(bool(body.get("on", True)))
    return JSONResponse({"ok": ok, "enabled": await lg.get("enabled")})


async def organize(request: Request) -> JSONResponse:
    """Agrupa e ordena com o LLM, e GRAVA o resultado.

    Abrir o mural depois nunca chama o modelo, e o que foi arrastado à mão não é
    desfeito (ADR 0003).

    Desde o ADR 0010 o prazo não é mais assunto do modelo: a faixa de horizonte é
    derivada do relógio e vem na frente de tudo na exibição. O que o `organize`
    grava em `sort_key` refina a ordem **dentro** da faixa.
    """
    app = request.app
    conn = app.state.conn
    # De propósito na ordem GRAVADA, sem `by_urgency`: é o que o modelo tem de
    # ver para refinar, e é a ordem que ele vai reescrever.
    notes = store.list_notes(conn)
    if not notes:
        return JSONResponse({"placed": 0, "groups": []})
    hoje = date.today()
    try:
        res = await app.state.llm.organize(
            [_note_json(n, today=hoje) for n in notes], priorities.current(conn) or ""
        )
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    ordem = {g: i for i, g in enumerate(res.groups_in_order)}
    conhecidos = {n.id: n for n in notes}
    aplicados = 0
    for p in res.placements:
        n = conhecidos.get(p.id)
        if n is None or n.pinned_by_user:
            continue   # a mão do usuário vence a do modelo
        conn.execute(
            "UPDATE notes SET group_name = ?, sort_key = ? WHERE id = ?",
            (p.group, ordem.get(p.group, 99) * 1000 + p.rank, p.id),
        )
        aplicados += 1
    return JSONResponse(
        {
            "placed": aplicados,
            "skipped_pinned": sum(1 for n in notes if n.pinned_by_user),
            "groups": res.groups_in_order,
            "model": app.state.llm.model,
        }
    )


async def detect_event(request: Request) -> JSONResponse:
    """Propõe um evento a partir de uma nota. NÃO cria nada (ADR 0007)."""
    app = request.app
    body = await request.json()
    note_id = body.get("note_id")
    texto = body.get("text")
    if note_id is not None:
        texto = store.get_note(app.state.conn, int(note_id)).text
    if not texto:
        return JSONResponse({"error": "falta text ou note_id"}, status_code=400)
    try:
        cand = await app.state.llm.detect_event(texto)
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    alvos = {
        ("trabalho" if s.parent.startswith("b1d1") else "pessoal"): s.uid
        for s in app.state.calendar.write_targets()
    }
    return JSONResponse(
        {
            "candidate": cand.model_dump(),
            "targets": alvos,
            # Nada foi criado. O cliente confirma via POST /calendar/event.
            "created": False,
        }
    )


async def create_event(request: Request) -> JSONResponse:
    """Grava o evento confirmado. Nunca com convidados (ADR 0007)."""
    app = request.app
    body = await request.json()
    for campo in ("source_uid", "title", "start", "end"):
        if not body.get(campo):
            return JSONResponse({"error": f"falta {campo}"}, status_code=400)
    if not body.get("confirmed"):
        return JSONResponse(
            {"error": "confirmação explícita é obrigatória (ADR 0007)"}, status_code=400
        )
    uid = await asyncio.to_thread(
        app.state.calendar.create_event,
        body["source_uid"],
        body["title"],
        datetime.fromisoformat(body["start"]),
        datetime.fromisoformat(body["end"]),
    )
    if uid is None:
        return JSONResponse({"error": "não consegui criar o evento"}, status_code=502)
    if body.get("note_id"):
        app.state.conn.execute(
            "INSERT OR REPLACE INTO calendar_links (note_id, uid, source_uid, created_at)"
            " VALUES (?, ?, ?, ?)",
            (int(body["note_id"]), uid, body["source_uid"],
             datetime.now().isoformat(timespec="seconds")),
        )
    return JSONResponse({"created": True, "uid": uid})


async def priorities_route(request: Request) -> JSONResponse:
    app = request.app
    conn = app.state.conn
    if request.method == "GET":
        return JSONResponse(
            {"content": priorities.current(conn), "questions": priorities.QUESTIONS}
        )
    body = await request.json()
    if "answers" in body:
        conteudo = priorities.from_answers(body["answers"])
        priorities.save(conn, conteudo)
        return JSONResponse({"content": conteudo})
    if "instruction" in body:
        try:
            conteudo = await priorities.rewrite(conn, app.state.llm, body["instruction"])
        except LLMUnavailable as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        return JSONResponse({"content": conteudo})
    if "content" in body:
        priorities.save(conn, body["content"])
        return JSONResponse({"content": body["content"]})
    return JSONResponse({"error": "nada a fazer"}, status_code=400)


async def digest_prose(request: Request) -> JSONResponse:
    """A prosa do dia. Enfeite opcional — a listagem é o padrão (ADR 0003)."""
    app = request.app
    eventos = await asyncio.to_thread(app.state.calendar.today)
    hoje = date.today()
    tarefas = store.by_urgency(store.due_today(app.state.conn), today=hoje)
    try:
        texto = await app.state.llm.digest_prose(
            [_event_json(e) for e in eventos], [_note_json(n, today=hoje) for n in tarefas]
        )
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({"text": texto})


async def models_route(request: Request) -> JSONResponse:
    llm = request.app.state.llm
    try:
        return JSONResponse({"current": llm.model, "available": await llm.list_models()})
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)


async def rules_route(request: Request) -> JSONResponse:
    app = request.app
    return JSONResponse(
        {
            "dir": str(user_rules_dir()),
            "rules": [
                {"name": r.name, "on": [str(t) for t in r.on], "source": r.source}
                for r in app.state.rules
            ],
            "errors": [{"file": f, "traceback": tb} for f, tb in app.state.rule_errors],
        }
    )


# ── Reação aos gatilhos ─────────────────────────────────────────────────────
async def _fire_reminders(app: Starlette, agora: datetime) -> None:
    """Reminders vencidos: notifica e marca. Fecha o loop tempo → ação.

    A notificação de desktop é o caminho obrigatório; o anúncio no Echo é
    adicional, e sua ausência ou falha nunca impede o aviso (ADR 0009).
    """
    conn = app.state.conn
    for note in store.pending_reminders(conn, now=agora):
        atraso = lateness_label(lateness_of(note.remind_at, agora))
        await app.state.notify.send("Lembrete", f"{note.text}{atraso}", urgency="critical")

        for echo in app.state.config.echo_entities:
            with contextlib.suppress(HomeError):
                await app.state.home.announce(echo, f"Lembrete: {note.text}")

        store.mark_fired(conn, note.id, now=agora)
        log.info("reminder #%s disparado%s", note.id, atraso)
        await engine.dispatch(
            app.state.rules,
            _make_context(app, engine.Trigger("reminder", note.id), note=_note_json(note)),
        )


def _wire_engine(app: Starlette) -> None:
    async def on_mic(ativo: bool, apps: list[str]) -> None:
        await engine.dispatch(
            app.state.rules,
            _make_context(app, engine.Trigger("mic", ativo), extra={"apps": apps}),
        )

    async def on_time(minuto: str, agora: datetime) -> None:
        await engine.dispatch(app.state.rules, _make_context(app, engine.Trigger("time", minuto)))

    async def on_state(entity_id: str, velho: str, novo: str) -> None:
        await engine.dispatch(
            app.state.rules,
            _make_context(
                app, engine.Trigger("state", entity_id), extra={"from": velho, "to": novo}
            ),
        )

    app.state.mic = MicWatcher(on_mic)
    app.state.scheduler = Scheduler(on_time, lambda agora: _fire_reminders(app, agora))
    app.state.state_watcher = StateWatcher(app.state.home, on_state)


# ── App ─────────────────────────────────────────────────────────────────────
def create_app(
    config: Config | None = None,
    *,
    db_path=None,
    rules_dir=None,
    calendar=None,
    lighter=None,
    background: bool = True,
) -> Starlette:
    """Monta o app.

    `calendar`, `lighter` e `background` existem para injeção: sem eles, cada teste que
    sobe o app conectaria ao Evolution Data Server de verdade e pagaria o
    aquecimento das fontes. Teste não deve tocar o ambiente do usuário.
    """
    cfg = config or Config.from_env()
    # Antes de qualquer outra coisa: um bind que alcança a rede sem credencial não
    # sobe. Fica aqui e não no `main()` para valer também para quem monta o app
    # por conta própria.
    cfg.check()
    rules_path = Path(rules_dir) if rules_dir else user_rules_dir()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.config = cfg
        app.state.auto_review = cfg.auto_review
        # Referências fortes das revisões em voo: task sem dono pode ser coletada
        # pelo GC antes de terminar.
        app.state.reviews = set()
        # Ids com revisão em voo. Impede que duas capturas seguidas enfileirem a
        # mesma Note duas vezes.
        app.state.in_review = set()
        app.state.review_sem = asyncio.Semaphore(CONCURRENT_REVIEWS)
        app.state.conn = connect(db_path)
        app.state.home = Home(cfg.ha_url, cfg.ha_token)
        # `lighter` é injetável pelo mesmo motivo que `calendar`: sem isso, cada
        # teste que sobe o app roda `gsettings` de verdade e mexe nas
        # configurações da extensão do usuário — inclusive deixando
        # `auto-switch` ligado no shutdown. Teste não toca o desktop.
        app.state.lighter = lighter if lighter is not None else Lighter()
        app.state.notify = Notifier()
        app.state.calendar = calendar if calendar is not None else Calendar()
        app.state.llm = LLM(cfg.gemini_api_key)
        app.state.cal_adapter = CalendarAdapter(app.state.calendar)

        report = engine.load_rules(rules_path)
        app.state.rules, app.state.rule_errors = report.rules, report.errors
        for arquivo, _ in report.errors:
            log.error("regra %s não carregou; as outras seguem", arquivo)

        _wire_engine(app)
        # Tira a autonomia da extensão enquanto o daemon comanda, para o
        # WindowWatcher dela não sobrescrever o que uma Rule acabou de fazer.
        await app.state.lighter.take_over()

        tarefas = []
        if background:
            # Aquece a agenda em background: uma fonte recém-criada paga até 10s
            # para conectar, e o cache de clientes torna isso um custo único —
            # mas só se alguém pagar antes do usuário digitar `ta today`.
            app.state.warm_task = asyncio.create_task(
                asyncio.to_thread(app.state.calendar.warm), name="warm-calendar"
            )
            tarefas.append(app.state.warm_task)
            # Os watchers ficam sob a mesma chave porque o de microfone roda
            # `pw-dump` em subprocesso a cada segundo. Num teste isso deixava um
            # `BaseSubprocessTransport` semi-destruído quando o loop fechava, e a
            # suíte travava na saída em ~1 de cada 3 execuções.
            tarefas += [
                asyncio.create_task(app.state.scheduler.run(), name="scheduler"),
                asyncio.create_task(app.state.mic.run(), name="mic"),
                asyncio.create_task(app.state.state_watcher.run(), name="state"),
            ]
        log.info(
            "daemon de pé em %s | %d regra(s), %d erro(s)",
            cfg.base_url, len(app.state.rules), len(app.state.rule_errors),
        )
        try:
            yield
        finally:
            for t in tarefas:
                t.cancel()
            await asyncio.gather(*tarefas, return_exceptions=True)
            await app.state.lighter.hand_back()
            await app.state.home.close()
            app.state.conn.close()

    return Starlette(
        lifespan=lifespan,
        # Sem token não há middleware: o caminho local fica idêntico ao que era.
        middleware=[Middleware(TokenAuth, token=cfg.token)] if cfg.token else [],
        routes=[
            Route("/", board),
            Route("/board", board),
            Route("/health", health),
            Route("/notes", notes_create, methods=["POST"]),
            Route("/notes", notes_list, methods=["GET"]),
            Route("/notes/{note_id:int}/move", notes_move, methods=["POST"]),
            Route("/notes/{note_id:int}/done", notes_done, methods=["POST"]),
            Route("/notes/{note_id:int}/status", notes_status, methods=["POST"]),
            Route("/notes/{note_id:int}", notes_delete, methods=["DELETE"]),
            Route("/notes/{note_id:int}/restore", notes_restore, methods=["POST"]),
            Route("/notes/{note_id:int}/purge", notes_purge, methods=["DELETE"]),
            Route("/trash", trash_purge, methods=["DELETE"]),
            Route("/review-all", review_all, methods=["POST"]),
            Route("/review-status", review_status),
            Route("/export", export),
            Route("/today", today),
            Route("/home/light", home_light, methods=["POST"]),
            Route("/home/off", home_off, methods=["POST"]),
            Route("/home/entities", home_entities),
            Route("/sensors", sensors_route),
            Route("/media", media, methods=["POST"]),
            Route("/lighter", lighter_route, methods=["POST"]),
            Route("/rules", rules_route),
            Route("/organize", organize, methods=["POST"]),
            Route("/detect-event", detect_event, methods=["POST"]),
            Route("/calendar/event", create_event, methods=["POST"]),
            Route("/priorities", priorities_route, methods=["GET", "POST"]),
            Route("/digest-prose", digest_prose, methods=["POST"]),
            Route("/models", models_route),
        ],
    )


def main() -> None:
    import sys

    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config.from_env()
    try:
        app = create_app(cfg)
    except ConfigError as e:
        # A mensagem já traz o conserto; um traceback só a esconderia.
        print(f"\n{e}\n", file=sys.stderr)
        raise SystemExit(2) from None

    if cfg.exposed:
        log.warning("daemon aberto em %s — protegido por TA_TOKEN", cfg.host)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()

