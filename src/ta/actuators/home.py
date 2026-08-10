"""Cliente do Home Assistant.

O HA é a única camada que conhece protocolo de aparelho (ADR 0001). Este módulo
só fala `entity_id` e serviços — nunca Tuya, nunca Zigbee, nunca IP.

**Estado por polling, não WebSocket.** O plano previa WS, e a troca é deliberada:
WS exigiria uma dependência nova e uma máquina de reconexão, e o único consumidor
de estado empurrado seria o gatilho `entity_state`, que não está no caminho da
regra que motivou o projeto (essa usa microfone e hora). Polling de alguns
segundos numa casa com três aparelhos custa nada e não tem estado para
ressincronizar quando a rede oscila. Se um dia um gatilho precisar de latência
sub-segundo, WS entra aqui sem mexer em quem chama.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

log = logging.getLogger("ta.home")

TIMEOUT = 8.0
POLL_S = 5.0


class HomeError(RuntimeError):
    """Falha ao falar com o HA. Mensagem pensada para o usuário ler."""


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
                raise HomeError(
                    "HA_TOKEN não está configurado. Confira o .env e "
                    "`curl localhost:7777/health`."
                )
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
            raise HomeError(f"Home Assistant não responde em {self.url}. `docker ps`?") from e
        except httpx.TimeoutException as e:
            raise HomeError(f"Home Assistant não respondeu em {TIMEOUT:.0f}s.") from e
        if r.status_code == 401:
            raise HomeError("HA recusou o token (401). Ele foi revogado?")
        if r.status_code >= 400:
            raise HomeError(f"HA respondeu {r.status_code}: {r.text[:200]}")
        return r.json() if r.content else None

    # ── Leitura ─────────────────────────────────────────────────────────────
    async def state(self, entity_id: str) -> dict:
        return await self._request("GET", f"/api/states/{entity_id}")

    async def states(self) -> list[dict]:
        return await self._request("GET", "/api/states")

    async def entities(self, *prefixes: str) -> list[dict]:
        """Entities filtradas por domínio, para `ta luz --list`."""
        todos = await self.states()
        if not prefixes:
            return todos
        return [e for e in todos if e["entity_id"].startswith(prefixes)]

    # ── Comandos ────────────────────────────────────────────────────────────
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
        """Liga a Entity, ciente do domínio.

        `brightness_pct` só existe no domínio `light`. Uma versão anterior disto
        chamava `light/turn_on` fixo, e num `switch` o HA respondia 200 sem fazer
        nada — falha silenciosa, o pior tipo. Aqui o brilho é ignorado com aviso
        quando o domínio não o suporta, em vez de o comando sumir.
        """
        domain = entity_id.split(".", 1)[0]
        if brightness_pct is None:
            return await self.turn_on(entity_id)

        pct = max(0, min(100, brightness_pct))
        if pct == 0:
            return await self.turn_off(entity_id)
        if domain != "light":
            log.info("%s não é light: brilho ignorado, ligando normalmente", entity_id)
            return await self.turn_on(entity_id)
        return await self.call("light", "turn_on", entity_id, brightness_pct=pct)

    # Nome antigo mantido para as Rules já escritas.
    async def light(self, entity_id: str, brightness_pct: int) -> Any:
        return await self.switch_on(entity_id, brightness_pct)

    async def confirm(self, entity_id: str, esperado: str, tries: int = 12) -> tuple[str, bool]:
        """Espera o estado virar o esperado. Devolve (estado, confirmado).

        Necessário porque ler o estado imediatamente depois de chamar o serviço
        devolve o valor ANTIGO: o comando vai para a nuvem da Tuya e volta em
        300–800ms. Sem isto, `ta on` reportava `off` e parecia não ter funcionado
        — foi exatamente como o bug se apresentou.

        Falha em confirmar não é falha em comandar, e por isso o chamador recebe
        os dois valores em vez de uma exceção.
        """
        for _ in range(tries):
            estado = (await self.state(entity_id)).get("state", "unknown")
            if estado == esperado:
                return estado, True
            await asyncio.sleep(0.25)
        return estado, False

    # ── Sensores ────────────────────────────────────────────────────────────
    async def sensors(self) -> dict:
        """Resumo curado: clima, roteador e consumo da tomada.

        Curado de propósito. As 17 entidades `sensor` incluem seis do módulo Sun e
        quatro do backup do HA, que não são o que alguém quer ver ao perguntar
        "como está a casa".
        """
        todos = {e["entity_id"]: e for e in await self.states()}

        def val(eid: str):
            e = todos.get(eid)
            if e is None or e["state"] in ("unknown", "unavailable", None):
                return None
            return e["state"]

        clima = todos.get("weather.forecast_casa") or {}
        attrs = clima.get("attributes", {})

        return {
            "weather": {
                "condition": clima.get("state"),
                "temperature": attrs.get("temperature"),
                "unit": attrs.get("temperature_unit"),
                "humidity": attrs.get("humidity"),
                "wind_speed": attrs.get("wind_speed"),
            },
            "router": {
                "external_ip": val("sensor.s7_external_ip"),
                "download_kib_s": val("sensor.s7_download_speed"),
                "upload_kib_s": val("sensor.s7_upload_speed"),
            },
            # A tomada mede corrente, então dá para saber se o ventilador está
            # realmente puxando energia — diferente de "o interruptor está ligado".
            "outlet": {
                "state": val("switch.ventilador_socket_1"),
                "watts": val("sensor.ventilador_energia"),
                "volts": val("sensor.ventilador_tensao"),
                "amps": val("sensor.ventilador_corrente"),
                "kwh_total": val("sensor.ventilador_energia_total"),
            },
        }

    # ── Mídia (Echo via alexa_media_player entra por aqui; ADR 0009) ─────────
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
        servicos = {
            "play": "media_play", "pause": "media_pause", "stop": "media_stop",
            "next": "media_next_track", "previous": "media_previous_track",
        }
        if action not in servicos:
            raise HomeError(f"ação de mídia inválida: {action}. Use: {', '.join(servicos)}")
        return await self.call("media_player", servicos[action], entity_id)

    async def announce(self, entity_id: str, message: str) -> Any:
        """Anúncio de voz num Echo.

        Sempre **adicional** à notificação de desktop, nunca substituto — a
        integração da Alexa é a peça menos confiável do projeto (ADR 0009).
        """
        return await self.call(
            "media_player", "play_media", entity_id,
            media_content_id=message, media_content_type="tts",
        )


class StateWatcher:
    """Observa mudanças de estado por polling e chama de volta nas transições."""

    def __init__(
        self, home: Home, on_change: Callable[[str, str, str], Awaitable[None]]
    ) -> None:
        self.home = home
        self.on_change = on_change
        self._anterior: dict[str, str] = {}

    async def tick(self) -> None:
        try:
            atual = {e["entity_id"]: e["state"] for e in await self.home.states()}
        except HomeError as e:
            log.debug("polling de estado falhou: %s", e)
            return  # HA fora do ar não é transição de estado

        if not self._anterior:              # primeira leitura só estabelece a base
            self._anterior = atual
            return

        for entity_id, novo in atual.items():
            velho = self._anterior.get(entity_id)
            if velho is not None and velho != novo:
                await self.on_change(entity_id, velho, novo)
        self._anterior = atual

    async def run(self) -> None:
        if not self.home.configured:
            log.info("HA sem token: watcher de estado inerte")
            return
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tick de estado falhou; o laço continua")
            await asyncio.sleep(POLL_S)
