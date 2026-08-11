"""Configuração: um lugar só, lida do ambiente.

Segredos nunca vêm de arquivo versionado. O systemd unit aponta um
`EnvironmentFile` para o `.env` da raiz, que está no `.gitignore`.

Nomes de variável têm que ser válidos para shell e para systemd: letras,
dígitos e `_`. Hífen não funciona em nenhum dos dois — foi um erro real no
início do projeto e está registrado no ROADMAP.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("ta")

# Loopback por padrão. O mural no celular continua possível, mas passou a exigir
# dois atos deliberados — `TA_HOST` e `TA_TOKEN` —, porque o daemon expõe as notas
# inteiras, o comando da casa e a chave do modelo, tudo sem credencial (ADR 0012).
# Quem lê SECURITY.md já se preocupa; quem segue o passo a passo é quem não sabe
# que devia.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7777

# Endereços em que não há rede alheia alcançando o daemon. Fora desta lista,
# `TA_TOKEN` é obrigatório e o daemon recusa subir sem ele.
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class ConfigError(RuntimeError):
    """Configuração que não dá para corrigir em runtime. O daemon não sobe."""


@dataclass(frozen=True)
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ha_url: str = "http://localhost:8123"
    ha_token: str | None = None
    gemini_api_key: str | None = None
    # Credencial do PRÓPRIO daemon, não de terceiro. Só é exigida quando o bind
    # sai do loopback; em loopback fica None e nada muda no uso local.
    token: str | None = None
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
            token=os.environ.get("TA_TOKEN") or None,
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

    @property
    def exposed(self) -> bool:
        """Se o bind alcança outra máquina. `0.0.0.0` e um IP de LAN alcançam."""
        return self.host not in LOOPBACK

    def check(self) -> None:
        """Recusa uma configuração que exporia o daemon sem credencial.

        Falha alto e cedo, com o conserto na mensagem — mesmo espírito do
        `make check-gi`. Um daemon que sobe e só depois se descobre aberto é pior
        que um que não sobe: ninguém vai reler o log de boot.
        """
        if self.exposed and not self.token:
            raise ConfigError(
                f"TA_HOST={self.host} expõe o daemon na rede, e ele não tem\n"
                "autenticação própria: qualquer um na mesma rede leria suas notas,\n"
                "comandaria a casa e gastaria sua chave de modelo.\n"
                "\n"
                "Para abrir com credencial, gere um token e reinicie:\n"
                "\n"
                "    echo \"TA_TOKEN=$(python3 -c 'import secrets;"
                " print(secrets.token_urlsafe(32))')\" >> .env\n"
                "    systemctl --user restart ta\n"
                "\n"
                "Para voltar ao acesso só local, remova TA_HOST do .env."
            )


# ── Configuração do usuário ─────────────────────────────────────────────────
# Apelidos e grupos moram em `~/.config/ta/config.toml`, e não aqui. Enquanto o
# repositório foi de uma pessoa só, ter `quarto = light.abajur` fixo no
# código-fonte era prático. Aberto, isso significaria que configurar a própria
# casa exige editar o pacote instalado — mudança que se perde em toda
# reinstalação (ADR 0014).
#
# O caminho segue o mesmo padrão de `db.default_db_path()`, que já estava certo.
def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ta"


def config_file() -> Path:
    return config_dir() / "config.toml"


# Grupos por domínio: `ta on luz` liga todas as luzes. Diferente dos apelidos,
# estes não são pessoais — valem para qualquer casa —, então vêm embutidos e o
# arquivo do usuário só acrescenta.
GRUPOS_PADRAO: dict[str, tuple[str, ...]] = {
    "luz": ("light.",),
    "luzes": ("light.",),
    "tomada": ("switch.",),
    "tomadas": ("switch.",),
    "tudo": ("light.", "switch."),
}


@lru_cache(maxsize=1)
def _user_config() -> dict:
    """Lê `config.toml` uma vez. Ausente ou ilegível não é erro.

    Uma casa sem apelidos funciona: `ta on light.o_que_for` continua exato, e
    `ta on quarto` casa por trecho do nome vindo do próprio Home Assistant. Falhar
    o daemon por causa de um arquivo de conveniência seria desproporcional — mas
    falhar **calado** por causa de TOML quebrado seria pior, então isso vira log.
    """
    caminho = config_file()
    if not caminho.exists():
        return {}
    try:
        with caminho.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("%s ignorado: %s", caminho, e)
        return {}


def entity_aliases() -> dict[str, str]:
    """Apelidos curtos para `entity_id`, do arquivo do usuário. Pode ser vazio."""
    bruto = _user_config().get("aliases", {})
    return {str(k): str(v) for k, v in bruto.items()} if isinstance(bruto, dict) else {}


def groups() -> dict[str, tuple[str, ...]]:
    """Os grupos embutidos, mais os do usuário. O do usuário vence no conflito."""
    do_usuario = _user_config().get("groups", {})
    extras = (
        {str(k): tuple(v) for k, v in do_usuario.items() if isinstance(v, list)}
        if isinstance(do_usuario, dict)
        else {}
    )
    return {**GRUPOS_PADRAO, **extras}


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
    return entity_aliases().get(name, name)


def resolve_targets(term: str, entities: list[dict]) -> list[str]:
    """Resolve um termo para uma LISTA de entity_id.

    A ordem importa, do mais específico ao mais amplo:

      1. `light.abajur`          — já é entity_id
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

    grupos = groups()
    if chave in grupos:
        prefixos = grupos[chave]
        return [
            e["entity_id"]
            for e in entities
            if e["entity_id"].startswith(prefixos) and _comandavel(e)
        ]

    apelidos = entity_aliases()
    if chave in apelidos:
        return [apelidos[chave]]

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
