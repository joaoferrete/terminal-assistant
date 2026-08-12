"""The tool's language: one decision, three surfaces.

`TA_LANG` governs the capture parser, the CLI and board text, and the language
the model writes prose in. Those three **have** to agree: a note captured in
Portuguese and displayed in English is not bilingualism, it is confusion.

Do not confuse this with the language of the **documentation**, which is a
separate and fixed decision (English, with a Portuguese summary). This one
belongs to the user and changes at runtime.

## Why one language at a time, and not both

`@03/04` is 3 April in Portuguese and 4 March in the American convention.
Accepting both vocabularies at once would make that token ambiguous, and
whichever side was chosen would be **silently wrong** for half the users: no
error, no missing mark, a valid and wrong date, and a missed deadline. With one
active language, the numeric order is a consequence of it (ADR 0013).

## Precedence

    TA_LANG  →  ~/.config/ta/config.toml  →  the system locale  →  'en'

The locale comes before the default precisely so nothing changes for someone
already using the tool: on a `pt_BR.UTF-8` machine the language stays Portuguese
with nothing configured. That is also why the first run does not need to ask.

The question also **cannot** live in `ta init`, which is the Priorities interview
and requires an LLM: the AI is optional, and the language is not.
"""

from __future__ import annotations

import os
from functools import lru_cache

LANGS = ("pt", "en")
DEFAULT_LANG = "en"


def _from_locale() -> str | None:
    """The system language, if it is one of the supported ones.

    Read in the order POSIX defines: `LC_ALL` beats everything, `LC_MESSAGES`
    beats `LANG`. A locale that is present but unsupported (`de_DE`) returns None
    and falls to the default — guessing would be worse than assuming English.
    """
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if not value or value in ("C", "POSIX"):
            continue
        code = value.split(".")[0].split("_")[0].lower()
        return code if code in LANGS else None
    return None


@lru_cache(maxsize=1)
def _resolved() -> tuple[str, str]:
    """(language, where it came from). The second item is what `ta doctor` shows.

    Saying just "pt" does not help someone who expected English: the next question
    is always "why?", and the answer is here.
    """
    if raw := os.environ.get("TA_LANG"):
        code = raw.split("_")[0].lower()
        if code in LANGS:
            return code, "TA_LANG"
        # An invalid value is not silenced: whoever wrote `TA_LANG=de` made a
        # choice, and ignoring it quietly makes the tool look broken.
        import logging

        logging.getLogger("ta").warning(
            "TA_LANG=%r is not a supported language (%s); using %s",
            raw, ", ".join(LANGS), DEFAULT_LANG,
        )
        return DEFAULT_LANG, "invalid TA_LANG"

    from .config import _user_config

    from_file = _user_config().get("lang")
    if isinstance(from_file, str) and from_file.lower() in LANGS:
        return from_file.lower(), "config.toml"

    if from_system := _from_locale():
        return from_system, "system locale"

    return DEFAULT_LANG, "default"


def lang() -> str:
    return _resolved()[0]


def lang_source() -> str:
    return _resolved()[1]


def reset_cache() -> None:
    """For tests and for `ta lang`. Without it the first `lang()` pins the process."""
    _resolved.cache_clear()


