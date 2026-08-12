"""Home Assistant client.

Home Assistant is the only layer that knows about device protocols (ADR 0001).
This module speaks only `entity_id` and services — never Tuya, never Zigbee,
never an IP address.

**State by polling, not WebSocket.** The plan called for WS, and the swap was
deliberate: WS would need a new dependency and a reconnection state machine, and
the only consumer of pushed state would be the `entity_state` trigger, which is
not on the path of the rule that motivated the project (that one uses the
microphone and the clock). Polling every few seconds in a house with three
appliances costs nothing and has no state to resynchronise when the network
wobbles. If a trigger ever needs sub-second latency, WS goes in here without
touching any caller.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .. import i18n

log = logging.getLogger("ta.home")

TIMEOUT = 8.0
POLL_S = 5.0


class HomeError(RuntimeError):
    """A failure talking to Home Assistant. The message is for a human to read."""


class Home:
    def __init__(self, url: str, token: str | None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._client: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            if not self.token:
                raise HomeError(i18n.t("home.no_token"))
            self._client = httpx.AsyncClient(
                base_url=self.url,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=TIMEOUT,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        client = await self._http()
        try:
            r = await client.request(method, path, json=payload)
        except httpx.ConnectError as e:
            raise HomeError(i18n.t("home.unreachable", url=self.url)) from e
        except httpx.TimeoutException as e:
            raise HomeError(i18n.t("home.timeout", s=f"{TIMEOUT:.0f}")) from e
        if r.status_code == 401:
            raise HomeError(i18n.t("home.token_rejected"))
        if r.status_code >= 400:
            raise HomeError(f"HA respondeu {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    # ── Reading ─────────────────────────────────────────────────────────────
    async def state(self, entity_id: str) -> dict:
        return await self._request("GET", f"/api/states/{entity_id}")

    async def states(self) -> list[dict]:
        return await self._request("GET", "/api/states")

    async def entities(self, *prefixes: str) -> list[dict]:
        """Entities filtered by domain, for `ta entities`."""
        todos = await self.states()
        if not prefixes:
            return todos
        return [e for e in todos if e["entity_id"].startswith(prefixes)]

    # ── Commands ────────────────────────────────────────────────────────────
    async def call(self, domain: str, service: str, entity_id: str, **data: Any) -> Any:
        return await self._request(
            "POST", f"/api/services/{domain}/{service}", {"entity_id": entity_id, **data}
        )

    async def turn_on(self, entity_id: str, **data: Any) -> Any:
        domain = entity_id.split(".", 1)[0]
        return await self.call(domain, "turn_on", entity_id, **data)

    async def turn_off(self, entity_id: str) -> Any:
        domain = entity_id.split(".", 1)[0]
        return await self.call(domain, "turn_off", entity_id)

    async def switch_on(self, entity_id: str, brightness_pct: int | None = None) -> Any:
        """Turn the Entity on, aware of its domain.

        `brightness_pct` only exists in the `light` domain. An earlier version of
        this called `light/turn_on` unconditionally, and on a `switch` Home
        Assistant answered 200 and did nothing — a silent failure, the worst kind.
        Here the brightness is ignored with a warning when the domain does not
        support it, rather than the command vanishing.
        """
        domain = entity_id.split(".", 1)[0]
        if brightness_pct is None:
            return await self.turn_on(entity_id)

        pct = max(0, min(100, brightness_pct))
        if pct == 0:
            return await self.turn_off(entity_id)
        if domain != "light":
            log.info("%s is not a light: brightness ignored, turning on normally", entity_id)
            return await self.turn_on(entity_id)
        return await self.call("light", "turn_on", entity_id, brightness_pct=pct)

    # The old name, kept for Rules that are already written.
    async def light(self, entity_id: str, brightness_pct: int) -> Any:
        return await self.switch_on(entity_id, brightness_pct)

    async def confirm(self, entity_id: str, expected: str, tries: int = 12) -> tuple[str, bool]:
        """Wait for the state to become the expected one. Returns (state, confirmed).

        Necessary because reading the state right after calling the service
        returns the OLD value: the command round-trips through the vendor's cloud
        and comes back in 300–800ms. Without this, `ta on` reported `off` and
        looked like it had not worked — which is exactly how the bug showed up.

        Failing to confirm is not failing to command, which is why the caller gets
        both values instead of an exception.
        """
        for _ in range(tries):
            state = (await self.state(entity_id)).get("state", "unknown")
            if state == expected:
                return state, True
            await asyncio.sleep(0.25)
        return state, False

    # ── Sensors ─────────────────────────────────────────────────────────────
    async def sensors(self) -> dict:
        """A curated summary: weather, router and the plug's consumption.

        Curated on purpose. A raw sensor dump is mostly sun-position and backup
        entities, which is not what anyone means by "how is the house?".
        """
        from ..config import sensors as sensor_map

        roles = sensor_map()
        everything = {e["entity_id"]: e for e in await self.states()}

        def val(role: str):
            """The state behind a role, or None when the role is not mapped.

            None here means two different things — "you did not configure this"
            and "the sensor is unavailable" — and that is deliberate: to the
            caller they are the same absence, and `ta doctor` is where the
            difference gets explained.
            """
            e = everything.get(roles.get(role, ""))
            if e is None or e["state"] in ("unknown", "unavailable", None):
                return None
            return e["state"]

        weather = everything.get(roles.get("weather", "")) or {}
        attrs = weather.get("attributes", {})

        return {
            "weather": {
                "condition": weather.get("state"),
                "temperature": attrs.get("temperature"),
                "unit": attrs.get("temperature_unit"),
                "humidity": attrs.get("humidity"),
                "wind_speed": attrs.get("wind_speed"),
            },
            "router": {
                "external_ip": val("external_ip"),
                "download_kib_s": val("download"),
                "upload_kib_s": val("upload"),
            },
            # The plug measures current, so we can tell whether the fan is
            # actually drawing power — different from "the switch is on".
            "outlet": {
                "state": val("outlet"),
                "watts": val("watts"),
                "volts": val("volts"),
                "amps": val("amps"),
                "kwh_total": val("kwh_total"),
            },
        }

    # ── Media (Echo via alexa_media_player comes through here; ADR 0009) ─────
    async def volume(self, entity_id: str, level_pct: int) -> Any:
        pct = max(0, min(100, level_pct))
        return await self.call("media_player", "volume_set", entity_id, volume_level=pct / 100)

    async def play_media(self, entity_id: str, query: str, kind: str = "MUSIC") -> Any:
        return await self.call(
            "media_player", "play_media", entity_id,
            media_content_id=query, media_content_type=kind,
        )

    async def media(self, entity_id: str, action: str) -> Any:
        """`play`, `pause`, `stop`, `next`, `previous`."""
        services = {
            "play": "media_play", "pause": "media_pause", "stop": "media_stop",
            "next": "media_next_track", "previous": "media_previous_track",
        }
        if action not in services:
            raise HomeError(f"invalid media action: {action}. Use: {', '.join(services)}")
        return await self.call("media_player", services[action], entity_id)

    async def announce(self, entity_id: str, message: str) -> Any:
        """A voice announcement on an Echo.

        Always **additional** to the desktop notification, never a substitute —
        the Alexa integration is the least reliable piece here (ADR 0009).
        """
        return await self.call(
            "media_player", "play_media", entity_id,
            media_content_id=message, media_content_type="tts",
        )


class StateWatcher:
    """Watches state changes by polling and calls back on transitions."""

    def __init__(
        self, home: Home, on_change: Callable[[str, str, str], Awaitable[None]]
    ) -> None:
        self.home = home
        self.on_change = on_change
        self._previous: dict[str, str] = {}

    async def tick(self) -> None:
        try:
            current = {e["entity_id"]: e["state"] for e in await self.home.states()}
        except HomeError as e:
            log.debug("state polling failed: %s", e)
            return  # Home Assistant being down is not a state transition

        if not self._previous:              # the first read only sets the baseline
            self._previous = current
            return

        for entity_id, new in current.items():
            old = self._previous.get(entity_id)
            if old is not None and old != new:
                await self.on_change(entity_id, old, new)
        self._previous = current

    async def run(self) -> None:
        if not self.home.configured:
            log.info("Home Assistant has no token: the state watcher stays inert")
            return
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("state tick failed; the loop carries on")
            await asyncio.sleep(POLL_S)
