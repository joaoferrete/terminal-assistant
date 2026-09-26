"""What works on this machine, and what to do about what does not.

It is the first question of whoever installs it: of the six integrations, which
are alive here? Before this, the answer was scattered — a `shutil.which` in
`notify.py`, another in `lighter.py`, another in `mic.py`, a `Calendar.error` the
`/health` route computed and threw away, and a `Home.configured` — and it only
reached the user through `journalctl`, after the daemon came up.

Here they become one list, and that list feeds **four** surfaces: `ta doctor`,
`/health`, the grouping in `ta --help`, and the runtime error messages. One place
decides what is alive, so they cannot disagree — the same way `STATUS_MARK` is one
place for the CLI and the export.

Nothing here needs the daemon running. That is on purpose: whoever most needs the
diagnosis is precisely the person whose daemon would not start.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from .config import Config, config_file
from .i18n import lang, lang_source, t


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    ok: bool
    # Why it is unavailable, and the exact step to fix it. Empty when ok.
    # `fix` is what separates a useful diagnosis from one that merely confirms
    # the problem.
    reason: str = ""
    fix: str = ""
    # `essential` marks what the project canNOT lose. Only notes are: they run
    # anywhere Python runs, and that is what the positioning promises.
    essential: bool = False
    # The commands that die with it. It is what `--help` uses to mark the group.
    commands: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        """The first sentence of the reason, to fit on one `--help` line.

        The full reason can be long — the calendar's carries the whole GLib error
        — and that is right in `ta doctor`, which has the screen to explain, and
        wrong in a command list, where it pushes everything off the edge.
        """
        return self.reason.split(". ")[0] if self.reason else ""


def _notes(cfg: Config) -> Capability:
    from .db import default_db_path

    path = default_db_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        writable = True
    except OSError:
        writable = False
    return Capability(
        key="notes",
        label=t("cap.notes"),
        ok=writable,
        reason="" if writable else f"cannot write to {path.parent}",
        fix="" if writable else f"check the permissions on {path.parent}",
        essential=True,
        commands=("note", "list", "done", "rm", "restore", "board", "export", "today"),
    )


def _calendar(_: Config) -> Capability:
    from .sensors.calendar import Calendar

    cal = Calendar()
    # `available` triggers the `gi` import and stores the reason — which already
    # came with the fix embedded and was thrown away by `/health`, which exposed
    # only the boolean.
    ok = cal.available
    reason = "" if ok else (cal.error or "Evolution Data Server is out of reach")

    # The fix depends on WHICH failure it was. Telling somebody to install
    # typelibs when they already have them and only lack a bus is worse than
    # suggesting nothing: they run the `apt`, nothing changes, and they start
    # distrusting the whole diagnosis.
    if ok:
        fix = ""
    elif "D-Bus" in reason or "DISPLAY" in reason:
        fix = "open it in a graphical session; without one the calendar cannot be read"
    else:
        fix = "sudo apt install gir1.2-ecal-2.0 gir1.2-edataserver-1.2"

    return Capability(
        key="calendar",
        label=t("cap.calendar"),
        ok=ok,
        reason=reason,
        fix=fix,
        commands=("today", "event"),
    )


def _mic(_: Config) -> Capability:
    ok = shutil.which("pw-dump") is not None
    return Capability(
        key="mic",
        label=t("cap.mic"),
        ok=ok,
        reason="" if ok else "`pw-dump` not found: the meeting trigger stays inert",
        fix="" if ok else "sudo apt install pipewire-utils",
        commands=(),
    )


def _home(cfg: Config) -> Capability:
    ok = bool(cfg.ha_token)
    return Capability(
        key="home",
        label=t("cap.home"),
        ok=ok,
        reason="" if ok else "HA_TOKEN is not set",
        fix="" if ok else "create a long-lived token in Home Assistant and put HA_TOKEN in .env",
        commands=("on", "off", "luz", "light", "entities", "temp", "router", "media"),
    )


def _lighter(_: Config) -> Capability:
    from .actuators.lighter import Lighter

    lit = Lighter()
    ok = lit.available
    has_gsettings = shutil.which("gsettings") is not None
    return Capability(
        key="lighter",
        label=t("cap.lighter"),
        ok=ok,
        reason=(
            ""
            if ok
            else ("`gsettings` not found" if not has_gsettings
                  else "the GNOME extension is not installed")
        ),
        fix="" if ok else "https://github.com/joaoferrete/Lighter",
        commands=("lighter",),
    )


def _ai(cfg: Config) -> Capability:
    # Either provider is enough: the routing skips whichever has no key (ADR 0018).
    ok = bool(cfg.deepseek_api_key or cfg.gemini_api_key)
    return Capability(
        key="ai",
        label=t("cap.ai"),
        ok=ok,
        reason="" if ok else "neither DEEPSEEK_API_KEY nor GEMINI_API_KEY is set",
        # The sentence says it is optional on purpose: without that, a red line
        # in the table reads as a broken install — and it is not. Almost
        # everything works without it.
        fix="" if ok else "optional. To enable it: DEEPSEEK_API_KEY or GEMINI_API_KEY in .env",
        commands=("init", "priorities", "organize", "prose", "revise", "event"),
    )


def _telegram(cfg: Config) -> Capability:
    from .config import telegram_owner

    has_token, owner = bool(cfg.telegram_token), telegram_owner()
    ok = has_token and bool(owner)
    if not has_token:
        reason = "TELEGRAM_BOT_TOKEN is not set"
        fix = "optional. Create a bot with @BotFather and put TELEGRAM_BOT_TOKEN in .env"
    elif not owner:
        # A token with no owner would poll and answer nobody: say which half is missing.
        reason = "no owner in config.toml, so the bot would answer nobody"
        fix = 'add [channel.telegram] owner = "your_username" to config.toml'
    else:
        reason = fix = ""
    return Capability(key="telegram", label=t("cap.telegram"), ok=ok, reason=reason, fix=fix)


def _voice(_: Config) -> Capability:
    from .speech import available

    ok = available()
    return Capability(
        key="voice",
        label=t("cap.voice"),
        ok=ok,
        reason="" if ok else "faster-whisper is not installed",
        fix="" if ok else 'optional. To transcribe voice notes: pip install -e ".[voice]"',
    )


def _gcal(cfg: Config) -> Capability:
    # The OAuth client only: which Members connected an account is theirs to do
    # from the chat, and not a property of this machine.
    ok = bool(cfg.google_client_id and cfg.google_client_secret)
    return Capability(
        key="gcal",
        label=t("cap.gcal"),
        ok=ok,
        reason="" if ok else "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set",
        fix="" if ok else "optional. Create an OAuth client in Google Cloud (docs/calendar.md)",
    )


PROBES = (_notes, _calendar, _mic, _home, _lighter, _ai, _telegram, _voice, _gcal)


def inspect(cfg: Config | None = None) -> list[Capability]:
    """The six capabilities, in the order that makes sense to a reader.

    `notes` first because it is the only essential one; the rest is what you add.

    A probe that blows up becomes one failure line and does not take the other
    five with it: a diagnosis that dies at the first problem is useless precisely
    on the machine with a problem. The calendar already proved that — with no
    graphical session it raised a GLib `GError` and took the whole `ta doctor`
    with it.
    """
    cfg = cfg or Config.from_env()
    out = []
    for probe in PROBES:
        try:
            out.append(probe(cfg))
        except Exception as e:  # noqa: BLE001 — a diagnosis must not die
            name = probe.__name__.lstrip("_")
            out.append(
                Capability(
                    key=name,
                    label=name.capitalize(),
                    ok=False,
                    reason=f"the probe failed: {e}",
                    fix="this is a Terminal Assistant bug; please report it",
                )
            )
    return out


def by_command(caps: list[Capability]) -> dict[str, Capability]:
    """From a CLI command to the capability it requires. For `--help`."""
    return {cmd: cap for cap in caps for cmd in cap.commands}


def environment(cfg: Config | None = None) -> list[tuple[str, str]]:
    """Context that is not a capability, but is the second thing people ask."""
    cfg = cfg or Config.from_env()
    return [
        (t("doctor.language"), f"{lang()} ({t('doctor.from')}: {lang_source()})"),
        (
            t("doctor.config"),
            str(config_file())
            + ("" if config_file().exists() else "  " + t("doctor.does_not_exist")),
        ),
        (
            t("doctor.daemon"),
            cfg.base_url + "  "
            + (t("doctor.exposed") if cfg.exposed else t("doctor.local_only")),
        ),
    ]
