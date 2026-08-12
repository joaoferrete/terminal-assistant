"""Gemini: the project's three LLM calls.

All of them off the critical path (ADR 0003). Note capture, the board and the
Digest never come through here; with no key or no network, all of that is intact.

The model is chosen from `client.models.list()` on the machine, not from memory.
`gemini-flash-latest` is an **alias** that follows the current flash — it does not
go stale, at the cost of being able to change behaviour on its own. To pin it,
`TA_GEMINI_MODEL=gemini-3.6-flash` in `.env`, one line.

Structured output through `response_schema` with Pydantic models, checked against
the installed SDK rather than assumed.

NOTE ON LANGUAGE: the prompt bodies and the `description=` of every schema field
stay in Portuguese, and that is deliberate (ADR 0013). They are not code — they
are text sent to the model, and rewriting them would change its behaviour with no
way to compare before and after without burning calls. What `TA_LANG` changes is
the language the model **answers** in, through a system instruction in one place.
"""

from __future__ import annotations

import logging
import os
from datetime import date

from pydantic import BaseModel, Field

from . import i18n
from .notes import SUGGESTED_TAGS

log = logging.getLogger("ta.llm")

DEFAULT_MODEL = "gemini-flash-latest"


class LLMUnavailable(RuntimeError):
    """No key, no SDK or no network. The message is for a human to read."""


# ── Output schemas ──────────────────────────────────────────────────────────
class NotePlacement(BaseModel):
    id: int = Field(description="id da nota")
    group: str = Field(description="nome curto do grupo temático")
    rank: int = Field(description="posição dentro do grupo, começando em 1")
    reason: str = Field(description="uma frase curta explicando a colocação")


class OrganizeResult(BaseModel):
    placements: list[NotePlacement]
    groups_in_order: list[str] = Field(
        description="os grupos, do que merece atenção primeiro para o que pode esperar"
    )


class EventCandidate(BaseModel):
    is_event: bool = Field(description="a nota descreve um compromisso com data e hora?")
    title: str = Field(default="", description="título curto para a agenda")
    start: str = Field(default="", description="ISO 8601 local, ex 2026-08-14T14:00")
    end: str = Field(default="", description="ISO 8601 local; padrão 1h depois do início")
    account: str = Field(
        default="pessoal", description="'pessoal' ou 'trabalho', pelo contexto da nota"
    )
    confidence: float = Field(default=0.0, description="0 a 1")


class CaptureReview(BaseModel):
    """The second pass over a freshly captured Note.

    It corrects the parser and classifies the intent in the **same** call:
    deciding whether "today" is a deadline and deciding whether the note is an
    appointment are the same question about the sentence, and two round trips to
    the model would cost double for the same reasoning.
    """

    intent: str = Field(
        description="'tarefa' se há algo a fazer, 'compromisso' se é hora marcada, "
        "'anotacao' se é registro ou ideia sem cobrança"
    )
    due: str = Field(
        default="", description="prazo correto em ISO (2026-08-14), ou vazio para NENHUM"
    )
    remind_at: str = Field(
        default="", description="lembrete correto em ISO (2026-08-14T08:30), ou vazio para NENHUM"
    )
    # A canonical value, independent of the answer's language: it is a structured
    # field that goes to the database, not text for a human (amendment to ADR 0006).
    priority: str = Field(
        default="", description="'high', 'medium', 'low', ou vazio se não der para dizer"
    )
    tags: list[str] = Field(
        default_factory=list, description="1 a 2 temas, SOMENTE da lista oferecida"
    )
    is_event: bool = Field(default=False, description="deve entrar na agenda?")
    title: str = Field(default="", description="título curto para a agenda")
    start: str = Field(default="", description="ISO 8601 local")
    end: str = Field(default="", description="ISO 8601 local; padrão 1h depois")
    account: str = Field(default="pessoal", description="'pessoal' ou 'trabalho'")
    confidence: float = Field(default=0.0, description="0 a 1")
    reason: str = Field(
        default="", description="uma frase curta dizendo o que mudou e por quê"
    )


class Prose(BaseModel):
    text: str


