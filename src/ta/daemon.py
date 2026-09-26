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
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from . import (
    board_access,
    builtin_tools,  # noqa: F401 - registers the built-in Tools
    capabilities,
    engine,
    grants,
    i18n,
    priorities,
    store,
    usage,
)
from . import members as members_mod
from . import memory as memory_mod
from . import notes as notes_mod
from . import tools as tools_mod
from .actuators.home import Home, HomeError, StateWatcher
from .actuators.lighter import Lighter
from .actuators.notify import Notifier
from .bot import AgentDeps, Bot
from .channel.telegram import TelegramChannel
from .config import (
    LOOPBACK,
    Config,
    ConfigError,
    _commandable,
    chat_config,
    config_dir,
    grants_config,
    lists_config,
    llm_prices,
    resolve_entity,
    resolve_targets,
    telegram_groups,
    telegram_owner,
)
from .config import groups as config_groups
from .db import connect, default_db_path
from .llm import LLM, LLMUnavailable, current_member, for_member
from .members import OWNER, OWNER_ID, SYSTEM, Viewer
from .scheduler import Scheduler, lateness_label, lateness_of
from .sensors.calendar import Calendar
from .sensors.mic import MicWatcher
from .speech import Transcriber

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

    def _member(self, request: Request) -> int | None:
        """Who this request speaks for, or None if it proves nobody."""
        # A session cookie first: it is the only credential that names a Member
        # other than the Owner, and it is what survives a reload (T1.7).
        cookie = request.cookies.get(board_access.COOKIE)
        if cookie and (member := board_access.session_member(self._token, cookie)):
            return member
        # `compare_digest` instead of `==`: comparing a secret with an early
        # exit leaks the correct prefix through response timing.
        presented = self._presented(request)
        if presented and hmac.compare_digest(presented, self._token):
            return OWNER_ID   # TA_TOKEN is the Owner's own credential
        return OWNER_ID if _peer_local(request) else None

    async def dispatch(self, request: Request, call_next):
        code = request.query_params.get("code")
        if request.url.path == "/board" and code:
            return self._redeem(request, code)
        member = self._member(request)
        request.state.viewer = Viewer(member) if member is not None else None
        if member is None:
            log.warning(
                "401 from %s on %s", request.client.host if request.client else "?",
                request.url.path,
            )
            return JSONResponse(
                {"error": i18n.t("auth.missing_credential")},
                status_code=401,
            )
        return await call_next(request)

    def _redeem(self, request: Request, code: str):
        """Trade a one-time code from the bot for a session cookie (T1.7)."""
        member = request.app.state.board_codes.redeem(code)
        if member is None:
            return JSONResponse({"error": i18n.t("auth.code_used")}, status_code=401)
        # A redirect, so the code leaves the address bar and the history.
        resp = RedirectResponse("/board", status_code=303)
        resp.set_cookie(
            board_access.COOKIE,
            board_access.session_cookie(self._token, member),
            max_age=int(board_access.SESSION_TTL.total_seconds()),
            httponly=True,
            # Lax, not Strict. The link is opened from the Telegram app, a
            # cross-site navigation, and a Strict cookie is withheld from the
            # redirect that follows it — the first load would 401 with the
            # cookie sitting right there.
            samesite="lax",
            path="/",
        )
        return resp


def _viewer(request: Request) -> Viewer:
    """Who this request speaks for. With no `TA_TOKEN` there is no middleware,
    and the daemon is on loopback: whoever reaches it is on this machine, the
    Owner's (ADR 0012)."""
    return getattr(request.state, "viewer", None) or OWNER


def _permissions(request: Request) -> grants.Permissions:
    """What the viewer may do (D12). Read from config.toml each time: it is
    cached by `_user_config`, and a Grant edited there applies on restart."""
    return _member_permissions(request.app.state.conn, _viewer(request).member_id)


def _member_permissions(conn, member_id: int) -> grants.Permissions:
    member = members_mod.get(conn, member_id)
    if member is None:
        return grants.NOTHING
    invited, defined = grants_config()
    return grants.permissions(member.handle, is_owner=member.is_owner, members=invited,
                              grants=defined, groups=config_groups())


def _forbidden() -> JSONResponse:
    return JSONResponse({"error": i18n.t("api.forbidden")}, status_code=403)


