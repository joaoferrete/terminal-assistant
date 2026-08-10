"""Motor de regras.

O app é o cérebro das automações (ADR 0002): ele avalia os gatilhos e trata o
Home Assistant como atuador burro. As Rules são Python, versionadas no
repositório, e este módulo é o despachante — não um interpretador.

Cada Rule é carregada isoladamente em try/except. Uma regra com erro de sintaxe
tira **ela** do ar, nunca o daemon: é a mitigação prometida no ADR 0002.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any

log = logging.getLogger("ta.engine")

# Assinatura de uma Rule: recebe o contexto, faz o que tem para fazer.
RuleFn = Callable[["Context"], Awaitable[None] | None]


@dataclass
class Trigger:
    """A condição que inicia uma Rule.

    `kind` identifica a fonte do sinal (`mic`, `time`, `reminder`, `state`), e
    `value` a qualifica. Deliberadamente pobre: gatilho rico viraria a linguagem
    de template que o ADR 0002 recusou escrever.
    """

    kind: str
    value: Any = None

    def __str__(self) -> str:
        return f"{self.kind}={self.value}" if self.value is not None else self.kind


@dataclass
class Rule:
    name: str
    fn: RuleFn
    on: list[Trigger]
    when: Callable[[Context], bool] | None = None
    source: str = "?"


@dataclass
class Context:
    """O que uma Rule recebe. Atuadores e sensores entram aqui pelo daemon."""

    trigger: Trigger
    now: Any = None
    home: Any = None
    lighter: Any = None
    notify: Any = None
    calendar: Any = None
    note: Any = None  # presente quando o gatilho é um Reminder
    extra: dict[str, Any] = field(default_factory=dict)


# ── Registro ────────────────────────────────────────────────────────────────
# Registro de módulo: os arquivos em rules/ chamam @rule na importação, e o
# loader recolhe o que apareceu. Simples e suficiente para um app de uma máquina.
_REGISTRY: list[Rule] = []


def rule(
    *,
    on: Trigger | list[Trigger],
    when: Callable[[Context], bool] | None = None,
    name: str | None = None,
) -> Callable[[RuleFn], RuleFn]:
    """Registra uma Rule.

        @rule(on=mic_active(), when=after("16:00"))
        async def reuniao_tarde(ctx): ...
    """
    triggers = on if isinstance(on, list) else [on]

    def deco(fn: RuleFn) -> RuleFn:
        _REGISTRY.append(
            Rule(
                name=name or fn.__name__,
                fn=fn,
                on=triggers,
                when=when,
                source=getattr(fn, "__module__", "?"),
            )
        )
        return fn

    return deco


# ── Gatilhos e condições prontos ────────────────────────────────────────────
def mic_active() -> Trigger:
    """Microfone passou a ser usado — o sinal de 'entrei numa call' (ADR 0008)."""
    return Trigger("mic", True)


def mic_inactive() -> Trigger:
    return Trigger("mic", False)


def at_time(hhmm: str) -> Trigger:
    return Trigger("time", hhmm)


def reminder_due() -> Trigger:
    return Trigger("reminder")


def entity_state(entity_id: str) -> Trigger:
    return Trigger("state", entity_id)


def after(hhmm: str) -> Callable[[Context], bool]:
    """Condição de hora: 'e passou das 16h'. É o que não existe na Lighter."""
    h, m = (int(x) for x in hhmm.split(":"))
    return lambda ctx: ctx.now.time() >= time(h, m)


def before(hhmm: str) -> Callable[[Context], bool]:
    h, m = (int(x) for x in hhmm.split(":"))
    return lambda ctx: ctx.now.time() < time(h, m)


def all_of(*conds: Callable[[Context], bool]) -> Callable[[Context], bool]:
    return lambda ctx: all(c(ctx) for c in conds)


def any_of(*conds: Callable[[Context], bool]) -> Callable[[Context], bool]:
    return lambda ctx: any(c(ctx) for c in conds)


# ── Carga ───────────────────────────────────────────────────────────────────
@dataclass
class LoadReport:
    rules: list[Rule]
    errors: list[tuple[str, str]]  # (arquivo, traceback)

    @property
    def ok(self) -> bool:
        return not self.errors


def load_rules(directory: Path) -> LoadReport:
    """Carrega os arquivos de Rule, um a um, isoladamente.

    Um arquivo que estoura entra em `errors` e os outros seguem carregando. É a
    diferença entre 'uma regra quebrada' e 'o daemon caiu'.
    """
    _REGISTRY.clear()
    errors: list[tuple[str, str]] = []

    if not directory.is_dir():
        log.warning("diretório de regras não existe: %s", directory)
        return LoadReport([], [])

    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        antes = len(_REGISTRY)
        try:
            spec = importlib.util.spec_from_file_location(f"ta_rules.{path.stem}", path)
            if spec is None or spec.loader is None:
                raise ImportError(f"não consegui carregar {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except BaseException:
            # BaseException de propósito: um `exit()` num arquivo de regra não
            # deve derrubar o daemon junto.
            errors.append((path.name, traceback.format_exc()))
            log.error("regra %s falhou ao carregar; as outras seguem", path.name)
            continue
        log.info("regra %s carregada (%d)", path.name, len(_REGISTRY) - antes)

    return LoadReport(list(_REGISTRY), errors)


# ── Despacho ────────────────────────────────────────────────────────────────
def matching(rules: list[Rule], trigger: Trigger) -> list[Rule]:
    return [
        r
        for r in rules
        # value None num gatilho registrado significa "qualquer valor".
        for t in r.on
        if t.kind == trigger.kind and (t.value is None or t.value == trigger.value)
    ]


async def dispatch(rules: list[Rule], ctx: Context) -> list[str]:
    """Roda as Rules que casam com o gatilho. Devolve os nomes das que rodaram.

    Exceção dentro de uma Rule é registrada e engolida: a regra seguinte roda de
    qualquer forma, e o daemon continua de pé.
    """
    executadas: list[str] = []
    for r in matching(rules, ctx.trigger):
        try:
            if r.when is not None and not r.when(ctx):
                continue
            resultado = r.fn(ctx)
            if inspect.isawaitable(resultado):
                await resultado
            executadas.append(r.name)
            log.info("regra %s executada por %s", r.name, ctx.trigger)
        except Exception:
            log.exception("regra %s falhou ao executar", r.name)
    return executadas
