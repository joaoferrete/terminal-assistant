"""Captura de Notes: parser determinístico.

Este módulo é o caminho crítico da decisão 3 (ADR 0003): despejar um pensamento
não pode esperar rede. Nada aqui faz I/O, chama LLM ou consulta relógio de rede.
Texto sem nenhuma marca continua sendo uma Note válida — a sintaxe é opcional,
sempre.

    #tag           tema
    !alta          prioridade (alta | media | baixa)
    @sexta         prazo  -> papel Task
    @@22:00        disparo -> papel Reminder  (`!!` também, mas quebra no zsh)

Além das marcas, o parser lê **data e hora escritas em português corrente**:
"Revisar o PR do Ana hoje" ganha prazo de hoje, e "ir na nutricionista 17 de
setembro às 8:30" ganha prazo e lembrete. Isto é deliberadamente determinístico e
não usa LLM: prazo é caminho crítico, e um prazo que depende de rede é um prazo
que falha no avião.

Duas regras que evitam surpresa:

1. **Marca explícita sempre vence.** A linguagem natural só preenche o que ficou
   vazio, então `@sexta` num texto que também diz "hoje" resolve para sexta.
2. **O texto não é mutilado.** Diferente das marcas, que são extraídas, a
   expressão em português fica onde está — "Revisar o PR do Ana hoje" continua
   lendo como uma frase.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

PRIORITIES = ("alta", "media", "baixa")

# Vocabulário fechado para a revisão escolher. Fechado de propósito: se o modelo
# pudesse inventar tag, cada nota ganharia um tema quase-igual ("trabalho",
# "profissional", "job") e o agrupamento visual do mural perderia o sentido —
# que é justamente o motivo de existir a tag automática.
#
# Você continua livre para escrever `#qualquer-coisa` à mão: a lista restringe o
# LLM, não você.
# `trabalho` e `pessoal` são o **eixo**: a revisão sempre marca um dos dois, e é
# por ele que o mural filtra. Os outros são temas, e entram no máximo dois.
EIXO = ("trabalho", "pessoal")

# Terceiro eixo: o que a nota É. Vem do `intent` da revisão, e vira tag para
# poder ser filtrado e visto no post-it como qualquer outra.
#
# `anotacao` é o caso que justifica o eixo existir: registro e ideia solta não
# têm prazo nem urgência, e pedir prioridade para elas só suja o mural.
TIPOS = ("tarefa", "compromisso", "anotacao")

TAGS_SUGERIDAS = (
    "trabalho",
    "pessoal",
    "saude",
    "casa",
    "estudo",
    "financeiro",
    "compras",
    "familia",
    "ideia",
)


def _sem_acento(s: str) -> str:
    """Para comparar palavra escrita com e sem acento. Não toca no texto da Note."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(c)
    )

# Dias da semana em português, sem acento (o usuário não vai acentuar ao digitar).
WEEKDAYS = {
    "segunda": 0, "seg": 0,
    "terca": 1, "ter": 1,
    "quarta": 2, "qua": 2,
    "quinta": 3, "qui": 3,
    "sexta": 4, "sex": 4,
    "sabado": 5, "sab": 5,
    "domingo": 6, "dom": 6,
}

MONTHS = {
    "janeiro": 1, "jan": 1,
    "fevereiro": 2, "fev": 2,
    "marco": 3, "mar": 3,
    "abril": 4, "abr": 4,
    "maio": 5,
    "junho": 6, "jun": 6,
    "julho": 7, "jul": 7,
    "agosto": 8, "ago": 8,
    "setembro": 9, "set": 9,
    "outubro": 10, "out": 10,
    "novembro": 11, "nov": 11,
    "dezembro": 12, "dez": 12,
}

# ── Linguagem natural ───────────────────────────────────────────────────────
# Os acentos entram como classe de caractere em vez de serem removidos do texto:
# normalizar mudaria os índices, e a extração das marcas depende deles.
RE_NL_DIA_MES = re.compile(
    r"(?<!\S)(\d{1,2})\s+de\s+([a-zç~ãâáéêíóôõú]+)(?:\s+de\s+(\d{4}))?(?!\S)", re.IGNORECASE
)
RE_NL_RELATIVA = re.compile(
    r"(?<!\S)(depois\s+de\s+amanh[ãa]|amanh[ãa]|hoje)(?!\S)", re.IGNORECASE
)

