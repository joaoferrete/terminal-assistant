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

The cookie is not the token. It is `v1.<issued>.<hmac>`, signed with `TA_TOKEN`:
it survives a daemon restart (nothing to remember server-side), it expires, and
rotating `TA_TOKEN` revokes every session at once. F6 replaces it with a
per-Member session without changing the flow.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import socket
from datetime import datetime, timedelta

CODE_TTL = timedelta(minutes=5)
SESSION_TTL = timedelta(days=90)
COOKIE = "ta_session"


class BoardCodes:
    """Codes waiting to be redeemed. In memory on purpose: they live five
    minutes, so a restart losing them costs one more `/board`."""

    def __init__(self) -> None:
        self._codes: dict[str, datetime] = {}

    def issue(self, now: datetime | None = None) -> str:
        now = now or datetime.now()
        # Drop the expired ones here, so the dict cannot grow without bound.
        self._codes = {c: exp for c, exp in self._codes.items() if exp > now}
        code = secrets.token_urlsafe(16)
        self._codes[code] = now + CODE_TTL
        return code

    def redeem(self, code: str, now: datetime | None = None) -> bool:
        """True once per valid code. `pop` is what makes the second use fail."""
        expires = self._codes.pop(code, None)
        return expires is not None and expires > (now or datetime.now())


def _mac(token: str, issued: int) -> str:
    return hmac.new(token.encode(), f"session:{issued}".encode(), hashlib.sha256).hexdigest()


def session_cookie(token: str, now: datetime | None = None) -> str:
    issued = int((now or datetime.now()).timestamp())
    return f"v1.{issued}.{_mac(token, issued)}"


def session_valid(token: str, value: str, now: datetime | None = None) -> bool:
    try:
        version, issued_s, mac = value.split(".")
        issued = int(issued_s)
    except ValueError:
        return False
    if version != "v1" or not hmac.compare_digest(mac, _mac(token, issued)):
        return False
    age = (now or datetime.now()).timestamp() - issued
    return 0 <= age <= SESSION_TTL.total_seconds()


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
