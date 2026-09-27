"""The pushed Digest (D15, D35): one message a day, per Member, at their hour.

Made of independent sections. Each one returns its lines, or nothing — no data,
not configured, its source down — and a section that fails never takes the others
with it: a Digest without the weather still goes out. The facts are deterministic;
the model only writes an optional opening line, and a Digest without it is complete.

Sections, in order: calendar, notes (overdue and today), household Lists, weather,
and three for admins only — server health, AI spend, and AdGuard (D35).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx

from . import store, usage
from .i18n import t
from .members import Viewer

log = logging.getLogger("ta.digest")

SECTIONS = ("calendar", "notes", "lists", "weather", "server", "ai_cost", "adguard")
ADMIN_ONLY = frozenset({"server", "ai_cost", "adguard"})
DEFAULT_TIME = "07:00"
# A Digest missed because the daemon was down still goes out if it comes back
# within this window, and not after: an 07:00 summary at 19:00 is noise, and a
# daemon down for a week must not send seven (the scheduler's rule, ADR 0011).
LATE_WINDOW = timedelta(hours=3)
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"


# ── Settings, per Member ────────────────────────────────────────────────────
@dataclass
class Settings:
    enabled: bool = False
    time: str = DEFAULT_TIME
    sections: tuple[str, ...] = SECTIONS
    last: str | None = None          # the date it last went out

    @classmethod
    def load(cls, conn: sqlite3.Connection, member_id: int) -> Settings:
        row = conn.execute("SELECT digest FROM members WHERE id = ?", (member_id,)).fetchone()
        raw = json.loads(row[0]) if row and row[0] else {}
        sections = tuple(s for s in raw.get("sections", SECTIONS) if s in SECTIONS)
        return cls(enabled=bool(raw.get("enabled", False)),
                   time=raw.get("time", DEFAULT_TIME), sections=sections or SECTIONS,
                   last=raw.get("last"))

    def save(self, conn: sqlite3.Connection, member_id: int) -> None:
        conn.execute("UPDATE members SET digest = ? WHERE id = ?", (json.dumps({
            "enabled": self.enabled, "time": self.time, "sections": list(self.sections),
            "last": self.last}), member_id))


def parse_time(text: str) -> str | None:
    """"7", "7h", "07:30", "7:30" → "HH:MM", or None."""
    raw = text.strip().lower().replace("h", ":").rstrip(":")
    try:
        parts = [int(x) for x in raw.split(":")] if raw else []
    except ValueError:
        return None
    if len(parts) == 1:
        parts.append(0)
    if len(parts) != 2 or not (0 <= parts[0] < 24 and 0 <= parts[1] < 60):
        return None
    return f"{parts[0]:02d}:{parts[1]:02d}"


def is_due(settings: Settings, now: datetime) -> bool:
    """Send now? Once a day, at or after its hour, within the late window."""
    if not settings.enabled or settings.last == now.date().isoformat():
        return False
    hh, mm = (int(x) for x in settings.time.split(":"))
    at = datetime.combine(now.date(), time(hh, mm))
    return at <= now < at + LATE_WINDOW


# ── Building it ─────────────────────────────────────────────────────────────
@dataclass
class Sources:
    """What the sections read from. Injectable, so tests reach nothing real."""

    conn: sqlite3.Connection
    calendar_today: Callable[[], Awaitable[list[dict]]] | None = None
    weather: dict | None = None               # {"latitude", "longitude", "place"}
    adguard: dict | None = None               # {"url", "user", "password"}
    prices: dict = field(default_factory=dict)
    transport: httpx.AsyncBaseTransport | None = None
    proc: Path = Path("/proc")
    thermal: Path = Path("/sys/class/thermal")
    disk: str = "/"


async def build(src: Sources, member_id: int, *, is_admin: bool,
                sections: tuple[str, ...] = SECTIONS, today: date | None = None,
                opening: str = "") -> str:
    today = today or date.today()
    parts = [t("digest.title", day=today.strftime("%d/%m"))]
    if opening:
        parts.append(opening)
    for name in sections:
        if name in ADMIN_ONLY and not is_admin:
            continue
        try:
            lines = await _SECTIONS[name](src, member_id, today)
        except Exception as e:   # one source down must not cost the others
            log.warning("digest section %s failed: %s", name, e)
            continue
        if lines:
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


async def _calendar(src: Sources, member_id: int, today: date) -> list[str]:
    if src.calendar_today is None:
        return []
    events = await src.calendar_today()
    if not events:
        return []
    return [t("digest.calendar")] + [f"• {e['start'][11:16]} {e['summary']}" for e in events]


async def _notes(src: Sources, member_id: int, today: date) -> list[str]:
    tasks = store.by_urgency(store.due_today(src.conn, viewer=Viewer(member_id), today=today),
                             today=today)
    if not tasks:
        return [t("digest.notes_none")]
    shown = [f"• #{n.id} {n.text}" + (f" ({t('digest.overdue')})"
                                      if store.horizon(n.due, today=today) == "overdue" else "")
             for n in tasks[:6]]
    more = [t("digest.more", n=len(tasks) - 6)] if len(tasks) > 6 else []
    return [t("digest.notes", n=len(tasks))] + shown + more


async def _lists(src: Sources, member_id: int, today: date) -> list[str]:
    lists = [lv for lv in store.lists_for(src.conn, viewer=Viewer(member_id))
             if lv.scope == "household" and lv.items]
    if not lists:
        return []
    return [t("digest.lists")] + [t("digest.list_line", name=lv.name, n=len(lv.items))
                                  for lv in lists]


# The WMO weather codes Open-Meteo returns, folded into a handful of words.
_WEATHER = [(0, "clear"), (3, "cloudy"), (48, "fog"), (57, "drizzle"), (67, "rain"),
            (77, "snow"), (82, "rain"), (86, "snow"), (99, "storm")]


def _weather_word(code: int) -> str:
    return next((w for top, w in _WEATHER if code <= top), "cloudy")


async def _weather(src: Sources, member_id: int, today: date) -> list[str]:
    if not src.weather:
        return []
    async with httpx.AsyncClient(timeout=10, transport=src.transport) as client:
        r = await client.get(OPEN_METEO, params={
            "latitude": src.weather["latitude"], "longitude": src.weather["longitude"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                     "precipitation_probability_max",
            "timezone": "auto", "forecast_days": 1,
        })
        r.raise_for_status()
    d = r.json()["daily"]
    return [t("digest.weather", place=src.weather.get("place", ""),
              sky=t(f"digest.sky.{_weather_word(int(d['weather_code'][0]))}"),
              low=round(d["temperature_2m_min"][0]), high=round(d["temperature_2m_max"][0]),
              rain=d["precipitation_probability_max"][0] or 0)]


async def _server(src: Sources, member_id: int, today: date) -> list[str]:
    load = (src.proc / "loadavg").read_text().split()[0]
    mem = dict(line.split(":", 1) for line in (src.proc / "meminfo").read_text().splitlines()
               if ":" in line)
    total = int(mem["MemTotal"].split()[0])
    available = int(mem["MemAvailable"].split()[0])
    disk = shutil.disk_usage(src.disk)
    uptime_days = float((src.proc / "uptime").read_text().split()[0]) / 86400
    temps = [int(p.read_text()) / 1000 for p in sorted(src.thermal.glob("thermal_zone*/temp"))
             if p.read_text().strip().lstrip("-").isdigit()]
    line = t("digest.server", load=load, mem=round(100 * (1 - available / total)),
             disk=round(100 * disk.used / disk.total), days=round(uptime_days))
    if temps:
        line += t("digest.server_temp", temp=round(max(temps)))
    return [t("digest.server_title"), line]


async def _ai_cost(src: Sources, member_id: int, today: date) -> list[str]:
    start = datetime.combine(today, time())
    yesterday = usage.spent_total(src.conn, start - timedelta(days=1), src.prices) - \
        usage.spent_total(src.conn, start, src.prices)
    month = usage.spent_total(src.conn, start.replace(day=1), src.prices)
    return [t("digest.ai_cost", yesterday=f"{yesterday:.3f}", month=f"{month:.2f}")]


async def _adguard(src: Sources, member_id: int, today: date) -> list[str]:
    if not src.adguard or not src.adguard.get("url"):
        return []
    auth = (src.adguard["user"], src.adguard["password"]) if src.adguard.get("user") else None
    async with httpx.AsyncClient(timeout=10, transport=src.transport, auth=auth) as client:
        r = await client.get(f"{src.adguard['url'].rstrip('/')}/control/stats")
        r.raise_for_status()
    s = r.json()
    queries, blocked = s.get("num_dns_queries", 0), s.get("num_blocked_filtering", 0)

    def top(key: str) -> str:
        # AdGuard answers these as a list of one-entry objects: [{"name": count}].
        return ", ".join(next(iter(x)) for x in (s.get(key) or [])[:3]) or "—"

    return [t("digest.adguard_title"),
            t("digest.adguard", queries=queries, blocked=blocked,
              pct=round(100 * blocked / queries) if queries else 0),
            t("digest.adguard_top", domains=top("top_blocked_domains"),
              clients=top("top_clients"))]


_SECTIONS = {"calendar": _calendar, "notes": _notes, "lists": _lists, "weather": _weather,
             "server": _server, "ai_cost": _ai_cost, "adguard": _adguard}


def adguard_from_env() -> dict | None:
    url = os.environ.get("ADGUARD_URL")
    if not url:
        return None
    return {"url": url, "user": os.environ.get("ADGUARD_USER", ""),
            "password": os.environ.get("ADGUARD_PASSWORD", "")}