# "hoje aprendi X" é retrospectivo, não é prazo — e o regex sozinho não sabe
# disso, porque a diferença está no tempo verbal. Esta lista é curada e curta de
# propósito: pega os casos frequentes com precisão alta em vez de tentar
# enumerar a conjugação do português. O que escapar é corrigido pela segunda
# passada do LLM, que entende intenção (emenda do ADR 0003).
RETROSPECTIVOS = (
    "aprendi", "aprendemos", "descobri", "vi", "fiz", "fizemos", "tive", "tivemos",
    "foi", "fui", "consegui", "conseguimos", "aconteceu", "rolou", "deu", "terminei",
    "acabei", "resolvi", "entendi", "notei", "percebi", "li", "ouvi", "falei",
)
RE_RETROSPECTIVO = re.compile(
    r"\s+(?:eu\s+|a\s+gente\s+|n[óo]s\s+)?(" + "|".join(RETROSPECTIVOS) + r")(?!\w)",
    re.IGNORECASE,
)
RE_NL_SEMANA = re.compile(
    r"(?<!\S)(?:na|no|pr[óo]xim[ao])\s+"
    r"(segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)(?:-feira)?(?!\S)",
    re.IGNORECASE,
)
# Hora só conta com preposição (`às 8h`) ou junto de uma data. Sem isso, "rodar
# 8h de bateria" viraria lembrete — o falso positivo mais provável de todos.
RE_NL_HORA_PREP = re.compile(
    r"(?<!\S)[àa]s?\s+(\d{1,2})(?::(\d{2})|h(\d{2})?)?(?!\S)", re.IGNORECASE
)
RE_NL_HORA_SOLTA = re.compile(r"(?<!\S)(\d{1,2})(?::(\d{2})|h(\d{2})?)(?!\S)", re.IGNORECASE)

# `@@HH:MM` é a marca de lembrete. O paralelo com `@data` é intencional: `@` é o
# dia, `@@` é o dia com hora.
#
# A marca antiga era `!!HH:MM`, e ela **não funciona no zsh**: `!!` dispara
# expansão de histórico na leitura da linha e o comando morre inteiro com
# "zsh: no such word in event" — nem chega ao parser. Medido, não suposto.
# `!!` continua aceito porque no mural não há shell nenhum, e porque quebrar
# nota já escrita não compra nada.
RE_REMIND = re.compile(r"(?<!\S)(?:@@|!!)(\d{1,2}):(\d{2})(?!\S)")
RE_PRIORITY = re.compile(r"(?<!\S)!(" + "|".join(PRIORITIES) + r")(?!\S)", re.IGNORECASE)
RE_TAG = re.compile(r"(?<!\S)#([\w-]+)(?!\S)", re.UNICODE)
# `/` entra na classe para aceitar @25/12 — sem ele, `\w` para no `2` de `25` e
# o lookahead falha, descartando a marca inteira em silêncio.
RE_DUE = re.compile(r"(?<!\S)@([\w/-]+)(?!\S)", re.UNICODE)


@dataclass
class ParsedNote:
    """O resultado da captura. Os papéis são derivados, nunca escolhidos."""

    text: str
    tags: list[str] = field(default_factory=list)
    priority: str | None = None
    due: date | None = None
    remind_at: datetime | None = None

    @property
    def is_task(self) -> bool:
        return self.due is not None

    @property
    def is_reminder(self) -> bool:
        return self.remind_at is not None


def _resolve_date(token: str, today: date) -> date | None:
    """Resolve um token de @data. Devolve None se não reconhecer.

    Não reconhecer é um resultado legítimo: `@casa` não é data, e a marca volta
    para o texto em vez de virar erro.
    """
    t = token.lower()

    if t in ("hoje", "today"):
        return today
    if t in ("amanha", "amanhã", "tomorrow"):
        return today + timedelta(days=1)
    if t in ("ontem",):
        return today - timedelta(days=1)

    # ISO completo
    try:
        return date.fromisoformat(token)
    except ValueError:
        pass

    # DD/MM ou DD/MM/AAAA
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", token):
        day, month, year = int(m[1]), int(m[2]), m[3]
        y = today.year if year is None else (2000 + int(year) if len(year) == 2 else int(year))
        try:
            candidate = date(y, month, day)
        except ValueError:
            return None
        # Sem ano explícito e já passou: o usuário quer o ano que vem.
        if year is None and candidate < today:
            try:
                candidate = date(y + 1, month, day)
            except ValueError:
                return None
        return candidate

    # Dia da semana: sempre o próximo, nunca hoje. "@sexta" numa sexta significa
    # a que vem — se fosse hoje, o usuário teria escrito "@hoje".
    if t in WEEKDAYS:
        delta = (WEEKDAYS[t] - today.weekday()) % 7
        return today + timedelta(days=delta or 7)

    return None


