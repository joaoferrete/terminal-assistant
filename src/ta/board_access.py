"""Getting the board onto a phone without typing, or leaking, `TA_TOKEN` (T1.7).

Two pieces, because there were two problems.

**A one-time code, sent by the bot.** `/board` in the Channel answers with
`/board?code=…`. The code works once and for five minutes. `TA_TOKEN` itself never
goes through the Channel: a chat is stored on the Channel's servers, and that
token controls the house.

**A session cookie, set when the code is redeemed.** The board used to keep the
token in `localStorage` and strip it from the address bar, which is right for the
history — and meant **every reload 401'd**, because loading a page cannot send a
header built by JavaScript. A cookie travels with the page load by itself.

The cookie is not the token. It is `v2.<member>.<issued>.<hmac>`, signed with
`TA_TOKEN`: it survives a daemon restart (nothing to remember server-side), it
expires, rotating `TA_TOKEN` revokes every session at once, and it says **which
Member** is looking — the board shows each person their own Notes (F3). A `v1`
cookie, from before Members, could only ever have been issued to the Owner, and
is still read as the Owner's rather than logging that phone out.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import socket
from datetime import datetime, timedelta

from .members import OWNER_ID

CODE_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=90)
COOKIE = "ta_session"


class BoardCodes:
    """Codes waiting to be redeemed, each for one Member. In memory on purpose:
    they live five minutes, so a restart losing them costs one more `/board`."""

    def __init__(self) -> None:
        self._codes: dict[str, tuple[datetime, int]] = {}

    def issue(self, member_id: int = OWNER_ID, now: datetime | None = None) -> str:
        now = now or datetime.now()
        # Drop the expired ones here, so the dict cannot grow without bound.
        self._codes = {c: v for c, v in self._codes.items() if v[0] > now}
        code = secrets.token_urlsafe(16)
        self._codes[code] = (now + CODE_TTL, member_id)
        return code

    def redeem(self, code: str, now: datetime | None = None) -> int | None:
        """The Member the code was issued to, once. `pop` makes reuse fail."""
        entry = self._codes.pop(code, None)
        if entry is None or entry[0] <= (now or datetime.now()):
            return None
        return entry[1]


def _mac(token: str, payload: str) -> str:
    return hmac.new(token.encode(), f"session:{payload}".encode(), hashlib.sha256).hexdigest()


def session_cookie(token: str, member_id: int = OWNER_ID, now: datetime | None = None) -> str:
    issued = int((now or datetime.now()).timestamp())
    return f"v2.{member_id}.{issued}.{_mac(token, f'{member_id}:{issued}')}"


def session_member(token: str, value: str, now: datetime | None = None) -> int | None:
    """The Member a valid cookie names, or None."""
    parts = value.split(".")
    try:
        if parts[0] == "v2" and len(parts) == 4:
            member_id, issued = int(parts[1]), int(parts[2])
            expected = _mac(token, f"{member_id}:{issued}")
        elif parts[0] == "v1" and len(parts) == 3:
            member_id, issued = OWNER_ID, int(parts[1])
            expected = _mac(token, str(issued))
        else:
            return None
    except ValueError:
        return None
    if not hmac.compare_digest(parts[-1], expected):
        return None
    age = (now or datetime.now()).timestamp() - issued
    return member_id if 0 <= age <= SESSION_TTL.total_seconds() else None


def session_valid(token: str, value: str, now: datetime | None = None) -> bool:
    return session_member(token, value, now) is not None


def lan_address() -> str | None:
    """This machine's address on the home network, for the link the bot sends.

    A UDP `connect` sends no packet; it only asks the kernel which interface
    would route outwards, which is the address a phone on the same Wi-Fi uses.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))   # TEST-NET-1: never routed, never answered
            return s.getsockname()[0]
    except OSError:
        return None
