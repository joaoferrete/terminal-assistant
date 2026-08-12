"""Desktop notifications.

The reliable, mandatory path for any alert. An Echo announcement is always
*additional* to this, never a substitute — see ADR 0009: the Alexa integration is
the least reliable piece in the project, and the fragile path cannot be the only
one.

It uses `notify-send`, which talks to the user's session bus. That is why the
service is a systemd *user* unit and not a system service (ADR 0005).
"""

from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("ta.notify")

URGENCIES = ("low", "normal", "critical")


class Notifier:
    def __init__(self, app_name: str = "Terminal Assistant") -> None:
        self.app_name = app_name
        self._bin = shutil.which("notify-send")
        if self._bin is None:
            log.warning("notify-send not found: notifications will do nothing")

    @property
    def available(self) -> bool:
        return self._bin is not None

    async def send(
        self, title: str, body: str = "", *, urgency: str = "normal", icon: str | None = None
    ) -> bool:
        """Fire the notification. Returns whether it worked.

        It never raises: an alert that fails must not take down the Rule that
        asked for it, let alone the daemon.
        """
        if self._bin is None:
            return False
        if urgency not in URGENCIES:
            urgency = "normal"

        cmd = [self._bin, "--app-name", self.app_name, "--urgency", urgency]
        if icon:
            cmd += ["--icon", icon]
        cmd += [title, body]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log.error("notify-send exited with %s: %s", proc.returncode, err.decode().strip())
                return False
            return True
        except Exception:
            log.exception("notify-send failed")
            return False
