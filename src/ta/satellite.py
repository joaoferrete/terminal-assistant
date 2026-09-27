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
import itertools
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
        self._machines: dict[tuple[int, str], tuple[float, float]] = {}
        # Questions waiting for a Satellite's answer (F9, D41): id → (member, future).
        self._pending: dict[str, tuple[int, asyncio.Future]] = {}
        self._ids = itertools.count(1)

    def seen(self, member_id: int, machine: str | None = None) -> None:
        self._seen[member_id] = time.monotonic()
        if machine:
            # Per computer, for `/satellites` (F10): a Member may pair two, and
            # the catalogue will need to say which one to act on.
            self._machines[(member_id, machine)] = (time.monotonic(), time.time())

    def machines(self, member_id: int | None = None) -> list[dict]:
        """The computers seen since the server started: whose, their name,
        whether they are connected now, and when they were last seen."""
        now = time.monotonic()
        return [{"member_id": m, "machine": name, "online": now - mono < SEEN_FOR,
                 "last_seen": wall}
                for (m, name), (mono, wall) in sorted(self._machines.items())
                if member_id is None or m == member_id]

    def connected(self, member_id: int) -> bool:
        return time.monotonic() - self._seen.get(member_id, -SEEN_FOR * 2) < SEEN_FOR

    def push(self, member_id: int, action: dict) -> None:
        q = self._queues[member_id]
        if q.full():
            q.get_nowait()        # drop the oldest
        q.put_nowait(action)

    async def next(self, member_id: int, wait: float, machine: str | None = None) -> list[dict]:
        """Everything queued for the Member, waiting up to `wait` for the first."""
        self.seen(member_id, machine)
        q = self._queues[member_id]
        try:
            first = await asyncio.wait_for(q.get(), timeout=wait)
        except TimeoutError:
            return []
        actions = [first]
        while not q.empty():
            actions.append(q.get_nowait())
        self.seen(member_id, machine)
        return actions


    async def ask(self, member_id: int, request: dict, timeout: float = 30.0) -> dict:
        """Send a question to the Member's Satellite and wait for its answer.

        The same long poll carries it out, and the answer comes back on
        `/satellite/answer`, so the laptop still opens every connection. Raises
        TimeoutError when nothing answers — the laptop went to sleep, or runs a
        version that does not know the question.
        """
        rid = f"q{next(self._ids)}"
        future = asyncio.get_running_loop().create_future()
        self._pending[rid] = (member_id, future)
        self.push(member_id, {**request, "id": rid})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(rid, None)

    def answer(self, member_id: int, rid: str, payload: dict) -> bool:
        """Deliver an answer. Only the Satellite the question went to may answer
        it: another Member's token guessing `q7` must not feed somebody else's
        agent."""
        waiting = self._pending.get(rid)
        if waiting is None or waiting[0] != member_id or waiting[1].done():
            return False
        waiting[1].set_result(payload)
        return True


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
