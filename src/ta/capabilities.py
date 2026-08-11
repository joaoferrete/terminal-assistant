"""O que funciona nesta máquina, e o que fazer com o que não funciona.

É a primeira pergunta de quem instala: das seis integrações, quais estão vivas
aqui? Antes disso, a resposta estava espalhada — um `shutil.which` em
`notify.py`, outro em `lighter.py`, outro em `mic.py`, um `Calendar.error` que o
`/health` calculava e descartava, e um `Home.configured` — e chegava ao usuário
só por `journalctl`, em português, depois do daemon subir.

Aqui elas viram uma lista só, e essa lista alimenta **quatro** superfícies:
`ta doctor`, `/health`, o agrupamento do `ta --help` e as mensagens de erro de
runtime. Um lugar decide o que está vivo, então elas não podem divergir — do
mesmo jeito que `STATUS_MARK` é um lugar só para o CLI e o export.

Nada aqui exige o daemon de pé. É de propósito: quem mais precisa do diagnóstico
é justamente quem não conseguiu subir o daemon.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from .config import Config, config_file
from .i18n import lang, lang_source


@dataclass(frozen=True)
class Capability:
    key: str
    label: str
    ok: bool
    # Por que não está disponível, e o passo exato do conserto. Vazios quando ok.
    # `fix` é o que separa um diagnóstico útil de um que só confirma o problema.
    reason: str = ""
    fix: str = ""
    # `essential` marca o que o projeto NÃO pode perder. Só as notas são: elas
    # rodam em qualquer lugar que rode Python, e é o que o posicionamento promete.
    essential: bool = False
    # Os comandos que morrem junto. É o que o `--help` usa para marcar o grupo.
    commands: tuple[str, ...] = ()

    @property
    def resumo(self) -> str:
        """A primeira frase do motivo, para caber numa linha de `--help`.

        O motivo completo pode ser longo — o da agenda traz o erro do GLib inteiro
        — e isso é certo no `ta doctor`, que tem a tela para explicar, e errado
        numa lista de comandos, onde ele empurra tudo para fora.
        """
        return self.reason.split(". ")[0] if self.reason else ""


def _notes(cfg: Config) -> Capability:
    from .db import default_db_path

    caminho = default_db_path()
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        gravavel = True
    except OSError:
        gravavel = False
    return Capability(
        key="notes",
        label="Notes",
        ok=gravavel,
        reason="" if gravavel else f"não dá para escrever em {caminho.parent}",
        fix="" if gravavel else f"confira as permissões de {caminho.parent}",
        essential=True,
        commands=("note", "list", "done", "rm", "restore", "board", "export", "today"),
    )


def _calendar(_: Config) -> Capability:
    from .sensors.calendar import Calendar

    cal = Calendar()
    # `available` dispara o import de `gi` e guarda o motivo — que já vinha com o
    # conserto embutido e era jogado fora pelo `/health`, que expunha só o booleano.
    ok = cal.available
    motivo = "" if ok else (cal.error or "Evolution Data Server fora de alcance")

    # O conserto depende de QUAL falha foi. Mandar instalar typelib para quem já
    # os tem e só não tem barramento é pior que não sugerir nada: a pessoa roda o
    # `apt`, nada muda, e passa a desconfiar do diagnóstico inteiro.
    if ok:
        conserto = ""
    elif "D-Bus" in motivo or "DISPLAY" in motivo:
        conserto = "abra numa sessão gráfica; sem ela a agenda não tem como ser lida"
    else:
        conserto = "sudo apt install gir1.2-ecal-2.0 gir1.2-edataserver-1.2"

    return Capability(
        key="calendar",
        label="Calendar",
        ok=ok,
        reason=motivo,
        fix=conserto,
        commands=("today", "event"),
    )


def _mic(_: Config) -> Capability:
    ok = shutil.which("pw-dump") is not None
    return Capability(
        key="mic",
        label="Microphone",
        ok=ok,
        reason="" if ok else "`pw-dump` não encontrado: o gatilho de reunião fica inerte",
        fix="" if ok else "sudo apt install pipewire-utils",
        commands=(),
    )


def _home(cfg: Config) -> Capability:
    ok = bool(cfg.ha_token)
    return Capability(
        key="home",
        label="Home Assistant",
        ok=ok,
        reason="" if ok else "HA_TOKEN não está definido",
        fix="" if ok else "gere um token de longa duração no HA e ponha HA_TOKEN no .env",
        commands=("on", "off", "luz", "light", "entities", "temp", "router", "media"),
    )


def _lighter(_: Config) -> Capability:
    from .actuators.lighter import Lighter

    lit = Lighter()
    ok = lit.available
    tem_gsettings = shutil.which("gsettings") is not None
    return Capability(
        key="lighter",
        label="Lighter (ringlight)",
        ok=ok,
        reason=(
            ""
            if ok
            else ("`gsettings` não encontrado" if not tem_gsettings
                  else "a extensão do GNOME não está instalada")
        ),
        fix="" if ok else "https://github.com/joaoferrete/Lighter",
        commands=("lighter",),
    )


def _ai(cfg: Config) -> Capability:
    ok = bool(cfg.gemini_api_key)
    return Capability(
        key="ai",
        label="AI (Gemini)",
        ok=ok,
        reason="" if ok else "GEMINI_API_KEY não está definida",
        # A frase diz que é opcional de propósito: sem isso, uma linha vermelha na
        # tabela se lê como instalação quebrada — e não é. Quase tudo funciona sem.
        fix="" if ok else "opcional. Para ligar: GEMINI_API_KEY no .env",
        commands=("init", "priorities", "organize", "prose", "revise", "event"),
    )


SONDAS = (_notes, _calendar, _mic, _home, _lighter, _ai)


def inspect(cfg: Config | None = None) -> list[Capability]:
    """As seis capacidades, na ordem em que fazem sentido para quem lê.

    `notes` primeiro porque é a única essencial; o resto é o que se acrescenta.

    Uma sonda que estoura vira uma linha de falha, e não derruba as outras cinco:
    um diagnóstico que morre no primeiro problema é inútil justamente na máquina
    com problema. A agenda já provou isso — sem sessão gráfica ela levantava um
    `GError` do GLib e levava junto o `ta doctor` inteiro.
    """
    cfg = cfg or Config.from_env()
    saida = []
    for sonda in SONDAS:
        try:
            saida.append(sonda(cfg))
        except Exception as e:  # noqa: BLE001 — diagnóstico não pode morrer
            nome = sonda.__name__.lstrip("_")
            saida.append(
                Capability(
                    key=nome,
                    label=nome.capitalize(),
                    ok=False,
                    reason=f"a sondagem falhou: {e}",
                    fix="isto é um bug do Terminal Assistant; por favor relate",
                )
            )
    return saida


def por_comando(caps: list[Capability]) -> dict[str, Capability]:
    """De comando do CLI para a capacidade que ele exige. Para o `--help`."""
    return {cmd: cap for cap in caps for cmd in cap.commands}


def ambiente(cfg: Config | None = None) -> list[tuple[str, str]]:
    """Contexto que não é capacidade, mas é a segunda coisa que se pergunta."""
    cfg = cfg or Config.from_env()
    return [
        ("idioma", f"{lang()} (de: {lang_source()})"),
        ("config", str(config_file()) + ("" if config_file().exists() else "  (não existe)")),
        ("daemon", cfg.base_url + ("  [aberto na rede]" if cfg.exposed else "  [só local]")),
    ]
