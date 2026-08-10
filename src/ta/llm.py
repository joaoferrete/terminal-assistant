"""Gemini: as três chamadas de LLM do projeto.

Todas fora do caminho crítico (ADR 0003). Captura de nota, mural e Digest não
passam por aqui; sem chave ou sem rede, tudo isso continua inteiro.

Modelo escolhido a partir de `client.models.list()` na máquina, não de memória.
`gemini-flash-latest` é um **alias** que acompanha o flash atual — não apodrece
com o tempo, ao custo de poder mudar de comportamento sozinho. Para fixar,
`TA_GEMINI_MODEL=gemini-3.6-flash` no `.env`, uma linha.

Saída estruturada via `response_schema` com modelos Pydantic, conferido contra o
SDK instalado (google-genai 2.17.0) em vez de assumido.
"""

from __future__ import annotations

import logging
import os
from datetime import date

from pydantic import BaseModel, Field

from .notes import TAGS_SUGERIDAS

log = logging.getLogger("ta.llm")

DEFAULT_MODEL = "gemini-flash-latest"


class LLMUnavailable(RuntimeError):
    """Sem chave, sem SDK ou sem rede. Mensagem para o usuário ler."""


# ── Schemas de saída ────────────────────────────────────────────────────────
class NotePlacement(BaseModel):
    id: int = Field(description="id da nota")
    group: str = Field(description="nome curto do grupo temático")
    rank: int = Field(description="posição dentro do grupo, começando em 1")
    reason: str = Field(description="uma frase curta explicando a colocação")


class OrganizeResult(BaseModel):
    placements: list[NotePlacement]
    groups_in_order: list[str] = Field(
        description="os grupos, do mais urgente para o menos urgente"
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
    """A segunda passada sobre uma Note recém-capturada.

    Corrige o parser e classifica a intenção na **mesma** chamada: decidir se
    "hoje" é prazo e decidir se a nota é compromisso são a mesma pergunta sobre
    a frase, e duas idas ao modelo custariam o dobro pelo mesmo raciocínio.
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
    priority: str = Field(
        default="", description="'alta', 'media', 'baixa', ou vazio se não der para dizer"
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
        default="", description="uma frase curta em português dizendo o que mudou e por quê"
    )


class Prose(BaseModel):
    text: str


# ── Cliente ─────────────────────────────────────────────────────────────────
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
            raise LLMUnavailable(
                "GEMINI_API_KEY não está configurada. Confira o .env e "
                "`curl localhost:7777/health`."
            )
        try:
            from google import genai
        except ImportError as e:  # pragma: no cover - dependência declarada
            raise LLMUnavailable("SDK google-genai não está instalado.") from e
        self._client = genai.Client(api_key=self.api_key)
        return self._client

    async def _structured(self, prompt: str, schema: type[BaseModel], system: str = ""):
        """Uma chamada com saída validada contra o schema."""
        import asyncio

        from google.genai import types

        client = self._get()
        cfg = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=system or None,
        )
        try:
            resp = await asyncio.to_thread(
                client.models.generate_content, model=self.model, contents=prompt, config=cfg
            )
        except Exception as e:
            raise LLMUnavailable(f"Gemini falhou: {e}") from e

        # `parsed` é a instância validada; o SDK a preenche quando há schema.
        if getattr(resp, "parsed", None) is None:
            raise LLMUnavailable("Gemini respondeu fora do schema pedido.")
        return resp.parsed

    async def list_models(self) -> list[str]:
        import asyncio

        client = self._get()
        modelos = await asyncio.to_thread(lambda: list(client.models.list()))
        return [
            m.name.replace("models/", "")
            for m in modelos
            if "generateContent" in (m.supported_actions or [])
        ]

    # ── As três chamadas ────────────────────────────────────────────────────
    async def organize(self, notes: list[dict], priorities: str) -> OrganizeResult:
        """Agrupa e ordena. O resultado é GRAVADO pelo chamador (ADR 0003)."""
        linhas = "\n".join(
            f"- id={n['id']} | {n['text']}"
            f"{' | prazo ' + n['due'] if n.get('due') else ''}"
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
                "Agrupe por tema e ordene por urgência real, considerando prazo, "
                "prioridade declarada e o que a pessoa disse que importa. "
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
        """Extrai candidato a evento. Nunca cria nada — quem decide é o usuário."""
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
        contas: str = "",
    ) -> CaptureReview:
        """Relê uma Note capturada e corrige o que o regex não podia saber.

        O parser acerta a **forma** ("hoje" é uma data) e erra a **intenção**
        ("hoje aprendi X" não é prazo). Esta passada é o oposto: entende intenção
        e não precisa acertar formato, porque o schema já o impõe.
        """
        hoje = date.today()
        dia = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"][
            hoje.weekday()
        ]
        # Sem o perfil, "revisar o consumer do Kafka" obriga o modelo a adivinhar
        # que Kafka é trabalho. O contexto foi coletado na entrevista do `ta init`
        # e está gravado; não passá-lo era jogar fora a única informação que
        # resolve o roteamento pessoal/trabalho.
        contexto = f"Contexto de quem escreveu:\n{priorities}\n\n" if priorities else ""
        if contas:
            contexto += f"Contas disponíveis: {contas}.\n\n"

        return await self._structured(
            prompt=(
                f"Hoje é {hoje.isoformat()}, uma {dia}.\n"
                f'Nota capturada: "{text}"\n'
                f"O parser determinístico marcou prazo={due or 'nenhum'} e "
                f"lembrete={remind_at or 'nenhum'}.\n\n"
                f"{contexto}"
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
                f"({', '.join(TAGS_SUGERIDAS)}) e uma prioridade, usando o que o "
                "contexto diz sobre o que não pode cair e o que costuma ser adiado. "
                "Se não der para dizer a prioridade, deixe vazia em vez de chutar."
            ),
            schema=CaptureReview,
            system=(
                "Você revisa anotações soltas em português. Preserve o que o parser "
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
                "Escreva 2 a 4 frases sobre como o dia se apresenta, em português, "
                "direto ao ponto. Aponte o aperto se houver — reuniões coladas, "
                "tarefa vencida. Sem saudação e sem lista."
            ),
            schema=Prose,
            system="Você resume o dia de alguém em poucas frases úteis.",
        )
        return r.text
