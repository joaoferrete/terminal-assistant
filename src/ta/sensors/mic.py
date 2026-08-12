"""Microphone watcher, through PipeWire.

The meeting trigger is the microphone being in use, not the window title: the
session is Wayland and no process outside the compositor can read a window title
(ADR 0008). The signal is also universal — it works for Meet, Zoom, Slack and
Discord without knowing the difference between them.

The signal is the existence of any `Stream/Input/Audio` node in the PipeWire
graph. Validated in practice: outside a call, `pw-dump` lists none.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable

log = logging.getLogger("ta.mic")

# An app can open and close the stream in bursts when joining a call. The
# debounce stops the light flickering along with it.
DEBOUNCE_S = 2.0
POLL_S = 2.0

# Streams that do not mean "I am in a meeting". GNOME's own audio level monitor
# shows up as a Stream/Input/Audio.
IGNORED = ("gnome-shell", "gsd-media-keys", "pipewire-pulse-monitor")


def _input_streams(dump: list[dict]) -> list[str]:
    """Names of the apps with an active capture stream."""
    names = []
    for node in dump:
        info = node.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Input/Audio":
            continue
        name = props.get("application.name") or props.get("node.name") or "?"
        if any(ig in name.lower() for ig in IGNORED):
            continue
        names.append(name)
    return names


class MicWatcher:
    """Watches the microphone and calls back on transitions, in both directions."""

    def __init__(self, on_change: Callable[[bool, list[str]], Awaitable[None]]) -> None:
        self.on_change = on_change
        self._bin = shutil.which("pw-dump")
        self.active = False
        self.apps: list[str] = []
        self._pending: bool | None = None
        self._since: float = 0.0
        if self._bin is None:
            log.warning("pw-dump not found: the microphone trigger stays inert")

    @property
    def available(self) -> bool:
        return self._bin is not None

    async def _read(self) -> list[str] | None:
        """Read the graph. None means "could not read", which differs from "empty"."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            return _input_streams(json.loads(out))
        except (json.JSONDecodeError, OSError):
            log.exception("failed to read pw-dump")
            return None

    async def tick(self, now: float) -> None:
        """One cycle. Split from the loop so it is testable without sleeping."""
        apps = await self._read()
        if apps is None:
            return  # a failed read is not a transition: do not invent state

        active = bool(apps)
        if active == self.active:
            self._pending = None  # went back before the debounce expired
            return

        if self._pending != active:
            self._pending, self._since = active, now
            return

        if now - self._since >= DEBOUNCE_S:
            self.active, self.apps, self._pending = active, apps, None
            log.info("microphone %s (%s)", "on" if active else "off", ", ".join(apps) or "-")
            await self.on_change(active, apps)

    async def run(self) -> None:
        if not self.available:
            return
        loop = asyncio.get_running_loop()
        while True:
            try:
                await self.tick(loop.time())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("microphone tick failed; the watcher carries on")
            await asyncio.sleep(POLL_S)
