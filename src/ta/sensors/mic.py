"""Watcher de microfone via PipeWire.

O gatilho de reunião é o microfone em uso, não o título da janela: a sessão é
Wayland e nenhum processo externo ao compositor lê título de janela (ADR 0008).
O sinal também é universal — vale para Meet, Zoom, Slack e Discord sem saber a
diferença entre eles.

O sinal é a existência de algum nó `Stream/Input/Audio` no grafo do PipeWire.
Validado na prática: fora de chamada, `pw-dump` não lista nenhum.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from collections.abc import Awaitable, Callable

log = logging.getLogger("ta.mic")

# Um app pode abrir e fechar o stream em rajada ao entrar numa call. O debounce
# evita que a luz pisque junto.
DEBOUNCE_S = 2.0
POLL_S = 2.0

# Streams que não significam "estou numa reunião". O próprio monitor de nível de
# áudio do GNOME aparece como Stream/Input/Audio.
IGNORAR = ("gnome-shell", "gsd-media-keys", "pipewire-pulse-monitor")


def _streams_de_entrada(dump: list[dict]) -> list[str]:
    """Nomes dos apps com stream de captura ativo."""
    nomes = []
    for node in dump:
        info = node.get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Input/Audio":
            continue
        nome = props.get("application.name") or props.get("node.name") or "?"
        if any(ig in nome.lower() for ig in IGNORAR):
            continue
        nomes.append(nome)
    return nomes


class MicWatcher:
    """Observa o microfone e chama de volta nas transições, nos dois sentidos."""

    def __init__(self, on_change: Callable[[bool, list[str]], Awaitable[None]]) -> None:
        self.on_change = on_change
        self._bin = shutil.which("pw-dump")
        self.active = False
        self.apps: list[str] = []
        self._pendente: bool | None = None
        self._desde: float = 0.0
        if self._bin is None:
            log.warning("pw-dump não encontrado: o gatilho de microfone fica inerte")

    @property
    def available(self) -> bool:
        return self._bin is not None

    async def _ler(self) -> list[str] | None:
        """Lê o grafo. None significa 'não consegui ler', que é diferente de 'vazio'."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self._bin, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
            out, _ = await proc.communicate()
            if proc.returncode != 0:
                return None
            return _streams_de_entrada(json.loads(out))
        except (json.JSONDecodeError, OSError):
            log.exception("falha ao ler pw-dump")
            return None

    async def tick(self, agora: float) -> None:
        """Um ciclo. Separado do laço para ser testável sem dormir."""
        apps = await self._ler()
        if apps is None:
            return  # falha de leitura não é transição: não inventa estado

        ativo = bool(apps)
        if ativo == self.active:
            self._pendente = None  # voltou ao estado atual antes do debounce vencer
            return

        if self._pendente != ativo:
            self._pendente, self._desde = ativo, agora
            return

        if agora - self._desde >= DEBOUNCE_S:
            self.active, self.apps, self._pendente = ativo, apps, None
            log.info("microfone %s (%s)", "ativo" if ativo else "inativo", ", ".join(apps) or "-")
            await self.on_change(ativo, apps)

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
                log.exception("tick do microfone falhou; o watcher continua")
            await asyncio.sleep(POLL_S)