def _visible(request: Request, note_id: int) -> store.Note | JSONResponse:
    """The Note, if this viewer may see it; otherwise the 404 to return.

    Every route that changes a Note goes through here FIRST. Three of them
    (move, done, status) used to write before checking anything — a 500 on an
    unknown id, and with Members, a way to edit somebody else's Note by id.
    """
    try:
        return store.get_note(request.app.state.conn, note_id, viewer=_viewer(request))
    except KeyError:
        return JSONResponse({"error": i18n.t("api.note_missing", id=note_id)}, status_code=404)


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
        "list_id": n.list_id,
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

    async def now(self) -> dict | None:
        ev = await asyncio.to_thread(self._cal.now)
        return _event_json(ev) if ev else None

    async def today(self) -> list[dict]:
        evs = await asyncio.to_thread(self._cal.today)
        return [_event_json(e) for e in evs]

    # The Portuguese names, kept for good. This is the API that Rules on disk
    # call, and a Rule lives in `~/.config/ta/rules/` — outside the repository,
    # where no rename of ours can reach it. Dropping these would break somebody's
    # working automation on an upgrade, with a traceback in the daemon log and no
    # clue as to why, which is the worst way for it to happen.
    #
    # Same category as `!alta` and `@sexta`: input that somebody already typed
    # (ADR 0014). New Rules should use `now()` and `today()`.
    agora = now
    hoje = today


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
    """Diagnosis. Reports the *presence* of a secret, never the value.

    It exists because `systemctl show` does not expose what came from
    `EnvironmentFile`, and "is the service reading the .env?" is the first
    question when something in Home Assistant or the model fails.
    """
    app = request.app
    cfg: Config = app.state.config
    caps = capabilities.inspect(cfg)
    return JSONResponse(
        {
            # This used to be a literal `True`. A diagnosis that answers "ok"
            # with the core broken is not a diagnosis — it is decoration.
            "ok": all(c.ok for c in caps if c.essential),
            "lang": {"code": i18n.lang(), "source": i18n.lang_source()},
            # `reason` and `fix` go along: `Calendar` already computed a reason
            # with the fix embedded, and this route threw it away, exposing only
            # the boolean. Whoever reads `available: false` learns what, not what
            # to do about it.
            "capabilities": [
                {"key": c.key, "ok": c.ok, "reason": c.reason, "fix": c.fix}
                for c in caps
            ],
            "ha": {"url": cfg.ha_url, "token_configured": bool(cfg.ha_token)},
            "gemini": {
                "key_configured": bool(cfg.gemini_api_key),
                "model": app.state.llm.model,
            },
            "deepseek": {"key_configured": bool(cfg.deepseek_api_key)},
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
        return JSONResponse({"error": i18n.t("api.empty_text")}, status_code=400)
    note = _capture(request.app, raw)
    return JSONResponse(_note_json(note), status_code=201)


def _agent_deps(app: Starlette) -> AgentDeps:
    """The agent's collaborators (F4): the built-in Tools plus the household's."""
    report = tools_mod.load_tools(config_dir() / "tools")
    for filename, err in report.errors:
        log.error("tool file %s: %s", filename, err)
    return AgentDeps(
        llm=app.state.llm,
        registry=tools_mod.registered,
        permissions=lambda member_id: _member_permissions(app.state.conn, member_id),
        services={
            "app": app,
            # The one capture path, so a List item added by the agent is born
            # like one added on the board (not reviewed, marked as such).
            "capture": lambda raw, owner_id=OWNER_ID, list_id=None: _capture(
                app, raw, owner_id=owner_id, list_id=list_id),
        },
        persona=lambda member_id: builtin_tools.persona_line(app.state.conn, member_id),
        house_rules=lambda: chat_config()["house_rules"],
        within_budget=lambda member_id: _within_budget(app.state.conn, member_id),
    )


def _within_budget(conn, member_id: int, now: datetime | None = None) -> bool:
    """Under the Member's daily and the household's monthly ceiling (D30)."""
    limits, prices = chat_config(), llm_prices()
    now = now or datetime.now()
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (usage.spent_by_member(conn, member_id, day, prices) < limits["daily_usd_per_member"]
            and usage.spent_total(conn, day.replace(day=1), prices)
            < limits["monthly_usd_household"])


def _sync_household(conn) -> None:
    """Members and Lists follow config.toml at every boot (F3).

    Created, never deleted: dropping a line from the file revokes access, but
    what a person wrote, and the Lists with things in them, stay.
    """
    invited, _ = grants_config()
    members_mod.sync(conn, owner=telegram_owner(), invited=list(invited))
    now = datetime.now().isoformat(timespec="seconds")
    for name, scope in lists_config().items():
        conn.execute(
            "INSERT OR IGNORE INTO lists (name, scope, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (name, scope, OWNER_ID, now),
        )


def _board_link(app: Starlette, cfg: Config, member_id: int = OWNER_ID) -> str | None:
    """The address the bot sends for `/board`, or None if a phone cannot reach it.

    On loopback nothing outside this machine can open it, so there is no link to
    send. With `TA_TOKEN` the link carries a one-time code; without one, the
    daemon is on loopback anyway.
    """
    if not cfg.exposed or cfg.token is None:
        return None
    base = cfg.public_url or (
        f"http://{addr}:{cfg.port}" if (addr := board_access.lan_address()) else None
    )
    if base is None:
        return None
    return f"{base.rstrip('/')}/board?code={app.state.board_codes.issue(member_id)}"


def _capture(
    app: Starlette, raw: str, *, owner_id: int = OWNER_ID, list_id: int | None = None
) -> store.Note:
    """Capture a Note: the one path shared by `POST /notes` and the bot.

    Review goes out in the background and the Note comes back now: capture never
    waits for the network (ADR 0003). Whatever it changes shows up on the board
    at the next reload.
    """
    note = store.add_note(app.state.conn, raw, owner_id=owner_id, list_id=list_id)
    if list_id is not None:
        # A List item is never reviewed: a model call so "leite" gains a deadline
        # and a tag is cost with no use. Marking it reviewed is what keeps it out
        # of the backlog queue too, not just out of this capture's review.
        store.mark_reviewed(app.state.conn, note.id)
        return note
    _schedule_review(app, note)
    return note


# The confidence floor for acting alone. Below it, review does nothing: a wrong
# correction is worse than none, and a ghost event in a work calendar is visible
# to colleagues (amendment to ADR 0007).
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


async def _review_call(app: Starlette, note: store.Note, targets):
    return await app.state.llm.review_capture(
        note.text,
        due=note.due,
        remind_at=note.remind_at,
        # The WRITER's Priorities: Ana's note is weighed by what matters to
        # Ana, not to the Owner (D22).
        priorities=priorities.current(app.state.conn, note.owner_id) or "",
        # Only the domain, not the address: it is what decides the routing,
        # and sending the whole email outside would be extra data for the
        # same result.
        accounts=", ".join(
            f"{'pessoal' if a.personal else 'trabalho'}: "
            f"{a.account.rsplit('@', 1)[-1] if '@' in a.account else '?'}"
            for a in targets
        ),
    )


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
        note = store.get_note(app.state.conn, note_id, viewer=SYSTEM)
    except KeyError:
        return  # deleted before review got to it

    # Count the attempt BEFORE trying: if the process dies halfway, the Note does
    # not keep retrying forever on the next boot.
    attempts = store.count_review_attempt(app.state.conn, note_id)

    try:
        # A broad Exception on purpose: this function is optional by design, and
        # nothing it does is worth taking down the daemon or losing the Note.
        targets = await asyncio.to_thread(app.state.calendar.write_targets)
        # The review serves the Note's writer: it counts against their ceiling.
        with for_member(note.owner_id):
            r = await _review_call(app, note, targets)
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
    """Put every open Note back in the review queue and start draining.

    It exists because Notes older than review never went through it, and because
    editing Priorities changes what review would decide — re-tagging everything is
    how you apply the new context to what was already written.
    """
    app = request.app
    if not app.state.llm.configured:
        return JSONResponse({"error": i18n.t("api.ai_not_configured")}, status_code=400)
    if not app.state.auto_review:
        return JSONResponse(
            {"error": i18n.t("api.review_off")}, status_code=400
        )

    n = store.queue_all_for_review(app.state.conn, viewer=_viewer(request))
    _schedule_review(app, limite=200)
    return JSONResponse({"queued": n, "running": len(app.state.in_review)})


async def review_status(request: Request) -> JSONResponse:
    """How many are left. The board uses this to show progress."""
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
    # `deleted=1` returns ONLY the deleted ones: it is the trash, not an "also include".
    deleted = request.query_params.get("deleted") == "1"
    notes = store.list_notes(
        request.app.state.conn, viewer=_viewer(request), include_done=include_done,
        deleted=deleted,
        # List items live in the List view, outside the Horizon order (D7). The
        # trash still shows them: recovering a deleted item happens there too.
        in_lists=deleted,
    )
    # Display order comes from here, not from the client: the board's three
    # views, `ta list` and any other consumer get the same order without each
    # reimplementing it (ADR 0010).
    reference_day = date.today()
    notes = store.by_urgency(notes, today=reference_day)
    return JSONResponse({"notes": [_note_json(n, today=reference_day) for n in notes]})


async def lists_route(request: Request) -> JSONResponse:
    """The Lists the viewer sees, with their open items (T3.5)."""
    perms = _permissions(request)
    return JSONResponse({"lists": [
        {"id": lv.id, "name": lv.name, "scope": lv.scope,
         # Whether the viewer may add to it: a household List needs the Grant,
         # one's own personal List never does.
         "writable": lv.scope == "personal" or perms.list_(lv.name),
         "items": [_note_json(n) for n in lv.items]}
        for lv in store.lists_for(request.app.state.conn, viewer=_viewer(request))
    ]})


async def list_add(request: Request) -> JSONResponse:
    list_id = int(request.path_params["list_id"])
    text = ((await request.json()).get("text") or "").strip()
    if not text:
        return JSONResponse({"error": i18n.t("api.empty_text")}, status_code=400)
    viewer = _viewer(request)
    lv = next((x for x in store.lists_for(request.app.state.conn, viewer=viewer)
               if x.id == list_id), None)
    if lv is None:
        return JSONResponse({"error": i18n.t("api.list_missing")}, status_code=404)
    if lv.scope == "household" and not _permissions(request).list_(lv.name):
        return _forbidden()
    note = _capture(request.app, text, owner_id=viewer.member_id, list_id=list_id)
    return JSONResponse(_note_json(note), status_code=201)


async def notes_delete(request: Request) -> JSONResponse:
    """Delete reversibly. The calendar event, if there is one, stays.

    Deleting the note about an appointment does not cancel the appointment —
    somebody deleting a post-it is not cancelling their dentist visit.
    """
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    if isinstance(found := _visible(request, note_id), JSONResponse):
        return found
    store.soft_delete(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id, viewer=_viewer(request))))


