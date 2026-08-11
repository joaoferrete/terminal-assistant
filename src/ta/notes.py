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

# O valor CANÔNICO, o que vai para o banco. Inglês, como `STATUSES` sempre foi:
# um schema com `status='todo'` ao lado de `priority='alta'` obriga quem lê o
# banco a saber duas línguas, e transformaria `TA_LANG` num bug — instruído a
# responder em inglês, o modelo devolve "high", a validação recusaria e a
# prioridade sumiria em silêncio (emenda do ADR 0006).
PRIORITIES = ("high", "medium", "low")

# Aceitos na CAPTURA, e para sempre. `!alta` foi a sintaxe por toda a vida do
# projeto e está na memória muscular de quem usa; quebrá-la não compraria nada.
# Idioma é coisa de entrada e de exibição — o valor gravado é um só.
PRIORITY_ALIASES = {
    "alta": "high", "media": "medium", "média": "medium", "baixa": "low",
    "high": "high", "medium": "medium", "low": "low",
}


def resolve_priority(token: str) -> str | None:
    """Normaliza o que foi digitado para o valor canônico. `None` se não for um."""
    return PRIORITY_ALIASES.get(token.lower())

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

# ── Vocabulário, por idioma ─────────────────────────────────────────────────
# Um idioma ATIVO por vez, escolhido por `TA_LANG` (ADR 0013). Os dois juntos
# tornariam `@03/04` ambíguo — 3 de abril em português, 4 de março na convenção
# americana — e qualquer lado escolhido estaria silenciosamente errado para
# metade dos usuários. Silenciosamente é a palavra: devolve data válida e errada.
#
# Sem acento porque ninguém acentua ao digitar às pressas.
WEEKDAYS_POR_LANG = {
    "pt": {
        "segunda": 0, "seg": 0,
        "terca": 1, "ter": 1,
        "quarta": 2, "qua": 2,
        "quinta": 3, "qui": 3,
        "sexta": 4, "sex": 4,
        "sabado": 5, "sab": 5,
        "domingo": 6, "dom": 6,
    },
    "en": {
        "monday": 0, "mon": 0,
        "tuesday": 1, "tue": 1, "tues": 1,
        "wednesday": 2, "wed": 2,
        "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
        "friday": 4, "fri": 4,
        "saturday": 5, "sat": 5,
        "sunday": 6, "sun": 6,
    },
}

