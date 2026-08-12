"""Actuator for the Lighter extension, through gsettings.

[Lighter](https://github.com/joaoferrete/Lighter) is by the same author. The
daemon decides *what* — which profile to apply — and the extension decides *how*,
because the edge calibration is tuned to the webcam's position and must not be
duplicated here (ADR 0002).

Verified in its code: `extension.js` connects a generic `changed` handler over
the appearance keys, and GSettings notifies external writes — so `gsettings set`
from outside works live, without altering the extension. The only change needed
was a `changed::active-profile` listener, so that applying a profile *by name*
became possible from outside.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger("ta.lighter")

SCHEMA = "org.gnome.shell.extensions.lighter"
UUID = "lighter@gnome-shell-extensions.ferrete.com"

# A GNOME extension's schema is NOT on gsettings' default search path — it lives
# inside the extension's own directory. Without `--schemadir` every command fails
# with "No such schema org.gnome.shell.extensions.lighter". Found the hard way;
# the path is resolved at runtime, never hardcoded.
SCHEMA_DIRS = (
    Path.home() / ".local/share/gnome-shell/extensions" / UUID / "schemas",
    Path("/usr/share/gnome-shell/extensions") / UUID / "schemas",
)


def _schemadir() -> Path | None:
    for d in SCHEMA_DIRS:
        if (d / "gschemas.compiled").is_file():
            return d
    return None


class Lighter:
    def __init__(self) -> None:
        self._bin = shutil.which("gsettings")
        self._dir = _schemadir()
        if self._bin is None:
            log.warning("gsettings not found: Lighter is out of reach")
        elif self._dir is None:
            log.warning("Lighter schema not found; is the extension installed?")

    @property
    def available(self) -> bool:
        return self._bin is not None and self._dir is not None

    async def _run(self, *args: str) -> str | None:
        if not self.available:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin, "--schemadir", str(self._dir), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, err = await proc.communicate()
            if proc.returncode != 0:
                log.error("gsettings %s failed: %s", " ".join(args), err.decode().strip())
                return None
            return out.decode().strip()
        except Exception:
            log.exception("gsettings failed")
            return None

    async def get(self, key: str) -> str | None:
        raw = await self._run("get", SCHEMA, key)
        # gsettings returns a string wrapped in single quotes; the caller wants the value.
        if raw and len(raw) >= 2 and raw[0] == "'" and raw[-1] == "'":
            return raw[1:-1]
        return raw

    async def set(self, key: str, value: str) -> bool:
        return await self._run("set", SCHEMA, key, value) is not None

    # ── State ───────────────────────────────────────────────────────────────
    async def enable(self, on: bool = True) -> bool:
        return await self.set("enabled", "true" if on else "false")

    async def toggle(self) -> bool:
        current = await self.get("enabled")
        return await self.enable(current != "true")

    async def profiles(self) -> list[dict]:
        """The profiles stored in the extension. The daemon does not keep them."""
        raw = await self.get("profiles")   # `get` already stripped the quotes
        if not raw:
            return []
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            log.error("Lighter profiles JSON is unreadable")
            return []
        return doc.get("profiles", doc) if isinstance(doc, dict) else doc

    async def profile_id(self, name: str) -> str | None:
        """Resolve a profile by *name*, not by uid.

        Hardcoding the uid would be the same mistake we avoid with the calendars:
        it changes if the profile is recreated.
        """
        for p in await self.profiles():
            if p.get("name", "").lower() == name.lower():
                return p.get("id")
        return None

    async def apply_profile(self, name: str, *, enable: bool = True) -> bool:
        """Apply a profile by name and turn the edge on.

        It depends on the `changed::active-profile` listener added to the
        extension. Without it, writing the key applies nothing — and the silence
        is the worst part: there is no error, the edge just does not change.
        """
        pid = await self.profile_id(name)
        if pid is None:
            log.error("profile %r does not exist in Lighter", name)
            return False
        ok = await self.set("active-profile", pid)
        if ok and enable:
            ok = await self.enable(True)
        return ok

    # ── Guard against a race ────────────────────────────────────────────────
    async def take_over(self) -> None:
        """Turn the extension's `auto-switch` off while the daemon is in charge.

        Without it, its `WindowWatcher` would apply a profile on the next
        `notify::focus-window` and overwrite what the Rule just did. `auto-switch`
        already ships as `false` today, so this is a guarantee, not a fix.
        """
        if (await self.get("auto-switch")) == "true":
            log.info("turning Lighter auto-switch off while the daemon is up")
            await self.set("auto-switch", "false")

    async def hand_back(self) -> None:
        """Give autonomy back to the extension when the daemon leaves.

        Called on a clean shutdown. If the daemon is killed outright, the key
        stays `false` — and the extension is still controllable from its own UI.
        """
        await self.set("auto-switch", "true")
