"""Notificação de desktop.

Caminho confiável e obrigatório de qualquer aviso. O anúncio no Echo é sempre
*adicional* a isto, nunca substituto — ver ADR 0009: a integração da Alexa é a
peça menos confiável do projeto, e o caminho frágil não pode ser o único.

Usa `notify-send`, que fala com o barramento de sessão do usuário. É por isso que
o serviço é um systemd *user* unit e não um serviço de sistema (ADR 0005).
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
            log.warning("notify-send não encontrado: notificações ficam sem efeito")

    @property
    def available(self) -> bool:
        return self._bin is not None

    async def send(
        self, title: str, body: str = "", *, urgency: str = "normal", icon: str | None = None
    ) -> bool:
        """Dispara a notificação. Devolve se conseguiu.

        Nunca levanta: um aviso que falha não deve derrubar a Rule que o pediu,
        muito menos o daemon.
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
                log.error("notify-send saiu com %s: %s", proc.returncode, err.decode().strip())
                return False
            return True
        except Exception:
            log.exception("notify-send falhou")
            return False