# ── Client ──────────────────────────────────────────────────────────────────
class LLM:
    def __init__(self, api_key: str | None, model: str | None = None) -> None:
        self.api_key = api_key
        self.model = model or os.environ.get("TA_GEMINI_MODEL", DEFAULT_MODEL)
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise LLMUnavailable(i18n.t("ai.no_key"))
        try:
            from google import genai
        except ImportError as e:  # pragma: no cover - dependência declarada
            raise LLMUnavailable(i18n.t("ai.no_sdk")) from e
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    # ── Output language ─────────────────────────────────────────────────────
    # What changes with `TA_LANG` is the language the model ANSWERS in, and that
    # fits in a system instruction, in one place. See the module docstring for why
    # the prompts themselves stay put.
    #
    # Only prose is translated. A structured field (`priority`, `status`) is
    # canonical: without that distinction, review would return "alta" under
    # `TA_LANG=pt`, validation would reject it for not being in the enum, and the
    # priority would vanish silently (amendment to ADR 0006).
    OUTPUT_LANGUAGE = {
        "pt": "Escreva todo texto livre em português do Brasil.",
        "en": "Write all free text in English.",
    }

    def _system(self, system: str = "") -> str:
        from .i18n import lang

        rule = (
            f"{self.OUTPUT_LANGUAGE[lang()]} "
            "Isso vale para prosa e para nomes de grupo, NUNCA para campos de "
            "valor fixo como prioridade ou status, que têm um vocabulário próprio "
            "definido no schema."
        )
        return f"{system}\n\n{rule}".strip()

    async def _structured(self, prompt: str, schema: type[BaseModel], system: str = ""):
        """One call with output validated against the schema."""
        import asyncio

        from google.genai import types

        client = self._get()
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=self._system(system),
        )
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content, model=self.model, contents=prompt, config=cfg
            )
        except Exception as e:
            raise LLMUnavailable(i18n.t("ai.failed", erro=e)) from e

        # `parsed` is the validated instance; the SDK fills it when there is a schema.
        if getattr(resp, "parsed", None) is None:
            raise LLMUnavailable(i18n.t("ai.off_schema"))
        return resp.parsed

    async def list_models(self) -> list[str]:
        import asyncio

        client = self._get()
        models = await asyncio.to_thread(lambda: list(client.models.list()))
        return [
            m.name.replace("models/", "")
            for m in models
            if "generateContent" in (m.supported_actions or [])
        ]

    # ── The three calls ─────────────────────────────────────────────────────
    async def organize(self, notes: list[dict], priorities: str) -> OrganizeResult:
        """Group and order. The result is STORED by the caller (ADR 0003).

        The deadline is **not** this call's business. The horizon band is derived
        from the clock and comes ahead of everything at display time (ADR 0010),
        so asking the model for urgency was asking it to compete with a rule that
        always beats it. What is left is what only it can do: group by theme and
        see what unblocks what.
        """
        linhas = "\n".join(
            f"- id={n['id']} | {n['text']}"
            f"{' | prazo ' + n['due'] if n.get('due') else ''}"
            f"{' | faixa ' + n['horizon'] if n.get('horizon') else ''}"
            f"{' | prio ' + n['priority'] if n.get('priority') else ''}"
            f"{' | tags ' + ','.join(n['tags']) if n.get('tags') else ''}"
            f"{' | fixada pelo usuário' if n.get('pinned_by_user') else ''}"
            for n in notes
        )
        return await self._structured(
            prompt=(
                f"Hoje é {date.today().isoformat()}.\n\n"
                f"O que importa para esta pessoa:\n{priorities or '(não informado)'}\n\n"
                f"Notas:\n{linhas}\n\n"
                "Agrupe por tema. **Não ordene por prazo**: a faixa de cada nota "
                "(vencida, hoje, semana, depois) já está resolvida e vem antes da "
                "sua ordem na hora de mostrar — ordenar por prazo aqui não muda "
                "nada. Ordene pelo que a sua ordem decide de fato: o que desbloqueia "
                "outra coisa vem antes do que não desbloqueia nada, e o que a pessoa "
                "disse que importa vem antes do que ela disse que costuma adiar. "
                "Notas marcadas como fixadas pelo usuário devem manter a posição "
                "relativa que já têm — a mão dela vence a sua."
            ),
            schema=OrganizeResult,
            system=(
                "Você organiza a lista de tarefas de uma pessoa. Seja conservador: "
                "poucos grupos, nomes curtos, sem inventar tarefa que não está na lista."
            ),
        )

    async def detect_event(self, text: str) -> EventCandidate:
        """Extract an event candidate. It never creates anything — the user decides."""
        return await self._structured(
            prompt=(
                f"Hoje é {date.today().isoformat()}, um "
                f"{['segunda','terça','quarta','quinta','sexta','sábado','domingo'][date.today().weekday()]}.\n"
                f'Nota: "{text}"\n\n'
                "Isto descreve um compromisso com data e hora? Se sim, extraia. "
                "Se for tarefa sem hora marcada, ou só uma ideia, is_event é falso. "
                "Para a conta: use 'trabalho' se o contexto for profissional, "
                "'pessoal' caso contrário."
            ),
            schema=EventCandidate,
            system=(
                "Você extrai compromissos de anotações soltas. Na dúvida sobre data ou "
                "hora, devolva confidence baixa em vez de adivinhar."
            ),
        )

    async def review_capture(
        self,
        text: str,
        *,
        due: str | None,
        remind_at: str | None,
        priorities: str = "",
        accounts: str = "",
    ) -> CaptureReview:
        """Re-read a captured Note and correct what the regex could not know.

        The parser gets the **form** right ("today" is a date) and the
        **intention** wrong ("today I learned X" is not a deadline). This pass is
        the opposite: it understands intent and does not need to get the format
        right, because the schema already enforces it.
        """
        today = date.today()
        # The weekday name is prompt content, so it stays Portuguese along with
        # the rest of the prompt (see the module docstring).
        weekday_name = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][
            today.weekday()
        ]
        # Without the profile, "review the Kafka consumer" forces the model to
        # guess that Kafka is work. The context was collected in the `ta init`
        # interview and is stored; not passing it threw away the one piece of
        # information that resolves personal/work routing.
        context = f"Contexto de quem escreveu:\n{priorities}\n\n" if priorities else ""
        if accounts:
            context += f"Contas disponíveis: {accounts}.\n\n"

        return await self._structured(
            prompt=(
                f"Hoje é {today.isoformat()}, uma {weekday_name}.\n"
                f'Nota capturada: "{text}"\n'
                f"O parser determinístico marcou prazo={due or 'nenhum'} e "
                f"lembrete={remind_at or 'nenhum'}.\n\n"
                f"{context}"
                "Revise. O parser lê datas por padrão de texto e não entende intenção, "
                "então ele erra em frases retrospectivas: 'hoje aprendi X' é um registro "
                "do que já aconteceu, não um prazo — nesse caso devolva due e remind_at "
                "vazios. Se a nota descreve compromisso com hora marcada (consulta, "
                "reunião, aula), intent é 'compromisso' e is_event é verdadeiro. "
                "Tarefa com prazo mas sem hora de início não é compromisso. "
                "Para a conta, use 'trabalho' quando o assunto pertencer ao trabalho "
                "descrito no contexto acima, e 'pessoal' caso contrário.\n\n"
                "O campo `intent` também vira etiqueta no mural, então escolha com "
                "cuidado: 'anotacao' para registro e ideia solta, que não recebem "
                "prioridade nenhuma; 'tarefa' para o que tem de ser feito; "
                "'compromisso' para hora marcada.\n"
                f"Classifique também: escolha 1 ou 2 tags EXCLUSIVAMENTE desta lista "
                f"({', '.join(SUGGESTED_TAGS)}) e uma prioridade, usando o que o "
                "contexto diz sobre o que não pode cair e o que costuma ser adiado. "
                "Se não der para dizer a prioridade, deixe vazia em vez de chutar."
            ),
            schema=CaptureReview,
            system=(
                "Você revisa anotações soltas escritas pelo usuário. Preserve o que o parser "
                "acertou e corrija só o que está errado. Na dúvida sobre data, hora ou "
                "intenção, devolva confidence baixa em vez de adivinhar — uma correção "
                "errada é pior que nenhuma."
            ),
        )

    async def digest_prose(self, events: list[dict], tasks: list[dict]) -> str:
        """Prosa do dia. Enfeite opcional: a listagem determinística é o padrão."""
        ev = "\n".join(f"- {e['start'][11:]} {e['summary']}" for e in events) or "(nada)"
        tk = "\n".join(f"- {t['text']}" for t in tasks) or "(nada)"
        r = await self._structured(
            prompt=(
                f"Compromissos de hoje:\n{ev}\n\nTarefas cobráveis:\n{tk}\n\n"
                "Escreva 2 a 4 frases sobre como o dia se apresenta, "
                "direto ao ponto. Aponte o aperto se houver — reuniões coladas, "
                "tarefa vencida. Sem saudação e sem lista."
            ),
            schema=Prose,
            system="Você resume o dia de alguém em poucas frases úteis.",
        )
        return r.text
