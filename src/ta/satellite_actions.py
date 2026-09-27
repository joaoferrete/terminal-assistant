"""The Satellite's catalogue of actions, on the laptop (F10, D43, D56).

What the agent may do on a Member's computer is decided here, by this computer:
each action checks the tool it needs exists, and `offer()` lists only what works on
this machine. There is no shell — every command is an argument list with a
timeout — and no free arguments reach one: a volume is a number, an app is a
desktop id found in the application folders, a URL must be http(s), a script is a
name from this laptop's own `[satellite.scripts]`.

A screenshot, suspend, and a script not marked `safe` need the Member's button.
The server asks first; the laptop refuses any of them that arrives unconfirmed,
so a server bug cannot skip the button either.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TIMEOUT = 10
NEEDS_CONFIRMATION = ("screenshot", "suspend")


def _run(*args: str, timeout: float = TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, timeout=timeout,
                          check=False)


def _has(tool: str) -> bool:
    return shutil.which(tool) is not None


# ── The actions ─────────────────────────────────────────────────────────────
def lock(_: str = "") -> str:
    r = _run("loginctl", "lock-session")
    return "locked" if r.returncode == 0 else f"could not lock ({r.stderr.strip()[:80]})"


def volume(value: str) -> str:
    """"up", "down", "mute" (toggle), or a level 0–100."""
    v = value.strip().lower().rstrip("%")
    sink = "@DEFAULT_AUDIO_SINK@"
    if v == "mute":
        r = _run("wpctl", "set-mute", sink, "toggle")
    elif v in ("up", "down"):
        r = _run("wpctl", "set-volume", "-l", "1.0", sink, "10%+" if v == "up" else "10%-")
    elif v.isdigit() and 0 <= int(v) <= 100:
        r = _run("wpctl", "set-volume", sink, f"{int(v)}%")
    else:
        return "volume is up, down, mute, or 0-100"
    return f"volume {v}" if r.returncode == 0 else "could not change the volume"


def _players() -> list[str]:
    r = _run("gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path",
             "/org/freedesktop/DBus", "--method", "org.freedesktop.DBus.ListNames")
    return re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", r.stdout)


def media(value: str) -> str:
    """play/pause (toggle), next, previous, on whatever player is open (MPRIS)."""
    method = {"play": "PlayPause", "pause": "PlayPause", "toggle": "PlayPause",
              "next": "Next", "previous": "Previous"}.get(value.strip().lower())
    if method is None:
        return "media is play, pause, next or previous"
    players = _players()
    if not players:
        return "no music or video player is open"
    r = _run("gdbus", "call", "--session", "--dest", players[0], "--object-path",
             "/org/mpris/MediaPlayer2", "--method", f"org.mpris.MediaPlayer2.Player.{method}")
    return f"{value} on {players[0].rsplit('.', 1)[-1]}" if r.returncode == 0 else \
        "the player did not accept it"


def open_(value: str) -> str:
    """A web address, or an installed app by its desktop id ("firefox")."""
    target = value.strip()
    if re.match(r"https?://\S+$", target):
        r = _run("xdg-open", target)
        return f"opened {target}" if r.returncode == 0 else "could not open it"
    app = re.sub(r"\.desktop$", "", target)
    if not re.fullmatch(r"[\w.-]+", app) or not _desktop_file(app):
        return f"no installed app called {target!r}"
    subprocess.Popen(["gtk-launch", app], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return f"opened {app}"


def _desktop_file(app: str) -> Path | None:
    dirs = [Path.home() / ".local/share/applications", Path("/usr/share/applications"),
            Path("/var/lib/snapd/desktop/applications"),
            Path("/var/lib/flatpak/exports/share/applications")]
    return next((d / f"{app}.desktop" for d in dirs if (d / f"{app}.desktop").exists()), None)


def screenshot(_: str = "") -> dict | str:
    path = Path(tempfile.mkdtemp()) / "screen.png"
    for cmd in (["gnome-screenshot", "-f", str(path)], ["grim", str(path)]):
        if _has(cmd[0]) and _run(*cmd).returncode == 0 and path.exists():
            data = base64.b64encode(path.read_bytes()).decode()
            path.unlink()
            return {"name": "screen.png", "data": data}
    return "no screenshot tool here (install gnome-screenshot)"


def suspend(_: str = "") -> str:
    # Detached: the machine goes to sleep while this very process is answering.
    subprocess.Popen(["systemctl", "suspend"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    return "suspending"


def script(name: str, scripts: dict) -> str:
    entry = scripts.get(name)
    if entry is None:
        return f"no script called {name!r} on this computer"
    run = os.path.expanduser(str(entry.get("run", "")))
    if not run or not Path(run).is_file() or not os.access(run, os.X_OK):
        return f"script {name}: {run!r} is not an executable file"
    try:
        r = _run(run, timeout=float(entry.get("timeout", 120)))
    except subprocess.TimeoutExpired:
        return f"script {name} took too long and was stopped"
    tail = (r.stdout or r.stderr).strip()[-300:]
    return f"script {name} finished ({'ok' if r.returncode == 0 else f'exit {r.returncode}'})" + \
        (f": {tail}" if tail else "")


ACTIONS = {"lock": (lock, "loginctl"), "volume": (volume, "wpctl"), "media": (media, "gdbus"),
           "open": (open_, "xdg-open"), "screenshot": (screenshot, None),
           "suspend": (suspend, "systemctl")}


def offer(scripts: dict) -> dict:
    """What this computer can do, for the agent to choose from."""
    available = [name for name, (_, tool) in ACTIONS.items() if tool is None or _has(tool)]
    if "screenshot" in available and not (_has("gnome-screenshot") or _has("grim")):
        available.remove("screenshot")
    return {"actions": available,
            "scripts": {n: {"description": str(e.get("description", "")),
                            "safe": bool(e.get("safe", False))} for n, e in scripts.items()}}


def handle(request: dict, scripts: dict) -> dict:
    """One request from the server → its answer. Never raises."""
    op, value = str(request.get("op", "")), str(request.get("value", ""))
    confirmed = bool(request.get("confirmed"))
    try:
        if op == "offer":
            return offer(scripts)
        if op == "script":
            entry = scripts.get(value)
            if entry is not None and not entry.get("safe") and not confirmed:
                return {"error": "this script needs the member's confirmation"}
            return {"text": script(value, scripts)}
        if op not in ACTIONS:
            return {"error": f"unknown action {op!r}"}
        if op in NEEDS_CONFIRMATION and not confirmed:
            return {"error": f"{op} needs the member's confirmation"}
        out = ACTIONS[op][0](value)
        return out if isinstance(out, dict) else {"text": out}
    except subprocess.TimeoutExpired:
        return {"error": f"{op} took too long"}
    except OSError as e:
        return {"error": f"{op} failed ({type(e).__name__})"}
