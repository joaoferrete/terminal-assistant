"""The calendar, through Evolution Data Server over DBus.

GNOME does the OAuth with an already-verified client and renews the token
itself; the app only reads what Evolution has already synced (ADR 0004). Local
and instant reads, which is what makes it viable to use the calendar as a Rule
condition without paying network latency.

Requires `gi`, which only exists in the system Python (ADR 0005), plus the
`gir1.2-ecal-2.0` and `gir1.2-edataserver-1.2` typelibs.

**Calendar UIDs are never hardcoded.** Evolution regenerates them if a calendar
is recreated; resolution is by display name plus parent account.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

log = logging.getLogger("ta.calendar")

# Evolution's local calendars, which do NOT sync with a provider.
# `system-calendar` is called "Personal" and is the trap: writing there never
# shows up on your phone.
LOCAL_ONLY = ("system-calendar", "birthdays")

WRITE_CALENDAR = "Terminal Assistant"

# Consumer email providers. An account on its own domain is treated as work,
# which is the right heuristic here: somebody with their own domain in Calendar
# has it because of a company.
CONSUMER_PROVIDERS = frozenset(
    {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
     "icloud.com", "me.com", "yahoo.com", "proton.me", "protonmail.com"}
)
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# How long to wait for a backend to come online, per source. Paid once per
# source, thanks to the client cache.
CONNECT_WAIT_S = 10


@dataclass
class Event:
    uid: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    calendar: str
    source_uid: str

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


@dataclass
class CalendarSource:
    uid: str
    name: str
    parent: str
    local: bool
    # The email of the account this calendar belongs to, when it can be known.
    #
    # Necessary because `parent` is an **opaque hash** from Online Accounts
    # ('d2c8054404442a86eb...'), not a readable name: routing by it is
    # impossible. The usable signal is that the provider always creates, in each
    # account, a calendar named after its email — so the sibling that looks like
    # an email identifies the whole account.
    account: str = ""

    @property
    def personal(self) -> bool:
        """A consumer account, by email provider. Your own domain means work."""
        domain = self.account.rsplit("@", 1)[-1].lower() if "@" in self.account else ""
        return domain in CONSUMER_PROVIDERS


class Calendar:
    """Reading from and writing to Evolution's calendars.

    It imports `gi` lazily so the rest of the app works on a machine with no
    typelibs — note capture does not depend on the calendar.
    """

    def __init__(self) -> None:
        self._registry = None
        self._error: str | None = None
        # Connecting to a source costs up to CONNECT_WAIT_S, and a freshly
        # created calendar pays the whole timeout because Evolution has no cache
        # for it yet — measured: 10s for a new calendar, 0.01s for old ones.
        # Without the cache, `ta today` took 20s.
        self._clients: dict[str, object] = {}

    def _load(self):
        if self._registry is not None or self._error is not None:
            return self._registry
        try:
            import gi

            gi.require_version("EDataServer", "1.2")
            gi.require_version("ECal", "2.0")
            gi.require_version("ICalGLib", "3.0")
            from gi.repository import EDataServer

            self._registry = EDataServer.SourceRegistry.new_sync(None)
        except (ImportError, ValueError) as e:
            # The library or the typelib is missing: the fix is `apt`.
            from ..i18n import t

            self._error = t("calendar.no_typelibs", erro=e)
            log.warning("%s", self._error)
        except Exception as e:  # noqa: BLE001
            # The typelibs exist, but there is no bus to talk to Evolution:
            # `GLib.GError: Cannot autolaunch D-Bus without X11 $DISPLAY`. It
            # happens on a server, in a container, over SSH with no graphical
            # session, and in CI — and before this it **raised**, taking down
            # `ta doctor` and `ta --help` exactly where they most need to answer.
            #
            # A broad `Exception` on purpose: any failure to reach Evolution
            # means the same thing to the user, which is "there is no calendar
            # here", and the alternative is enumerating error types from a C
            # stack.
            from ..i18n import t

            self._error = t("calendar.no_bus", erro=e)
            log.warning("%s", self._error)
        return self._registry

    @property
    def available(self) -> bool:
        return self._load() is not None

    @property
    def error(self) -> str | None:
        self._load()
        return self._error

    # ── Sources ─────────────────────────────────────────────────────────────
    def sources(self) -> list[CalendarSource]:
        reg = self._load()
        if reg is None:
            return []
        from gi.repository import EDataServer

        enabled = [
            s for s in reg.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR) if s.get_enabled()
        ]
        # First find each account's email, through the sibling named after it.
        accounts: dict[str, str] = {}
        for s in enabled:
            name = s.get_display_name() or ""
            if RE_EMAIL.match(name):
                accounts[s.get_parent() or ""] = name

        out = []
        for s in enabled:
            uid = s.get_uid()
            parent = s.get_parent() or ""
            out.append(
                CalendarSource(
                    uid=uid,
                    name=s.get_display_name(),
                    parent=parent,
                    local=uid in LOCAL_ONLY or parent.endswith("-stub"),
                    account=accounts.get(parent, ""),
                )
            )
        return out

    def write_targets(self) -> list[CalendarSource]:
        """The dedicated write calendars — one per account (amendment to ADR 0007)."""
        return [s for s in self.sources() if s.name == WRITE_CALENDAR and not s.local]

    # ── Clients ─────────────────────────────────────────────────────────────
    def _client(self, src):
        """A connected client for a source, memoised.

        Without this, every read reconnects to every source and pays the timeout
        of the ones Evolution has not cached yet.
        """
        uid = src.get_uid()
        cached = self._clients.get(uid)
        if cached is not None:
            return cached
        from gi.repository import ECal

        client = ECal.Client.connect_sync(
            src, ECal.ClientSourceType.EVENTS, CONNECT_WAIT_S, None
        )
        self._clients[uid] = client
        return client

    def warm(self) -> int:
        """Connect to every source, so the reads that follow are instant.

        **In parallel, and the reason is measured.** `connect_sync` takes a
        `wait_for_connected_seconds` which, for a cloud source, is consumed in
        full: each of the six cloud calendars spent exactly 10s and *then*
        connected successfully. In series that was 60s of boot, and the first
        `ta today` after a restart blew the CLI's timeout. Concurrently, the cost
        becomes the slowest single source rather than the sum.

        The client `dict` is written by several threads, which is safe here:
        assignment into a `dict` is atomic, and two threads resolving the same
        source would store equivalent clients.
        """
        reg = self._load()
        if reg is None:
            return 0
        from concurrent.futures import ThreadPoolExecutor

        from gi.repository import EDataServer

        fontes = [
            s for s in reg.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR) if s.get_enabled()
        ]
        if not fontes:
            return 0

        def conectar(src) -> bool:
            try:
                self._client(src)
                return True
            except Exception:
                log.debug("could not connect to %s", src.get_display_name(), exc_info=True)
                return False

        with ThreadPoolExecutor(max_workers=len(fontes)) as pool:
            n = sum(pool.map(conectar, fontes))
        log.info("agenda aquecida: %d de %d fonte(s)", n, len(fontes))
        return n

    # ── Reading ─────────────────────────────────────────────────────────────
    def events_between(self, start_at: datetime, end_at: datetime) -> list[Event]:
        """Events in the interval, with recurrences **expanded**.

        It uses `generate_instances_sync`, not `get_object_list_as_comps_sync`:
        the latter returns the *master* component of a recurring event, whose
        DTSTART is the start of the series. A "Daily" created months ago would
        show up with its creation date, and several occurrences would collapse
        into one. That is exactly what turned up in the first test against a real
        calendar.
        """
        reg = self._load()
        if reg is None:
            return []
        from gi.repository import EDataServer

        eventos: list[Event] = []
        for src in reg.list_sources(EDataServer.SOURCE_EXTENSION_CALENDAR):
            if not src.get_enabled():
                continue
            try:
                client = self._client(src)

                def coletar(icomp, inst_start, inst_end, _data, _cancellable, src=src):
                    ev = _instancia(icomp, inst_start, inst_end, src)
                    if ev is not None:
                        eventos.append(ev)
                    return True   # True = continuar gerando

                client.generate_instances_sync(
                    int(start_at.timestamp()), int(end_at.timestamp()), None, coletar, None
                )
            except Exception:
                # One failing calendar must not hide the others.
                log.debug("falha ao ler a agenda %s", src.get_display_name(), exc_info=True)

        eventos.sort(key=lambda e: (e.start, e.summary))
        return eventos

    def today(self, dia: date | None = None) -> list[Event]:
        dia = dia or date.today()
        return self.events_between(
            datetime.combine(dia, time.min), datetime.combine(dia, time.max)
        )

    def now(self, at: datetime | None = None) -> Event | None:
        """The appointment in progress, if any. It is ADR 0008's enrichment."""
        at = at or datetime.now()
        for e in self.today(at.date()):
            if not e.all_day and e.start <= at <= e.end:
                return e
        return None

    # ── Writing ─────────────────────────────────────────────────────────────
    def create_event(
        self, source_uid: str, summary: str, start: datetime, end: datetime
    ) -> str | None:
        """Create an event in the given calendar.

        ADR 0007's invariant: **it never adds guests.** There is no parameter for
        it, and it must not gain one.
        """
        reg = self._load()
        if reg is None:
            return None
        from gi.repository import ECal, ICalGLib

        src = reg.ref_source(source_uid)
        if src is None:
            log.error("calendar %s does not exist", source_uid)
            return None

        client = self._client(src)
        comp = ICalGLib.Component.new_vevent()
        comp.set_summary(summary)
        comp.set_dtstart(_ical_time(start))
        comp.set_dtend(_ical_time(end))
        ok, uid = client.create_object_sync(comp, ECal.OperationFlags.NONE, None)
        return uid if ok else None


# ── Conversions ─────────────────────────────────────────────────────────────
def _ical(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _ical_time(dt: datetime):
    from gi.repository import ICalGLib

    t = ICalGLib.Time.new_null_time()
    t.set_date(dt.year, dt.month, dt.day)
    t.set_time(dt.hour, dt.minute, 0)
    t.set_is_date(False)
    return t


def _instancia(icomp, inst_start, inst_end, src) -> Event | None:
    """A concrete occurrence. The times come from the instance, not the master."""
    try:
        dtstart = icomp.get_dtstart()
        return Event(
            uid=icomp.get_uid() or "",
            summary=icomp.get_summary() or "(untitled)",
            start=_from_ical(inst_start),
            end=_from_ical(inst_end) if inst_end is not None else _from_ical(inst_start),
            all_day=bool(dtstart is not None and dtstart.is_date()),
            calendar=src.get_display_name(),
            source_uid=src.get_uid(),
        )
    except Exception:
        log.debug("unreadable calendar component", exc_info=True)
        return None


def _from_ical(t) -> datetime:
    return datetime(
        t.get_year(), t.get_month(), t.get_day(),
        max(0, t.get_hour()), max(0, t.get_minute()), max(0, t.get_second()),
    )