async def notes_purge(request: Request) -> JSONResponse:
    """Permanently delete a note **that is already in the trash**.

    The 409 when it is not there is deliberate: refusing beats deleting by
    surprise something the user thought was safe.
    """
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    if isinstance(nota := _visible(request, note_id), JSONResponse):
        return nota
    if not nota.is_deleted:
        return JSONResponse(
            {"error": i18n.t("api.note_not_in_trash", id=note_id)},
            status_code=409,
        )
    store.purge(conn, note_id)
    return JSONResponse({"purged": 1, "id": note_id})


async def trash_purge(request: Request) -> JSONResponse:
    """Empty the trash. No way back, which is why it requires `confirmed`."""
    body = await request.json() if await request.body() else {}
    if not body.get("confirmed"):
        return JSONResponse(
            {"error": i18n.t("api.confirm_required")},
            status_code=400,
        )
    n = store.purge_all(request.app.state.conn, viewer=_viewer(request))
    return JSONResponse({"purged": n})


async def notes_restore(request: Request) -> JSONResponse:
    note_id = int(request.path_params["note_id"])
    conn = request.app.state.conn
    if isinstance(found := _visible(request, note_id), JSONResponse):
        return found
    store.restore(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id, viewer=_viewer(request))))


async def notes_move(request: Request) -> JSONResponse:
    note_id = int(request.path_params["note_id"])
    body = await request.json()
    if isinstance(found := _visible(request, note_id), JSONResponse):
        return found
    store.move_note(
        request.app.state.conn,
        note_id,
        sort_key=body.get("sort_key"),
        pos_x=body.get("pos_x"),
        pos_y=body.get("pos_y"),
        group_name=body.get("group"),
        color=body.get("color"),
    )
    return JSONResponse(
        _note_json(store.get_note(request.app.state.conn, note_id, viewer=_viewer(request)))
    )