MONTHS_POR_LANG = {
    "pt": {
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
    },
    "en": {
        "january": 1, "jan": 1,
        "february": 2, "feb": 2,
        "march": 3, "mar": 3,
        "april": 4, "apr": 4,
        "may": 5,
        "june": 6, "jun": 6,
        "july": 7, "jul": 7,
        "august": 8, "aug": 8,
        "september": 9, "sep": 9, "sept": 9,
        "october": 10, "oct": 10,
        "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    },
}

# "hoje aprendi X" é retrospectivo, não é prazo — e o regex sozinho não sabe
# disso, porque a diferença está no tempo verbal. As listas são curadas e curtas
# de propósito: pegam os casos frequentes com precisão alta em vez de tentar
# enumerar a conjugação de um idioma. O que escapar é corrigido pela segunda
# passada do LLM, que entende intenção (emenda do ADR 0003).
RETROSPECTIVOS_POR_LANG = {
    "pt": (
        "aprendi", "aprendemos", "descobri", "vi", "fiz", "fizemos", "tive", "tivemos",
        "foi", "fui", "consegui", "conseguimos", "aconteceu", "rolou", "deu", "terminei",
        "acabei", "resolvi", "entendi", "notei", "percebi", "li", "ouvi", "falei",
    ),
    "en": (
        "learned", "learnt", "found", "figured", "discovered", "saw", "did", "was",
        "were", "had", "got", "went", "finished", "shipped", "fixed", "solved",
        "realized", "realised", "noticed", "read", "heard", "talked", "met", "wrote",
    ),
}

# Sujeitos que podem aparecer entre a palavra de tempo e o verbo retrospectivo:
# "hoje EU aprendi", "today I learned".
SUJEITOS_POR_LANG = {
    "pt": r"(?:eu\s+|a\s+gente\s+|n[óo]s\s+)?",
    "en": r"(?:i\s+|we\s+)?",
}


def _lang() -> str:
    """Importado tarde para `notes` não depender de `config` no import."""
    from .i18n import lang

    return lang()


def weekdays() -> dict[str, int]:
    return WEEKDAYS_POR_LANG[_lang()]


def months() -> dict[str, int]:
    return MONTHS_POR_LANG[_lang()]

# ── Linguagem natural ───────────────────────────────────────────────────────
# As formas são ESTRUTURALMENTE diferentes entre os idiomas, não é questão de
# trocar palavras: português diz "17 de outubro" e "na sexta", inglês diz
# "October 17" e "next Friday". Cada idioma tem o seu conjunto, compilado uma vez.
#
# Os acentos entram como classe de caractere em vez de serem removidos do texto:
# normalizar mudaria os índices, e a extração das marcas depende deles.
def _compilar(lang: str) -> dict[str, re.Pattern]:
    meses = "|".join(sorted(MONTHS_POR_LANG[lang], key=len, reverse=True))
    dias = "|".join(sorted(WEEKDAYS_POR_LANG[lang], key=len, reverse=True))
    retro = "|".join(RETROSPECTIVOS_POR_LANG[lang])
    sujeito = SUJEITOS_POR_LANG[lang]

    if lang == "pt":
        dia_mes = (
            r"(?<!\S)(?P<d1>\d{1,2})\s+de\s+(?P<m1>[a-zç~ãâáéêíóôõú]+)"
            r"(?:\s+de\s+(?P<ano>\d{4}))?(?!\S)"
        )
        relativa = r"(?<!\S)(depois\s+de\s+amanh[ãa]|amanh[ãa]|hoje)(?!\S)"
        semana = (
            r"(?<!\S)(?:na|no|pr[óo]xim[ao])\s+"
            r"(segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)(?:-feira)?(?!\S)"
        )
        # Hora só conta com preposição (`às 8h`) ou junto de uma data. Sem isso,
        # "rodar 8h de bateria" viraria lembrete — o falso positivo mais provável.
        hora_prep = r"(?<!\S)[àa]s?\s+(\d{1,2})(?::(\d{2})|h(\d{2})?)?\s*(am|pm)?(?!\S)"
    else:
        # Inglês aceita as duas ordens porque as duas são correntes: "October 17"
        # e "17 October". `17th` também, que português não tem.
        dia_mes = (
            r"(?<!\S)(?:(?P<d1>\d{1,2})(?:st|nd|rd|th)?\s+(?P<m1>" + meses + r")"
            r"|(?P<m2>" + meses + r")\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?)"
            r"(?:,?\s+(?P<ano>\d{4}))?(?!\S)"
        )
        relativa = r"(?<!\S)(day\s+after\s+tomorrow|tomorrow|today|tonight)(?!\S)"
        semana = r"(?<!\S)(?:next|on|this)\s+(" + dias + r")(?!\S)"
        # `at 8`, `at 8:30`, `at 8pm`. O `pm` é o que português não precisa e
        # inglês não vive sem — e sem ele `8pm` não casava NADA, em silêncio,
        # porque o lookahead `(?!\S)` falhava no `p`.
        hora_prep = r"(?<!\S)at\s+(\d{1,2})(?::(\d{2}))?()\s*(am|pm)?(?!\S)"

    return {
        "dia_mes": re.compile(dia_mes, re.IGNORECASE),
        "relativa": re.compile(relativa, re.IGNORECASE),
        "semana": re.compile(semana, re.IGNORECASE),
        "hora_prep": re.compile(hora_prep, re.IGNORECASE),
        "retrospectivo": re.compile(
            r"\s+" + sujeito + r"(" + retro + r")(?!\w)", re.IGNORECASE
        ),
        # `8h`, `8:30`, `8pm` soltos — só valem junto de uma data.
        "hora_solta": re.compile(
            r"(?<!\S)(\d{1,2})(?::(\d{2})|h(\d{2})?)?\s*(am|pm)?(?!\S)"
            if lang == "en"
            else r"(?<!\S)(\d{1,2})(?::(\d{2})|h(\d{2})?)()(?!\S)",
            re.IGNORECASE,
        ),
    }


RE_POR_LANG = {lang: _compilar(lang) for lang in ("pt", "en")}


def _re(nome: str) -> re.Pattern:
    return RE_POR_LANG[_lang()][nome]

# `@@HH:MM` é a marca de lembrete. O paralelo com `@data` é intencional: `@` é o
# dia, `@@` é o dia com hora.
#
# A marca antiga era `!!HH:MM`, e ela **não funciona no zsh**: `!!` dispara
# expansão de histórico na leitura da linha e o comando morre inteiro com
# "zsh: no such word in event" — nem chega ao parser. Medido, não suposto.
# `!!` continua aceito porque no mural não há shell nenhum, e porque quebrar
# nota já escrita não compra nada.
RE_REMIND = re.compile(r"(?<!\S)(?:@@|!!)(\d{1,2}):(\d{2})(?!\S)")
# A alternância vem da tabela de apelidos, não de `PRIORITIES`: quem digita tem
# mais formas válidas do que o banco guarda. Ordenada da mais longa para a mais
# curta porque `media` é prefixo de `medium` — sem isso a alternância casaria o
# prefixo e voltaria só depois de backtracking, o que funciona mas depende de um
# detalhe do motor de regex em vez de estar escrito.
RE_PRIORITY = re.compile(
    r"(?<!\S)!(" + "|".join(sorted(PRIORITY_ALIASES, key=len, reverse=True)) + r")(?!\S)",
    re.IGNORECASE,
)
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

    # As palavras relativas não colidem entre os idiomas, então valem sempre:
    # `@today` numa máquina em português é inequívoco, e recusá-lo seria
    # pedantismo. O que NÃO pode valer nos dois é a data numérica, logo abaixo.
    if t in ("hoje", "today"):
        return today
    if t in ("amanha", "amanhã", "tomorrow"):
        return today + timedelta(days=1)
    if t in ("ontem", "yesterday"):
        return today - timedelta(days=1)

    # ISO completo
    try:
        return date.fromisoformat(token)
    except ValueError:
        pass

    # Data numérica. A ORDEM segue o idioma ativo, e é o motivo inteiro de a
    # decisão ser "um idioma por vez": `03/04` é 3 de abril em português e
    # 4 de março na convenção americana. Aceitar os dois faria este token
    # devolver uma data válida e errada, sem erro nenhum (ADR 0013).
    if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", token):
        primeiro, segundo, year = int(m[1]), int(m[2]), m[3]
        day, month = (segundo, primeiro) if _lang() == "en" else (primeiro, segundo)
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
    dias = weekdays()
    if t in dias:
        delta = (dias[t] - today.weekday()) % 7
        return today + timedelta(days=delta or 7)

    return None


def _nl_data(raw: str, today: date) -> date | None:
    """Data escrita no idioma corrente. None quando não há nenhuma."""
    if m := _re("dia_mes").search(raw):
        # Grupos nomeados nos dois idiomas, porque o inglês aceita as duas ordens
        # ("October 17" e "17 October") e o português só uma. Coalescer aqui é o
        # que deixa o resto do corpo idêntico para ambos.
        g = m.groupdict()
        bruto_dia, bruto_mes, ano_bruto = (
            g.get("d1") or g.get("d2"), g.get("m1") or g.get("m2"), g.get("ano")
        )
        mes = months().get(_sem_acento(bruto_mes)) if bruto_mes else None
        if mes is not None and bruto_dia:
            dia, ano = int(bruto_dia), int(ano_bruto) if ano_bruto else today.year
            try:
                achada = date(ano, mes, dia)
            except ValueError:
                achada = None
            if achada is not None:
                # Sem ano explícito e já passou: quis dizer o ano que vem.
                if ano_bruto is None and achada < today:
                    try:
                        achada = date(ano + 1, mes, dia)
                    except ValueError:
                        return None
                return achada

    if m := _re("relativa").search(raw):
        # Verbo retrospectivo logo depois da palavra? Então não é prazo:
        # "hoje aprendi X" e "today I learned X" são registro, não tarefa.
        if _re("retrospectivo").match(raw, m.end()):
            return None
        palavra = _sem_acento(re.sub(r"\s+", " ", m[1]))
        if palavra in ("hoje", "today", "tonight"):
            return today
        if palavra in ("amanha", "tomorrow"):
            return today + timedelta(days=1)
        if palavra in ("depois de amanha", "day after tomorrow"):
            return today + timedelta(days=2)

    if m := _re("semana").search(raw):
        alvo = weekdays().get(_sem_acento(m[1]))
        if alvo is not None:
            delta = (alvo - today.weekday()) % 7
            return today + timedelta(days=delta or 7)

    return None


def _nl_hora(raw: str, *, exige_preposicao: bool) -> time | None:
    """Hora escrita no idioma corrente.

    `exige_preposicao` é a guarda contra falso positivo: sem uma data no texto,
    só um "às"/"at" transforma um número em horário. "rodar 8h de bateria" não é
    compromisso, e "8 hours of battery" também não.
    """
    m = _re("hora_prep").search(raw)
    if m is None and not exige_preposicao:
        m = _re("hora_solta").search(raw)
        # Um número pelado NUNCA é hora. Sem esta guarda, "dentist on October 17"
        # ganhava lembrete às 17:00: o dia do mês era relido como hora, porque a
        # data já tinha sido achada e a preposição deixou de ser exigida.
        # Português não sofria disso — `8h` e `8:30` exigem `h` ou `:` na forma —,
        # e o inglês precisa da regra escrita porque `8pm` obriga o sufixo a ser
        # opcional no regex.
        if m is not None and not (m[2] or m[3] or (m.re.groups >= 4 and m[4])):
            return None
    if m is None:
        return None

    hh = int(m[1])
    mm = int(m[2] or m[3] or 0)

    # AM/PM. Português não usa e o grupo vem sempre vazio; inglês não vive sem, e
    # sem tratá-lo `8pm` não casava NADA — o lookahead `(?!\S)` falhava no `p` e
    # a marca era descartada em silêncio, que é o pior modo de falha possível.
    sufixo = (m[4] or "").lower() if m.re.groups >= 4 else ""
    if sufixo == "pm" and hh < 12:
        hh += 12
    elif sufixo == "am" and hh == 12:
        hh = 0   # 12am é meia-noite, não meio-dia

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
        # O que foi digitado pode ser `!alta` ou `!high`; o que é gravado é um só.
        priority = resolve_priority(m[1])
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
