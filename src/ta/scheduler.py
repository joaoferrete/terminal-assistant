"""Scheduler: os gatilhos de tempo.

Dois papéis. Disparar Rules em horários (`at_time`), e disparar os Reminders
vencidos — que é o caminho que fecha o primeiro loop completo do motor
(tempo → notificar) e prova a tese de que os dois módulos compartilham um motor.

Uma decisão explícita: **nada de enxurrada retroativa.** Se o daemon ficou parado
por horas, subir não deve cuspir dezenas de notificações de horários que já
passaram. Reminder vencido dispara uma vez, mas horário perdido é perdido.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

log = logging.getLogger("ta.scheduler")

TICK_S = 20.0

# Reminder mais atrasado que isto dispara com aviso de atraso em vez de fingir
# que é agora. Sem isso, subir o daemon depois de um fim de semana avisaria
# "tomar remédio" de sexta.
ATRASO_MAX = timedelta(hours=12)


class Scheduler:
    def __init__(
        self,
        on_time: Callable[[str, datetime], Awaitable[None]],
        on_reminder: Callable[[datetime], Awaitable[None]],
    ) -> None:
        self.on_time = on_time
        self.on_reminder = on_reminder
        self._ultimo_minuto: str | None = None

    async def tick(self, agora: datetime) -> None:
        """Um ciclo. Recebe `agora` para ser testável sem esperar o relógio."""
        minuto = agora.strftime("%H:%M")

        # Um gatilho de horário dispara uma vez por minuto, não uma por tick.
        if minuto != self._ultimo_minuto:
            primeiro = self._ultimo_minuto is None
            self._ultimo_minuto = minuto
            # No primeiro tick não dispara: subir o daemon às 16:00 não deve
            # executar a regra das 16:00 como se o minuto tivesse acabado de virar.
            if not primeiro:
                await self.on_time(minuto, agora)

        await self.on_reminder(agora)

    async def run(self) -> None:
        while True:
            try:
                await self.tick(datetime.now())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick do scheduler falhou; o laço continua")
            await asyncio.sleep(TICK_S)


def atraso_de(remind_at: str, agora: datetime) -> timedelta:
    return agora - datetime.fromisoformat(remind_at)


def texto_de_atraso(atraso: timedelta) -> str:
    """Rótulo honesto quando o Reminder dispara tarde."""
    if atraso <= timedelta(minutes=2):
        return ""
    if atraso < timedelta(hours=1):
        return f" (atrasado {int(atraso.total_seconds() // 60)} min)"
    if atraso < ATRASO_MAX:
        return f" (atrasado {int(atraso.total_seconds() // 3600)} h)"
    return " (muito atrasado)"