async def notes_done(request: Request) -> JSONResponse:
    """Toggle completion. Accepts `{"done": false}` to unmark."""
    note_id = int(request.path_params["note_id"])
    done = True
    if await request.body():
        done = bool((await request.json()).get("done", True))
    conn = request.app.state.conn
    if isinstance(found := _visible(request, note_id), JSONResponse):
        return found
    store.mark_done(conn, note_id) if done else store.mark_undone(conn, note_id)
    return JSONResponse(_note_json(store.get_note(conn, note_id, viewer=_viewer(request))))


async def notes_status(request: Request) -> JSONResponse:
    """Change the Note's state. It is what dragging between kanban columns calls."""
    note_id = int(request.path_params["note_id"])
    status = (await request.json()).get("status", "")
    conn = request.app.state.conn
    if isinstance(found := _visible(request, note_id), JSONResponse):
        return found
    try:
        store.set_status(conn, note_id, status)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse(_note_json(store.get_note(conn, note_id, viewer=_viewer(request))))


async def export(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        store.export_markdown(request.app.state.conn, viewer=_viewer(request))
    )


async def board(request: Request) -> HTMLResponse:
    """Serve the board, always re-read from disk and never cached.

    `no-store` is not excessive care: the file changes along with the code, and
    Chrome caches header-less HTML by its own heuristic. Without this, an edit to
    the board only appeared after a forced reload — and time was lost thinking the
    CSS was wrong when it was only the cache.
    """
    # The catalogue is INJECTED into the HTML rather than fetched by a route: the
    # language does not change during the life of the page, so a second request
    # would only add latency and a failure mode (the board drawn before the
    # catalogue arrives, showing raw keys for an instant).
    #
    # The JS has no parallel table — the same discipline as `HORIZON_LABEL`. A
    # second translation living in the client would be one no test compares.
    html = BOARD_HTML.read_text(encoding="utf-8").replace(
        I18N_MARKER, json.dumps(i18n.catalogo(), ensure_ascii=False), 1
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


async def today(request: Request) -> JSONResponse:
    """The Digest: calendar events plus chaseable Notes.

    Deterministic and instant — the LLM never comes in here (ADR 0003). The
    generated prose is optional decoration, asked for separately.
    """
    app = request.app
    param = request.query_params.get("date")
    day = date.fromisoformat(param) if param else None
    # Warming takes ~30s at boot, and reading the calendar before it finishes
    # blocks for the same time. Waiting here and SAYING that it waited is better
    # than the CLI blowing its timeout and the user thinking the daemon died.
    warming = False
    warm = getattr(app.state, "warm_task", None)
    if warm is not None and not warm.done():
        warming = True
        with contextlib.suppress(Exception):
            await warm

    events = await asyncio.to_thread(app.state.calendar.today, day)
    # `day` rather than `date.today()`: with `--date`, the band has to be counted
    # against the requested day, otherwise everything it returns becomes
    # `overdue`. Only `overdue` and `today` appear here, because `due_today`
    # filters `due <= day`.
    reference_day = day or date.today()
    tasks = store.by_urgency(
        store.due_today(app.state.conn, viewer=_viewer(request), today=day), today=reference_day
    )
    # The weather goes into the Digest because it was asked for, and degrades to
    # None silently: the Digest must not fail because Home Assistant is down.
    weather = None
    with contextlib.suppress(HomeError):
        weather = (await app.state.home.sensors())["weather"]
    return JSONResponse(
        {
            "date": reference_day.isoformat(),
            "weather": weather,
            "calendar_available": app.state.calendar.available,
            "calendar_error": app.state.calendar.error,
            "calendar_warming": warming,
            "events": [_event_json(e) for e in events],
            "tasks": [_note_json(n, today=reference_day) for n in tasks],
        }
    )


async def home_light(request: Request) -> JSONResponse:
    """Turn on one or several targets. The term can be an entity_id, group or room."""
    body = await request.json()
    termo = (body.get("entity") or "").strip()
    if not termo:
        return JSONResponse({"error": i18n.t("api.missing_entity")}, status_code=400)
    home = request.app.state.home
    try:
        targets = resolve_targets(termo, await home.entities("light.", "switch."))
        if not targets:
            return JSONResponse(
                {"error": i18n.t("api.no_match", termo=termo)}, status_code=404
            )
        # Only what the viewer's Grant covers (D12). All out of reach is a 403,
        # not a 404: the Entity exists, it is just not theirs to switch.
        perms = _permissions(request)
        targets = [e for e in targets if perms.entity(e)]
        if not targets:
            return _forbidden()
        # With no explicit brightness, turning a light on means turning it fully
        # on. The `switch` domain ignores the value (see `Home.switch_on`), so
        # the default changes nothing for a plug.
        brilho = int(body["brightness"]) if "brightness" in body else DEFAULT_BRIGHTNESS
        # 0 turns off; any other value (or none) turns on.
        esperado = "off" if brilho == 0 else "on"
        resultados = []
        for entity in targets:
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
    target = body.get("entity")
    home = request.app.state.home
    perms = _permissions(request)
    try:
        if target:
            entities = resolve_targets(target, await home.entities("light.", "switch."))
            if not entities:
                return JSONResponse(
                    {"error": i18n.t("api.no_match", termo=target)}, status_code=404
                )
            if not (entities := [e for e in entities if perms.entity(e)]):
                return _forbidden()
        else:
            # With no target, turn off everything that is on. Never a hardcoded
            # list: the inventory belongs to Home Assistant (ADR 0001).
            # "Everything" means everything THIS viewer may switch: a housemate's
            # "turn it all off" must not reach the Owner's bedroom mid-call.
            entities = [
                e["entity_id"]
                for e in await home.entities("light.", "switch.")
                if e["state"] == "on" and _commandable(e) and perms.entity(e["entity_id"])
            ]
        resultados = []
        for entity in entities:
            await home.turn_off(entity)
            estado, confirmado = await home.confirm(entity, "off")
            resultados.append({"entity_id": entity, "state": estado, "confirmed": confirmado})
        # The ringlight is the Owner's desk, not the house's.
        if perms.is_admin:
            await request.app.state.lighter.enable(False)
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"turned_off": [r["entity_id"] for r in resultados], "results": resultados})