# ── Catalogue ───────────────────────────────────────────────────────────────
# One key per message, both languages side by side. Side by side on purpose: one
# file per language makes the translation something to do later, and nobody
# notices — with both on the same line, the gap is visible while you write it.
#
# `test_i18n` guarantees the two dictionaries have exactly the same keys. Without
# that pin, a key only in `pt` becomes wrong text on the machine of somebody
# using `en`.
MESSAGES: dict[str, dict[str, str]] = {
    # ── Erros de configuração e de disponibilidade ──────────────────────────
    "ai.no_key": {
        "pt": "A IA não está configurada. Defina GEMINI_API_KEY no .env — "
              "ou siga sem ela: captura, mural e automações não dependem de IA.",
        "en": "AI is not configured. Set GEMINI_API_KEY in your .env — "
              "or carry on without it: capture, board and automations never need AI.",
    },
    "ai.no_sdk": {
        "pt": "O SDK google-genai não está instalado. Rode: pip install -e \".[dev]\"",
        "en": "The google-genai SDK is not installed. Run: pip install -e \".[dev]\"",
    },
    "ai.failed": {
        "pt": "O modelo falhou: {erro}",
        "en": "The model failed: {erro}",
    },
    "ai.off_schema": {
        "pt": "O modelo respondeu fora do formato pedido.",
        "en": "The model answered outside the requested schema.",
    },
    "home.no_token": {
        "pt": "O Home Assistant não está configurado. Defina HA_TOKEN e HA_URL no .env.",
        "en": "Home Assistant is not configured. Set HA_TOKEN and HA_URL in your .env.",
    },
    "home.token_rejected": {
        "pt": "O Home Assistant recusou o token (401). Ele foi revogado?",
        "en": "Home Assistant rejected the token (401). Was it revoked?",
    },
    "daemon.down": {
        "pt": "O daemon não está de pé. Suba com: systemctl --user start ta",
        "en": "The daemon is not running. Start it with: systemctl --user start ta",
    },
    "daemon.outdated": {
        "pt": "O daemon está desatualizado — este mural é mais novo que ele.\n"
              "Rode: systemctl --user restart ta",
        "en": "The daemon is out of date — this board is newer than it is.\n"
              "Run: systemctl --user restart ta",
    },
    "config.exposed_without_token": {
        "pt": "TA_HOST={host} expõe o daemon na rede, e ele não tem\n"
              "autenticação própria: qualquer um na mesma rede leria suas notas,\n"
              "comandaria a casa e gastaria sua chave de modelo.\n"
              "\n"
              "Para abrir com credencial, gere um token e reinicie:\n"
              "\n"
              "    echo \"TA_TOKEN=$(python3 -c 'import secrets;"
              " print(secrets.token_urlsafe(32))')\" >> .env\n"
              "    systemctl --user restart ta\n"
              "\n"
              "Para voltar ao acesso só local, remova TA_HOST do .env.",
        "en": "TA_HOST={host} exposes the daemon to the network, and it has no\n"
              "authentication of its own: anyone on the same network could read your\n"
              "notes, command your house and spend your model API key.\n"
              "\n"
              "To open it with a credential, generate a token and restart:\n"
              "\n"
              "    echo \"TA_TOKEN=$(python3 -c 'import secrets;"
              " print(secrets.token_urlsafe(32))')\" >> .env\n"
              "    systemctl --user restart ta\n"
              "\n"
              "To go back to local-only access, remove TA_HOST from your .env.",
    },
    "auth.missing_credential": {
        "pt": "credencial ausente ou inválida (TA_TOKEN)",
        "en": "missing or invalid credential (TA_TOKEN)",
    },
    "auth.board": {
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
    "board.title": {"pt": "Mural", "en": "Board"},
    "board.loading": {"pt": "carregando…", "en": "loading…"},
    "board.empty": {"pt": "nenhuma nota ainda", "en": "no notes yet"},
    "board.load_failed": {"pt": "não deu para carregar", "en": "could not load"},
    "board.out_of_queue": {"pt": "fora da fila", "en": "out of the queue"},
    "board.done": {"pt": "concluídas", "en": "done"},
    "board.trash": {"pt": "lixeira", "en": "trash"},
    "board.trash_empty": {"pt": "a lixeira está vazia", "en": "the trash is empty"},
    "board.capture": {
        "pt": "nota nova — ex: ligar dentista @sexta #saude !alta @@09:00",
        "en": "new note — e.g. call dentist @friday #health !high @@09:00",
    },
    "board.review": {"pt": "revisar tudo", "en": "review all"},
    "board.confirm": {"pt": "confirmar?", "en": "confirm?"},
    "board.failed": {"pt": "falhou", "en": "failed"},
    "board.delete": {"pt": "apagar?", "en": "delete?"},
    "board.empty_trash": {"pt": "esvaziar lixeira", "en": "empty trash"},
    "board.restore": {"pt": "restaurar", "en": "restore"},
    "board.overdue": {"pt": "vencida ", "en": "overdue "},
    "board.due": {"pt": "prazo ", "en": "due "},
    "board.reminds": {"pt": "lembra ", "en": "reminds "},
    "board.view.board": {"pt": "geral", "en": "board"},
    "board.view.list": {"pt": "lista", "en": "list"},
    "board.view.kanban": {"pt": "kanban", "en": "kanban"},
    "board.filter.area": {"pt": "todas as áreas", "en": "all areas"},
    "board.filter.type": {"pt": "todos os tipos", "en": "all types"},
    "board.filter.tag": {"pt": "todas as tags", "en": "all tags"},
    "board.counter": {"pt": "nota(s)", "en": "note(s)"},
    "board.back_to_board": {"pt": "voltar ao mural", "en": "back to the board"},
    "board.no_match": {
        "pt": "nenhuma nota passa pelo filtro.", "en": "no note matches the filter.",
    },
    "board.clear_filters": {"pt": "limpar filtros", "en": "clear filters"},
    "board.title_filter_area": {"pt": "filtrar por área", "en": "filter by area"},
    "board.title_empty_trash": {
        "pt": "apaga em definitivo tudo que está na lixeira",
        "en": "permanently deletes everything in the trash",
    },
    "board.title_auto_color": {
        "pt": "cor automática (pela prioridade)", "en": "automatic colour (by priority)",
    },
    "board.title_delete": {"pt": "apagar (reversível)", "en": "delete (reversible)"},
    "board.save_failed": {"pt": "não deu para salvar: ", "en": "could not save: "},
    "board.in_trash": {
        "pt": "na lixeira — `↩` restaura, ou volte ao mural para escrever",
        "en": "in the trash — `↩` restores; go back to the board to write",
    },
    # ── CLI ─────────────────────────────────────────────────────────────────
    "cli.nothing_due": {
        "pt": "nada cobrável hoje.",
        "en": "nothing due today.",
    },
    "cli.tasks": {"pt": "tarefas", "en": "tasks"},
    "cli.events": {"pt": "compromissos", "en": "events"},
    "cli.overdue": {"pt": "ATRASADA", "en": "OVERDUE"},
    "cli.no_notes": {"pt": "nenhuma nota.", "en": "no notes."},
    "cli.deleted": {"pt": "apagada", "en": "deleted"},
    "cli.restored": {"pt": "restaurada", "en": "restored"},
    "cli.undoes": {"pt": "desfaz", "en": "undoes it"},
}


def t(chave: str, **fmt: object) -> str:
    """A mensagem no idioma ativo.

    Chave desconhecida devolve a própria chave em vez de estourar: uma mensagem
    feia é melhor que um `KeyError` no meio de um comando que ia funcionar.
    """
    entrada = MESSAGES.get(chave)
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
        k: t(k) for k in MESSAGES if k.startswith(prefixo)
    }
