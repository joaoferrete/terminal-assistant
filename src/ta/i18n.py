"""O idioma da ferramenta: uma decisão só, três superfícies.

`TA_LANG` governa o parser de captura, o texto do CLI e do mural, e o idioma em
que o modelo escreve prosa. São três coisas que **têm** que concordar: uma nota
capturada em português e exibida em inglês não é bilinguismo, é confusão.

Não confunda com o idioma da **documentação**, que é outra decisão e é fixa
(inglês, com um resumo em português). Esta aqui é do usuário e muda em runtime.

## Por que um idioma por vez, e não os dois juntos

`@03/04` é 3 de abril em português e 4 de março na convenção americana. Aceitar
os dois vocabulários ao mesmo tempo tornaria esse token ambíguo, e qualquer lado
escolhido estaria **silenciosamente errado** para metade dos usuários: não dá
erro, não fica sem marca, devolve uma data válida e errada, e a pessoa perde o
prazo. Com um idioma ativo por vez, a ordem numérica é uma consequência dele
(ADR 0013).

## Precedência

    TA_LANG  →  ~/.config/ta/config.toml  →  locale do SO  →  'en'

O locale vem antes do default justamente para que nada mude para quem já usa: numa
máquina em `pt_BR.UTF-8`, o idioma continua sendo português sem ninguém configurar
nada. E é por isso que a primeira execução não precisa perguntar.

A pergunta também **não pode** morar no `ta init`, que é a entrevista de
Priorities e exige LLM: a IA é opcional, e o idioma não é.
"""

from __future__ import annotations

import os
from functools import lru_cache

LANGS = ("pt", "en")
DEFAULT_LANG = "en"


def _do_locale() -> str | None:
    """O idioma do sistema, se for um dos suportados.

    Lê na ordem que o POSIX define: `LC_ALL` vence tudo, `LC_MESSAGES` vence
    `LANG`. Um locale presente mas não suportado (`de_DE`) devolve None e cai no
    default — adivinhar seria pior que assumir inglês.
    """
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        valor = os.environ.get(var)
        if not valor or valor in ("C", "POSIX"):
            continue
        codigo = valor.split(".")[0].split("_")[0].lower()
        return codigo if codigo in LANGS else None
    return None


@lru_cache(maxsize=1)
def _resolvido() -> tuple[str, str]:
    """(idioma, de onde veio). O segundo item é o que o `ta doctor` mostra.

    Dizer só "pt" não ajuda quem esperava inglês: a pergunta seguinte é sempre
    "por quê?", e a resposta está aqui.
    """
    if bruto := os.environ.get("TA_LANG"):
        codigo = bruto.split("_")[0].lower()
        if codigo in LANGS:
            return codigo, "TA_LANG"
        # Valor inválido não é silenciado: quem escreveu `TA_LANG=de` fez uma
        # escolha, e ignorá-la calado faz a ferramenta parecer quebrada.
        import logging

        logging.getLogger("ta").warning(
            "TA_LANG=%r não é um idioma suportado (%s); usando %s",
            bruto, ", ".join(LANGS), DEFAULT_LANG,
        )
        return DEFAULT_LANG, "TA_LANG inválido"

    from .config import _user_config

    do_arquivo = _user_config().get("lang")
    if isinstance(do_arquivo, str) and do_arquivo.lower() in LANGS:
        return do_arquivo.lower(), "config.toml"

    if do_sistema := _do_locale():
        return do_sistema, "locale do sistema"

    return DEFAULT_LANG, "padrão"


def lang() -> str:
    return _resolvido()[0]


def lang_source() -> str:
    return _resolvido()[1]


def reset_cache() -> None:
    """Para teste e para `ta lang`. Sem isto o primeiro `lang()` fixa o processo."""
    _resolvido.cache_clear()


