"""Satellites: a Member's own machine, seen from the server (F6, ADR 0015).

A Satellite reports what only it can sense — the microphone — and does what only
it can do — the Lighter, desktop notifications. The server never reaches into it:
the Satellite opens the connection, by long polling, the same shape as the
Channel's. So the laptop needs no open port, and can be asleep, off, or elsewhere.

On the server, the local Lighter and notifier are unavailable (no GNOME session),
so Rules get a `RemoteLighter` and a `RemoteNotifier` instead: the same interface,
whose calls become actions queued for the Owner's Satellite. The meeting Rule runs
on the server again, unchanged: the microphone signal travels in, the ring light
action travels back out.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict

# A Satellite that has not polled for this long is taken as gone: its actions
# would wait for a machine that is off, so the remote actuators report
# unavailable instead of pretending the ring light turned on.
SEEN_FOR = 90.0
# One queue per Member, bounded: a laptop off for a week must not come back to a
# thousand ring-light changes. Only the latest matter; the oldest are dropped.
MAX_QUEUED = 50


class Hub:
    def __init__(self) -> None:
        self._queues: dict[int, asyncio.Queue] = defaultdict(
            lambda: asyncio.Queue(maxsize=MAX_QUEUED))
        self._seen: dict[int, float] = {}

    def seen(self, member_id: int) -> None:
        self._seen[member_id] = time.monotonic()

    def connected(self, member_id: int) -> bool:
        return time.monotonic() - self._seen.get(member_id, -SEEN_FOR * 2) < SEEN_FOR

    def push(self, member_id: int, action: dict) -> None:
        q = self._queues[member_id]
        if q.full():
            q.get_nowait()        # drop the oldest
        q.put_nowait(action)

    async def next(self, member_id: int, wait: float) -> list[dict]:
        """Everything queued for the Member, waiting up to `wait` for the first."""
        self.seen(member_id)
        q = self._queues[member_id]
        try:
            first = await asyncio.wait_for(q.get(), timeout=wait)
        except TimeoutError:
            return []
        actions = [first]
        while not q.empty():
            actions.append(q.get_nowait())
        self.seen(member_id)
        return actions


class RemoteLighter:
    """The Lighter's interface, sent to a Member's Satellite."""

    def __init__(self, hub: Hub, member_id: int) -> None:
        self.hub, self.member_id = hub, member_id

    @property
    def available(self) -> bool:
        return self.hub.connected(self.member_id)

    def _send(self, **action) -> bool:
        if not self.available:
            return False
        self.hub.push(self.member_id, {"kind": "lighter", **action})
        return True

    async def get(self, key: str) -> str | None:
        return None       # a remote read would need a round trip Rules do not wait for

    async def set(self, key: str, value: str) -> bool:
        return self._send(op="set", key=key, value=value)

    async def enable(self, on: bool = True) -> bool:
        return self._send(op="enable", on=on)

    async def toggle(self) -> bool:
        return self._send(op="toggle")

    async def apply_profile(self, name: str, *, enable: bool = True) -> bool:
        return self._send(op="apply_profile", name=name, enable=enable)

    async def profiles(self) -> list[dict]:
        return []

    # The Satellite takes over and hands back its own extension around its own
    # run; the server has nothing of the desktop's to take over.
    async def take_over(self) -> None:
        pass

    async def hand_back(self) -> None:
        pass


class RemoteNotifier:
    def __init__(self, hub: Hub, member_id: int) -> None:
        self.hub, self.member_id = hub, member_id

    @property
    def available(self) -> bool:
        return self.hub.connected(self.member_id)

    async def send(self, title: str, body: str = "", *, urgency: str = "normal",
                   icon: str | None = None) -> bool:
        if not self.available:
            return False
        self.hub.push(self.member_id, {"kind": "notify", "title": title, "body": body,
                                       "urgency": urgency})
        return True