async def sensors_route(request: Request) -> JSONResponse:
    """Weather, router and the plug's consumption. Curated — see Home.sensors()."""
    try:
        return JSONResponse(await request.app.state.home.sensors())
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)


async def home_entities(request: Request) -> JSONResponse:
    try:
        ents = await request.app.state.home.entities("light.", "switch.", "media_player.")
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    perms = _permissions(request)
    return JSONResponse(
        {"entities": [{"entity_id": e["entity_id"], "state": e["state"]} for e in ents
                      if perms.entity(e["entity_id"])]}
    )


async def media(request: Request) -> JSONResponse:
    """Media on the Echos. Zero Alexa code: they are Home Assistant `media_player`s (ADR 0009)."""
    if not _permissions(request).is_admin:
        return _forbidden()
    body = await request.json()
    cfg = request.app.state.config
    pedido = (body.get("entity") or "").strip()
    # With no explicit target, the target is the configured Echo. A house with
    # one Echo should not require typing its entity_id in every command.
    entity = resolve_entity(pedido) if pedido else next(iter(cfg.echo_entities), "")
    if not entity:
        return JSONResponse(
            {
                "error": i18n.t("api.no_echo")
            },
            status_code=400,
        )
    if not entity.startswith("media_player."):
        # Without this, Home Assistant returns a 400 with no useful body and the
        # error arrives unreadable.
        return JSONResponse(
            {"error": i18n.t("api.not_media_player", entity=entity)},
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
            return JSONResponse({"error": i18n.t("api.nothing_to_do")}, status_code=400)
    except HomeError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse({"ok": True, "entity_id": entity})


async def lighter_route(request: Request) -> JSONResponse:
    if not _permissions(request).is_admin:
        return _forbidden()
    body = await request.json() if await request.body() else {}
    lg = request.app.state.lighter
    if not lg.available:
        return JSONResponse({"error": i18n.t("api.lighter_missing")}, status_code=502)
    if "profile" in body:
        ok = await lg.apply_profile(str(body["profile"]))
    elif body.get("toggle"):
        ok = await lg.toggle()
    else:
        ok = await lg.enable(bool(body.get("on", True)))
    return JSONResponse({"ok": ok, "enabled": await lg.get("enabled")})


async def organize(request: Request) -> JSONResponse:
    """Agrupa e ordena com o LLM, e GRAVA o resultado.

    Opening the board afterwards never calls the model, and what was dragged by
    hand is not undone (ADR 0003).

    Since ADR 0010 the deadline is no longer the model's business: the horizon
    band is derived from the clock and comes ahead of everything at display time.
    What `organize` writes to `sort_key` refines the order **within** the band.
    """
    app = request.app
    conn = app.state.conn
    # Deliberately in STORED order, without `by_urgency`: it is what the model
    # has to see in order to refine, and it is the order it will rewrite.
    notes = store.list_notes(conn, viewer=_viewer(request))
    if not notes:
        return JSONResponse({"placed": 0, "groups": []})
    reference_day = date.today()
    try:
        res = await app.state.llm.organize(
            [_note_json(n, today=reference_day) for n in notes],
            priorities.current(conn, _viewer(request).member_id) or "",
        )
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    order = {g: i for i, g in enumerate(res.groups_in_order)}
    conhecidos = {n.id: n for n in notes}
    aplicados = 0
    for p in res.placements:
        n = conhecidos.get(p.id)
        if n is None or n.pinned_by_user:
            continue   # the user's hand beats the model's
        conn.execute(
            "UPDATE notes SET group_name = ?, sort_key = ? WHERE id = ?",
            (p.group, order.get(p.group, 99) * 1000 + p.rank, p.id),
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
    """Propose an event from a note. It creates NOTHING (ADR 0007)."""
    app = request.app
    body = await request.json()
    note_id = body.get("note_id")
    text = body.get("text")
    if note_id is not None:
        if isinstance(found := _visible(request, int(note_id)), JSONResponse):
            return found
        text = found.text
    if not text:
        return JSONResponse({"error": "falta text ou note_id"}, status_code=400)
    try:
        cand = await app.state.llm.detect_event(text)
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)

    targets = {
        ("trabalho" if s.parent.startswith("b1d1") else "pessoal"): s.uid
        for s in app.state.calendar.write_targets()
    }
    return JSONResponse(
        {
            "candidate": cand.model_dump(),
            "targets": targets,
            # Nothing was created. The client confirms via POST /calendar/event.
            "created": False,
        }
    )


async def create_event(request: Request) -> JSONResponse:
    """Write the confirmed event. Never with guests (ADR 0007)."""
    app = request.app
    body = await request.json()
    for field in ("source_uid", "title", "start", "end"):
        if not body.get(field):
            return JSONResponse({"error": f"falta {field}"}, status_code=400)
    if not body.get("confirmed"):
        return JSONResponse(
            {"error": i18n.t("api.confirm_required_event")}, status_code=400
        )
    uid = await asyncio.to_thread(
        app.state.calendar.create_event,
        body["source_uid"],
        body["title"],
        datetime.fromisoformat(body["start"]),
        datetime.fromisoformat(body["end"]),
    )
    if uid is None:
        return JSONResponse({"error": i18n.t("api.event_failed")}, status_code=502)
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
    me = _viewer(request).member_id
    if request.method == "GET":
        return JSONResponse(
            {"content": priorities.current(conn, me), "questions": priorities.QUESTIONS}
        )
    body = await request.json()
    if "answers" in body:
        conteudo = priorities.from_answers(body["answers"])
        priorities.save(conn, conteudo, member_id=me)
        return JSONResponse({"content": conteudo})
    if "instruction" in body:
        try:
            conteudo = await priorities.rewrite(conn, app.state.llm, body["instruction"], me)
        except LLMUnavailable as e:
            return JSONResponse({"error": str(e)}, status_code=503)
        return JSONResponse({"content": conteudo})
    if "content" in body:
        priorities.save(conn, body["content"], member_id=me)
        return JSONResponse({"content": body["content"]})
    return JSONResponse({"error": i18n.t("api.nothing_to_do")}, status_code=400)


async def digest_prose(request: Request) -> JSONResponse:
    """The day in prose. Optional decoration — the listing is the default (ADR 0003)."""
    app = request.app
    events = await asyncio.to_thread(app.state.calendar.today)
    reference_day = date.today()
    tasks = store.by_urgency(
        store.due_today(app.state.conn, viewer=_viewer(request)), today=reference_day
    )
    try:
        text = await app.state.llm.digest_prose(
            [_event_json(e) for e in events], [_note_json(n, today=reference_day) for n in tasks]
        )
    except LLMUnavailable as e:
        return JSONResponse({"error": str(e)}, status_code=503)
    return JSONResponse({"text": text})


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


# ── Reacting to triggers ────────────────────────────────────────────────────
async def _fire_reminders(app: Starlette, now: datetime) -> None:
    """Due Reminders: notify and mark. It closes the time → action loop.

    The desktop notification is the mandatory path; the Echo announcement is
    additional, and its absence or failure never blocks the alert (ADR 0009).
    """
    conn = app.state.conn
    for note in store.pending_reminders(conn, now=now):
        late = lateness_label(lateness_of(note.remind_at, now))
        await app.state.notify.send("Lembrete", f"{note.text}{late}", urgency="critical")

        for echo in app.state.config.echo_entities:
            with contextlib.suppress(HomeError):
                await app.state.home.announce(echo, f"Lembrete: {note.text}")

        store.mark_fired(conn, note.id, now=now)
        log.info("reminder #%s disparado%s", note.id, late)
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

    async def on_time(minuto: str, now: datetime) -> None:
        await engine.dispatch(app.state.rules, _make_context(app, engine.Trigger("time", minuto)))

    async def on_state(entity_id: str, old: str, new: str) -> None:
        await engine.dispatch(
            app.state.rules,
            _make_context(
                app, engine.Trigger("state", entity_id), extra={"from": old, "to": new}
            ),
        )

    app.state.mic = MicWatcher(on_mic)
    app.state.scheduler = Scheduler(on_time, lambda now: _fire_reminders(app, now))
    app.state.state_watcher = StateWatcher(app.state.home, on_state)


# ── App ─────────────────────────────────────────────────────────────────────
def _usage_recorder(app: Starlette):
    """Write each answered model call to `llm_usage` (ADR 0018).

    Never raises. The call it describes already succeeded, and failing it over
    the bookkeeping would throw away a review the user is waiting for.
    """
    prices = llm_prices()

    def on_usage(provider: str, model: str, task: str, spent) -> None:
        try:
            usage.record(app.state.conn, provider=provider, model=model, task=task,
                         usage=spent, prices=prices, member_id=current_member())
        except Exception:
            log.exception("could not record model usage for %s/%s", provider, task)

    return on_usage


def create_app(
    config: Config | None = None,
    *,
    db_path=None,
    rules_dir=None,
    calendar=None,
    lighter=None,
    channel=None,
    background: bool = True,
) -> Starlette:
    """Monta o app.

    `calendar`, `lighter` and `background` exist for injection: without them,
    every test that boots the app would connect to a real Evolution Data Server
    and pay for warming the sources. A test must not touch the user's environment.
    """
    cfg = config or Config.from_env()
    # Before anything else: a bind that reaches the network with no credential
    # does not start. It lives here rather than in `main()` so it also applies to
    # anyone assembling the app themselves.
    cfg.check()
    rules_path = Path(rules_dir) if rules_dir else user_rules_dir()

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        app.state.config = cfg
        app.state.auto_review = cfg.auto_review
        # Strong references to in-flight reviews: an ownerless task can be
        # collected by the GC before it finishes.
        app.state.reviews = set()
        # Ids with a review in flight. Stops two consecutive captures queuing
        # the same Note twice.
        app.state.in_review = set()
        app.state.review_sem = asyncio.Semaphore(CONCURRENT_REVIEWS)
        app.state.conn = connect(db_path)
        # Untranscribed audio lives next to the database: same owner, same
        # backup, and a test's temporary database takes its audio with it.
        db_path_resolved = Path(db_path) if db_path else default_db_path()
        app.state.home = Home(cfg.ha_url, cfg.ha_token)
        # `lighter` is injectable for the same reason as `calendar`: without it,
        # every test that boots the app runs a real `gsettings` and changes the
        # user's extension settings — including leaving `auto-switch` on at
        # shutdown. A test does not touch the desktop.
        app.state.lighter = lighter if lighter is not None else Lighter()
        app.state.notify = Notifier()
        app.state.calendar = calendar if calendar is not None else Calendar()
        app.state.llm = LLM.from_config(cfg)
        app.state.llm.on_usage = _usage_recorder(app)
        app.state.cal_adapter = CalendarAdapter(app.state.calendar)
        # Injectable like `calendar`: a test hands in a fake and never reaches
        # Telegram.
        app.state.channel = channel if channel is not None else TelegramChannel(
            cfg.telegram_token
        )
        _sync_household(app.state.conn)
        # Conversation Memory has a retention (D13). At boot is enough: the daemon
        # restarts on every deploy, and a few extra days of memory harm nothing.
        if forgotten := memory_mod.forget_older(app.state.conn):
            log.info("forgot %d message(s) past the retention", forgotten)
        app.state.board_codes = board_access.BoardCodes()
        app.state.bot = Bot(
            app.state.conn,
            app.state.channel,
            capture=lambda raw, owner_id=OWNER_ID: _capture(app, raw, owner_id=owner_id),
            owner_username=telegram_owner(),
            board_link=lambda member_id: _board_link(app, cfg, member_id),
            invited=lambda: set(grants_config()[0]),
            allowed_groups=telegram_groups,
            transcriber=Transcriber(),
            audio_dir=db_path_resolved.parent / "audio",
            # The agent only when a model is there to drive it: without one, every
            # text is captured, which is F1's behaviour and keeps invariant 1.
            agent=_agent_deps(app) if app.state.llm.configured else None,
        )

        report = engine.load_rules(rules_path)
        app.state.rules, app.state.rule_errors = report.rules, report.errors
        for filename, _ in report.errors:
            log.error("rule %s did not load; the others carry on", filename)

        _wire_engine(app)
        # Take autonomy from the extension while the daemon is in charge, so its
        # WindowWatcher does not overwrite what a Rule just did.
        await app.state.lighter.take_over()

        tasks = []
        if background:
            # Warm the calendar in the background: a freshly created source pays
            # up to 10s to connect, and the client cache makes that a one-off
            # cost — but only if somebody pays it before the user types
            # `ta today`.
            app.state.warm_task = asyncio.create_task(
                asyncio.to_thread(app.state.calendar.warm), name="warm-calendar"
            )
            tasks.append(app.state.warm_task)
            # The watchers live under the same key because the microphone one runs
            # `pw-dump` in a subprocess every second. In a test that left a
            # half-destroyed `BaseSubprocessTransport` when the loop closed, and
            # the suite hung on exit in roughly 1 run in 3.
            tasks += [
                asyncio.create_task(app.state.scheduler.run(), name="scheduler"),
                asyncio.create_task(app.state.mic.run(), name="mic"),
                asyncio.create_task(app.state.state_watcher.run(), name="state"),
            ]
            # Without an Owner to pair there is nobody the bot may answer, so it
            # does not poll at all rather than read messages it would drop.
            if app.state.channel.configured and telegram_owner():
                tasks.append(asyncio.create_task(
                    app.state.channel.run(app.state.bot.handle), name="channel"
                ))
        log.info(
            "daemon up on %s | %d rule(s), %d error(s)",
            cfg.base_url, len(app.state.rules), len(app.state.rule_errors),
        )
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await app.state.lighter.hand_back()
            await app.state.home.close()
            if hasattr(app.state.channel, "close"):
                await app.state.channel.close()
            app.state.conn.close()

    return Starlette(
        lifespan=lifespan,
        # With no token there is no middleware: the local path is identical to before.
        middleware=[Middleware(TokenAuth, token=cfg.token)] if cfg.token else [],
        routes=[
            Route("/", board),
            Route("/board", board),
            Route("/health", health),
            Route("/notes", notes_create, methods=["POST"]),
            Route("/notes", notes_list, methods=["GET"]),
            Route("/lists", lists_route),
            Route("/lists/{list_id:int}/items", list_add, methods=["POST"]),
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
        # The message already carries the fix; a traceback would only hide it.
        print(f"\n{e}\n", file=sys.stderr)
        raise SystemExit(2) from None

    if cfg.exposed:
        log.warning("daemon aberto em %s — protegido por TA_TOKEN", cfg.host)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")


if __name__ == "__main__":
    main()