# ── Catálogo ────────────────────────────────────────────────────────────────
# Uma chave por mensagem, os dois idiomas lado a lado. Lado a lado de propósito:
# um arquivo por idioma faz a tradução ficar para depois e ninguém percebe — com
# as duas na mesma linha, a falta é visível na hora de escrever.
#
# `test_i18n` garante que os dois dicionários têm exatamente as mesmas chaves.
# Sem esse pino, uma chave só em `pt` vira KeyError na máquina de quem usa `en`.
MENSAGENS: dict[str, dict[str, str]] = {
    # ── Erros de configuração e de disponibilidade ──────────────────────────
    "ai.sem_chave": {
        "pt": "A IA não está configurada. Defina GEMINI_API_KEY no .env — "
              "ou siga sem ela: captura, mural e automações não dependem de IA.",
        "en": "AI is not configured. Set GEMINI_API_KEY in your .env — "
              "or carry on without it: capture, board and automations never need AI.",
    },
    "ai.sem_sdk": {
        "pt": "O SDK google-genai não está instalado. Rode: pip install -e \".[dev]\"",
        "en": "The google-genai SDK is not installed. Run: pip install -e \".[dev]\"",
    },
    "ai.falhou": {
        "pt": "O modelo falhou: {erro}",
        "en": "The model failed: {erro}",
    },
    "ai.fora_do_schema": {
        "pt": "O modelo respondeu fora do formato pedido.",
        "en": "The model answered outside the requested schema.",
    },
    "home.sem_token": {
        "pt": "O Home Assistant não está configurado. Defina HA_TOKEN e HA_URL no .env.",
        "en": "Home Assistant is not configured. Set HA_TOKEN and HA_URL in your .env.",
    },
    "home.token_recusado": {
        "pt": "O Home Assistant recusou o token (401). Ele foi revogado?",
        "en": "Home Assistant rejected the token (401). Was it revoked?",
    },
    "daemon.fora_do_ar": {
        "pt": "O daemon não está de pé. Suba com: systemctl --user start ta",
        "en": "The daemon is not running. Start it with: systemctl --user start ta",
    },
    "daemon.desatualizado": {
        "pt": "O daemon está desatualizado — este mural é mais novo que ele.\n"
              "Rode: systemctl --user restart ta",
        "en": "The daemon is out of date — this board is newer than it is.\n"
              "Run: systemctl --user restart ta",
    },
    "auth.sem_credencial": {
        "pt": "credencial ausente ou inválida (TA_TOKEN)",
        "en": "missing or invalid credential (TA_TOKEN)",
    },
    "auth.mural": {
        "pt": "Este mural está aberto para a rede e a credencial não confere.\n"
              "Abra pelo endereço com ?token=… que está no seu .env (TA_TOKEN).",
        "en": "This board is open to the network and the credential does not match.\n"
              "Open it with the ?token=… address from your .env (TA_TOKEN).",
    },
    # ── Horizonte, status e prioridade ─────────────────────────────────────
    "horizon.overdue": {"pt": "vencidas", "en": "overdue"},
    "horizon.today": {"pt": "hoje", "en": "today"},
    "horizon.week": {"pt": "próximos 7 dias", "en": "next 7 days"},
    "horizon.later": {"pt": "depois e sem prazo", "en": "later and undated"},
    "export.title": {"pt": "Notas", "en": "Notes"},
    "export.due": {"pt": "prazo", "en": "due"},
    "export.reminds": {"pt": "lembra", "en": "reminds"},
    "export.priority": {"pt": "prio", "en": "prio"},
    "status.todo": {"pt": "Não feito", "en": "To do"},
    "status.doing": {"pt": "Em andamento", "en": "Doing"},
    "status.hold": {"pt": "Em hold", "en": "On hold"},
    "status.done": {"pt": "Concluída", "en": "Done"},
    "status.cancelled": {"pt": "Cancelada", "en": "Cancelled"},
    "priority.high": {"pt": "alta", "en": "high"},
    "priority.medium": {"pt": "media", "en": "medium"},
    "priority.low": {"pt": "baixa", "en": "low"},
    # ── Mural ───────────────────────────────────────────────────────────────
    "board.titulo": {"pt": "Mural", "en": "Board"},
    "board.carregando": {"pt": "carregando…", "en": "loading…"},
    "board.vazio": {"pt": "nenhuma nota ainda", "en": "no notes yet"},
    "board.nao_carregou": {"pt": "não deu para carregar", "en": "could not load"},
    "board.fora_da_fila": {"pt": "fora da fila", "en": "out of the queue"},
    "board.concluidas": {"pt": "concluídas", "en": "done"},
    "board.lixeira": {"pt": "lixeira", "en": "trash"},
    "board.lixeira_vazia": {"pt": "a lixeira está vazia", "en": "the trash is empty"},
    "board.captura": {
        "pt": "nota nova — ex: ligar dentista @sexta #saude !alta @@09:00",
        "en": "new note — e.g. call dentist @friday #health !high @@09:00",
    },
    "board.revisar": {"pt": "revisar tudo", "en": "review all"},
    "board.confirmar": {"pt": "confirmar?", "en": "confirm?"},
    "board.falhou": {"pt": "falhou", "en": "failed"},
    "board.apagar": {"pt": "apagar?", "en": "delete?"},
    "board.esvaziar": {"pt": "esvaziar lixeira", "en": "empty trash"},
    "board.restaurar": {"pt": "restaurar", "en": "restore"},
    "board.vencida": {"pt": "vencida ", "en": "overdue "},
    "board.prazo": {"pt": "prazo ", "en": "due "},
    "board.lembra": {"pt": "lembra ", "en": "reminds "},
    "board.view.geral": {"pt": "geral", "en": "board"},
    "board.view.lista": {"pt": "lista", "en": "list"},
    "board.view.kanban": {"pt": "kanban", "en": "kanban"},
    "board.filtro.area": {"pt": "todas as áreas", "en": "all areas"},
    "board.filtro.tipo": {"pt": "todos os tipos", "en": "all types"},
    "board.filtro.tag": {"pt": "todas as tags", "en": "all tags"},
    "board.contador": {"pt": "nota(s)", "en": "note(s)"},
    "board.na_lixeira": {
        "pt": "na lixeira — `↩` restaura, ou volte ao mural para escrever",
        "en": "in the trash — `↩` restores; go back to the board to write",
    },
    # ── CLI ─────────────────────────────────────────────────────────────────
    "cli.nada_cobravel": {
        "pt": "nada cobrável hoje.",
        "en": "nothing due today.",
    },
    "cli.tarefas": {"pt": "tarefas", "en": "tasks"},
    "cli.compromissos": {"pt": "compromissos", "en": "events"},
    "cli.atrasada": {"pt": "ATRASADA", "en": "OVERDUE"},
    "cli.sem_notas": {"pt": "nenhuma nota.", "en": "no notes."},
    "cli.apagada": {"pt": "apagada", "en": "deleted"},
    "cli.restaurada": {"pt": "restaurada", "en": "restored"},
    "cli.desfaz": {"pt": "desfaz", "en": "undoes it"},
}


def t(chave: str, **fmt: object) -> str:
    """A mensagem no idioma ativo.

    Chave desconhecida devolve a própria chave em vez de estourar: uma mensagem
    feia é melhor que um `KeyError` no meio de um comando que ia funcionar.
    """
    entrada = MENSAGENS.get(chave)
    if entrada is None:
        return chave
    texto = entrada.get(lang()) or entrada[DEFAULT_LANG]
    return texto.format(**fmt) if fmt else texto


def catalogo(prefixo: str = "") -> dict[str, str]:
    """O catálogo do idioma ativo, para o mural receber pronto.

    O JS não tem tabela paralela — mesma disciplina do `HORIZON_LABEL`, que já é
    o único lugar do mural que nomeia faixas. Uma segunda tradução vivendo no
    cliente seria uma tradução que nenhum teste compara com esta.
    """
    return {
        k: t(k) for k in MENSAGENS if k.startswith(prefixo)
    }