def _nl_data(raw: str, today: date) -> date | None:
    """Data escrita em português corrente. None quando não há nenhuma."""
    if m := RE_NL_DIA_MES.search(raw):
        mes = MONTHS.get(_sem_acento(m[2]))
        if mes is not None:
            dia = int(m[1])
            ano = int(m[3]) if m[3] else today.year
            try:
                achada = date(ano, mes, dia)
            except ValueError:
                achada = None
            if achada is not None:
                # Sem ano explícito e já passou: quis dizer o ano que vem.
                if m[3] is None and achada < today:
                    try:
                        achada = date(ano + 1, mes, dia)
                    except ValueError:
                        return None
                return achada

    if m := RE_NL_RELATIVA.search(raw):
        # Verbo retrospectivo logo depois da palavra? Então não é prazo.
        if RE_RETROSPECTIVO.match(raw, m.end()):
            return None
        palavra = _sem_acento(re.sub(r"\s+", " ", m[1]))
        if palavra == "hoje":
            return today
        if palavra == "amanha":
            return today + timedelta(days=1)
        if palavra == "depois de amanha":
            return today + timedelta(days=2)

    if m := RE_NL_SEMANA.search(raw):
        alvo = WEEKDAYS.get(_sem_acento(m[1]))
        if alvo is not None:
            delta = (alvo - today.weekday()) % 7
            return today + timedelta(days=delta or 7)

    return None


def _nl_hora(raw: str, *, exige_preposicao: bool) -> time | None:
    """Hora escrita em português corrente.

    `exige_preposicao` é a guarda contra falso positivo: sem uma data no texto,
    só um "às" transforma um número em horário. "rodar 8h de bateria" não é
    compromisso.
    """
    m = RE_NL_HORA_PREP.search(raw)
    if m is None and not exige_preposicao:
        m = RE_NL_HORA_SOLTA.search(raw)
    if m is None:
        return None

    hh = int(m[1])
    mm = int(m[2] or m[3] or 0)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)


def parse(raw: str, *, now: datetime | None = None) -> ParsedNote:
    """Extrai atributos do texto cru.

    `now` é injetável para que os testes não dependam do dia em que rodam.
    """
    now = now or datetime.now()
    today = now.date()

    tags: list[str] = []
    priority: str | None = None
    due: date | None = None
    remind_at: datetime | None = None
    consumed: list[tuple[int, int]] = []

    if m := RE_REMIND.search(raw):
        hh, mm = int(m[1]), int(m[2])
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            at = datetime.combine(today, time(hh, mm))
            # Horário já passado significa amanhã: ninguém agenda para o passado.
            remind_at = at if at > now else at + timedelta(days=1)
            consumed.append(m.span())

    if m := RE_PRIORITY.search(raw):
        priority = m[1].lower()
        consumed.append(m.span())

    for m in RE_TAG.finditer(raw):
        tags.append(m[1].lower())
        consumed.append(m.span())

    for m in RE_DUE.finditer(raw):
        if (resolved := _resolve_date(m[1], today)) is not None:
            due = resolved
            consumed.append(m.span())
        # Token não-data (@casa) fica no texto de propósito.

    # Linguagem natural entra por último, e só onde a marca explícita não falou.
    # O texto NÃO é alterado: a expressão em português faz parte da frase.
    if due is None:
        due = _nl_data(raw, today)

    if remind_at is None:
        hora = _nl_hora(raw, exige_preposicao=due is None)
        if hora is not None:
            base = due or today
            remind_at = datetime.combine(base, hora)
            # Sem data no texto, um horário que já passou é amanhã — a mesma
            # regra do `!!HH:MM`, para os dois caminhos não divergirem.
            if due is None and remind_at <= now:
                remind_at += timedelta(days=1)

    # Remover as marcas de trás para frente, para não invalidar os índices.
    text = raw
    for start, end in sorted(consumed, reverse=True):
        text = text[:start] + text[end:]

    return ParsedNote(
        text=re.sub(r"\s{2,}", " ", text).strip(),
        tags=sorted(set(tags)),
        priority=priority,
        due=due,
        remind_at=remind_at,
    )
