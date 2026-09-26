"""`ta satellite run`: the laptop side of a Satellite (F6, T6.3).

Three loops, all opened from here — the server never reaches in:

- the microphone watcher, reporting each change to the server, where the Owner's
  Rules run;
- a long poll for actions the server queued for this machine — the ring light,
  a desktop notification — executed locally;
- the capture queue, flushed every minute, so what was noted offline arrives.

It runs as a systemd **user** unit on the laptop: it needs the desktop session
for `gsettings` (the Lighter) and `notify-send`, which is exactly why it is not
on the server.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from . import outbox
from .actuators.lighter import Lighter
from .actuators.notify import Notifier
from .config import Config
from .sensors.mic import MicWatcher

log = logging.getLogger("ta.satellite")

POLL_WAIT = 25          # the server holds the poll this long when there is nothing
BACKOFF = (1, 2, 5, 15, 60)


class Satellite:
    def __init__(self, cfg: Config, *, lighter=None, notifier=None, transport=None,
                 sleep=asyncio.sleep) -> None:
        if not cfg.server:
            raise ValueError("TA_SERVER is not set")
        self.cfg = cfg
        self.lighter = lighter if lighter is not None else Lighter()
        self.notifier = notifier if notifier is not None else Notifier()
        self._sleep = sleep
        token = cfg.credential()
        self.http = httpx.AsyncClient(
            base_url=cfg.server, timeout=POLL_WAIT + 10, transport=transport,
            headers={"Authorization": f"Bearer {token}"} if token else {})

    async def on_mic(self, active: bool, apps: list[str]) -> None:
        try:
            r = await self.http.post("/satellite/signal", json={"mic": active, "apps": apps})
            r.raise_for_status()
        except httpx.HTTPError as e:
            # A meeting starting while the server is away cannot wait for it:
            # the signal is lost, and the next change is reported normally.
            log.warning("could not report the microphone: %s", type(e).__name__)

    async def act(self, action: dict) -> None:
        if action.get("kind") == "notify":
            await self.notifier.send(action.get("title", ""), action.get("body", ""),
                                     urgency=action.get("urgency", "normal"))
            return
        if action.get("kind") != "lighter":
            log.info("unknown action %r ignored", action.get("kind"))
            return
        op = action.get("op")
        if op == "enable":
            await self.lighter.enable(bool(action.get("on", True)))
        elif op == "toggle":
            await self.lighter.toggle()
        elif op == "apply_profile":
            await self.lighter.apply_profile(action.get("name", ""),
                                             enable=bool(action.get("enable", True)))
        elif op == "set":
            await self.lighter.set(action["key"], action["value"])

    async def poll_once(self) -> int:
        r = await self.http.get("/satellite/actions", params={"wait": POLL_WAIT})
        r.raise_for_status()
        actions = r.json().get("actions", [])
        for action in actions:
            try:
                await self.act(action)
            except Exception:
                log.exception("action %r failed", action)
        return len(actions)

    async def poll_forever(self) -> None:
        failures = 0
        while True:
            try:
                await self.poll_once()
                failures = 0
            except httpx.HTTPError as e:
                wait = BACKOFF[min(failures, len(BACKOFF) - 1)]
                failures += 1
                log.warning("server unreachable (%s); retrying in %ss", type(e).__name__, wait)
                await self._sleep(wait)

    async def flush_forever(self, every: float = 60.0) -> None:
        while True:
            try:
                sent, _ = await asyncio.to_thread(outbox.flush, self._send_sync)
                if sent:
                    log.info("sent %d queued note(s)", sent)
            except Exception:
                log.exception("flushing the queue failed")
            await self._sleep(every)

    def _send_sync(self, item: dict) -> None:
        token = self.cfg.credential()
        r = httpx.post(f"{self.cfg.server}/notes", json=item, timeout=10,
                       headers={"Authorization": f"Bearer {token}"} if token else {})
        r.raise_for_status()

    async def run(self) -> None:
        mic = MicWatcher(self.on_mic)
        # The Lighter extension has its own window watcher; the daemon took it
        # over while in charge, and a Satellite in charge of the desk does too.
        await self.lighter.take_over()
        try:
            await asyncio.gather(mic.run(), self.poll_forever(), self.flush_forever())
        finally:
            await self.lighter.hand_back()
            await self.http.aclose()
