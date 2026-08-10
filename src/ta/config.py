"""Configuração: um lugar só, lida do ambiente.

Segredos nunca vêm de arquivo versionado. O systemd unit aponta um
`EnvironmentFile` para o `.env` da raiz, que está no `.gitignore`.

Nomes de variável têm que ser válidos para shell e para systemd: letras,
dígitos e `_`. Hífen não funciona em nenhum dos dois — foi um erro real no
início do projeto e está registrado no ROADMAP.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_HOST = "0.0.0.0"  # noqa: S104 — LAN de propósito: o mural abre no celular
DEFAULT_PORT = 7777


@dataclass(frozen=True)
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ha_url: str = "http://localhost:8123"
    ha_token: str | None = None
    gemini_api_key: str | None = None
    # Echo(s) para anúncio de voz. Vazio = ninguém para falar, e o Reminder
    # continua avisando na tela — o caminho confiável nunca depende disto.
    echo_entities: tuple[str, ...] = ()

    # Segunda passada do LLM sobre cada Note capturada. Ligada por padrão; custa
    # uma chamada de modelo por captura, e `TA_AUTO_REVIEW=0` desliga sem tocar
    # em código. Desligada, a captura continua funcionando com o regex sozinho.
    auto_review: bool = True

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            host=os.environ.get("TA_HOST", DEFAULT_HOST),
            port=int(os.environ.get("TA_PORT", DEFAULT_PORT)),
            ha_url=os.environ.get("HA_URL", "http://localhost:8123").rstrip("/"),
            ha_token=os.environ.get("HA_TOKEN") or None,
            gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
            auto_review=os.environ.get("TA_AUTO_REVIEW", "1") not in ("0", "false", "no"),
            echo_entities=tuple(
                e.strip() for e in os.environ.get("TA_ECHOS", "").split(",") if e.strip()
            ),
        )

    @property
    def base_url(self) -> str:
        """Endereço que o CLI usa para falar com o daemon."""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host  # noqa: S104
        return f"http://{host}:{self.port}"


# Apelidos curtos para entity_id, para que a linha de comando não exija digitar
# `light.lampada_do_quarto`. O inventário real está no README; estes apelidos são
# conveniência de CLI, não modelo de domínio.
ENTITY_ALIASES: dict[str, str] = {
    "quarto": "light.lampada_do_quarto",
    "ventilador": "switch.ventilador_socket_1",
}


# Grupos por domínio: `ta on luz` liga todas as luzes.
GROUPS: dict[str, tuple[str, ...]] = {
    "luz": ("light.",),
    "luzes": ("light.",),
    "tomada": ("switch.",),
    "tomadas": ("switch.",),
    "tudo": ("light.", "switch."),
}


def _sem_acento(s: str) -> str:
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
    )


def _comandavel(e: dict) -> bool:
    """Se a Entity é um aparelho, e não um ajuste do aparelho.

    A tomada Ekasa expõe duas Entities: o socket, com `device_class: outlet`, e o
    travamento infantil, **sem device_class nenhum**. `ta on tudo` estava ligando
    a trava, que é configuração e não aparelho.

    A API REST do HA não expõe `entity_category`, então a presença de
    `device_class` é o sinal disponível — e é um sinal de princípio, não um
    casamento de nome. Vale só para grupos e ambientes: nomear a Entity
    explicitamente continua ligando o que você pediu.
    """
    if e["entity_id"].startswith("light."):
        return True
    return bool(e.get("attributes", {}).get("device_class"))


def resolve_entity(name: str) -> str:
    """Traduz apelido para entity_id. Um valor com ponto já é entity_id."""
    if "." in name:
        return name
    return ENTITY_ALIASES.get(name, name)


def resolve_targets(term: str, entities: list[dict]) -> list[str]:
    """Resolve um termo para uma LISTA de entity_id.

    A ordem importa, do mais específico ao mais amplo:

      1. `light.lampada_do_quarto` — já é entity_id
      2. `luz`, `tudo`            — grupo por domínio
      3. `quarto`                 — apelido explícito
      4. `sala`                   — ambiente, por trecho do nome

    Ambiente casa por trecho porque o HA não expõe áreas na API REST, e porque
    isso pega luzes futuras do mesmo lugar sem eu ter que atualizar uma lista.
    Busca sem acento e sem caso: `Lâmpada do quarto` casa com `quarto`.
    """
    if "." in term:
        return [term]

    chave = _sem_acento(term)

    if chave in GROUPS:
        prefixos = GROUPS[chave]
        return [
            e["entity_id"]
            for e in entities
            if e["entity_id"].startswith(prefixos) and _comandavel(e)
        ]

    if chave in ENTITY_ALIASES:
        return [ENTITY_ALIASES[chave]]

    def casa(e: dict, prefixo: str) -> bool:
        if not e["entity_id"].startswith(prefixo) or not _comandavel(e):
            return False
        nome = _sem_acento(e.get("attributes", {}).get("friendly_name") or "")
        return chave in _sem_acento(e["entity_id"]) or chave in nome

    # Luz primeiro: "ligar o quarto" quer dizer a luz, não o ventilador. Só cai
    # para switch se nenhuma luz casar.
    luzes = [e["entity_id"] for e in entities if casa(e, "light.")]
    if luzes:
        return luzes
    return [e["entity_id"] for e in entities if casa(e, "switch.")]
