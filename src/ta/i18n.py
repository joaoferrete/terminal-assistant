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
        "pt": "A IA não está configurada. Defina DEEPSEEK_API_KEY ou GEMINI_API_KEY no .env — "
              "ou siga sem ela: captura, mural e automações não dependem de IA.",
        "en": "AI is not configured. Set DEEPSEEK_API_KEY or GEMINI_API_KEY in your .env — "
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
    "home.unreachable": {
        "pt": "Home Assistant não responde em {url}. `docker ps`?",
        "en": "Home Assistant is not answering at {url}. `docker ps`?",
    },
    "home.timeout": {
        "pt": "Home Assistant não respondeu em {s}s.",
        "en": "Home Assistant did not answer in {s}s.",
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
    "api.empty_text": {"pt": "texto vazio", "en": "empty text"},
    "api.ai_not_configured": {
        "pt": "nem DEEPSEEK_API_KEY nem GEMINI_API_KEY configuradas",
        "en": "neither DEEPSEEK_API_KEY nor GEMINI_API_KEY is configured",
    },
    "api.review_off": {
        "pt": "revisão desligada (TA_AUTO_REVIEW=0)",
        "en": "review is off (TA_AUTO_REVIEW=0)",
    },
    "api.note_missing": {"pt": "nota {id} não existe", "en": "note {id} does not exist"},
    "api.note_not_in_trash": {
        "pt": "nota {id} não está na lixeira. Apague primeiro.",
        "en": "note {id} is not in the trash. Delete it first.",
    },
    "api.confirm_required": {
        "pt": "confirmação explícita é obrigatória: isto não tem volta",
        "en": "explicit confirmation is required: this cannot be undone",
    },
    "api.missing_entity": {"pt": "falta o entity", "en": "the entity is missing"},
    "api.no_match": {
        "pt": "nada casou com {termo!r}. Veja `ta entities`.",
        "en": "nothing matched {termo!r}. See `ta entities`.",
    },
    "api.no_echo": {
        "pt": "nenhum Echo configurado. Defina TA_ECHOS no .env com o "
              "entity_id do media_player, ou passe o alvo no comando.",
        "en": "no Echo configured. Set TA_ECHOS in your .env to the "
              "media_player entity_id, or pass the target in the command.",
    },
    "api.not_media_player": {
        "pt": "{entity!r} não é um media_player. Veja `ta entities`.",
        "en": "{entity!r} is not a media_player. See `ta entities`.",
    },
    "api.lighter_missing": {
        "pt": "Lighter não está instalada", "en": "Lighter is not installed",
    },
    "api.confirm_required_event": {
        "pt": "confirmação explícita é obrigatória (ADR 0007)",
        "en": "explicit confirmation is required (ADR 0007)",
    },
    "api.event_failed": {
        "pt": "não consegui criar o evento", "en": "could not create the event",
    },
    "api.nothing_to_do": {"pt": "nada a fazer", "en": "nothing to do"},
    "review.note_reviewed": {"pt": "Nota revisada", "en": "Note reviewed"},
    "review.due_removed": {"pt": "prazo removido", "en": "deadline removed"},
    "review.priority": {"pt": "prioridade", "en": "priority"},
    "review.priority_removed": {"pt": "prioridade removida", "en": "priority removed"},
    "review.untitled": {"pt": "(sem título)", "en": "(untitled)"},
    "review.event_created": {"pt": "evento criado", "en": "event created"},
    "reminder.late_minutes": {"pt": " (atrasado {n} min)", "en": " ({n} min late)"},
    "reminder.late_hours": {"pt": " (atrasado {n} h)", "en": " ({n} h late)"},
    "reminder.very_late": {"pt": " (muito atrasado)", "en": " (very late)"},
    "calendar.no_typelibs": {
        "pt": "agenda indisponível: {erro}. "
              "Rode `make check-gi` — provavelmente faltam os typelibs do apt.",
        "en": "calendar unavailable: {erro}. "
              "Run `make check-gi` — the apt typelibs are probably missing.",
    },
    "calendar.no_bus": {
        "pt": "agenda indisponível: {erro}. É esperado sem sessão gráfica — a agenda "
              "precisa do barramento do usuário. O resto do Terminal Assistant "
              "funciona sem ela.",
        "en": "calendar unavailable: {erro}. This is expected with no graphical "
              "session — the calendar needs the user bus. The rest of Terminal "
              "Assistant works without it.",
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
    "api.list_missing": {"pt": "essa lista não existe", "en": "that list does not exist"},
    "api.forbidden": {
        "pt": "Você não tem permissão para isso. Quem administra a casa define isso no config.",
        "en": "You are not allowed to do that. Whoever runs the house sets it in the config.",
    },
    "auth.code_used": {
        "pt": "Esse link já foi usado ou expirou. Peça outro ao bot com /board.",
        "en": "This link was already used or has expired. Ask the bot for another with /board.",
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
    # `{cmd}` recebe marcação (`<code>ta note</code>`) montada pelo mural. É o
    # único texto do catálogo com um buraco para HTML, e os dois lados são
    # nossos — nada de entrada do usuário passa por aqui.
    "board.empty": {
        "pt": "nada aqui. Escreva algo acima, ou use {cmd}.",
        "en": "nothing here. Write something above, or use {cmd}.",
    },
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
    "board.view.lists": {"pt": "listas", "en": "lists"},
    "board.scope.household": {"pt": "da casa", "en": "household"},
    "board.scope.personal": {"pt": "sua", "en": "yours"},
    "board.list_add": {"pt": "adicionar…", "en": "add…"},
    "board.list_empty": {"pt": "nada aqui", "en": "nothing here"},
    "board.lists_none": {
        "pt": "Nenhuma lista ainda. Declare em [lists] no config.toml.",
        "en": "No lists yet. Declare them under [lists] in config.toml.",
    },
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
    "board.title_filter_type": {"pt": "filtrar por tipo", "en": "filter by type"},
    "board.title_filter_tag": {"pt": "filtrar por tag", "en": "filter by tag"},
    "board.title_review": {
        "pt": "pede à LLM para reetiquetar todas as notas abertas",
        "en": "asks the LLM to re-tag every open note",
    },
    "board.title_status": {"pt": "estado", "en": "status"},
    "board.title_delete_again": {
        "pt": "clique de novo para apagar", "en": "click again to delete",
    },
    "board.queueing": {"pt": "enfileirando…", "en": "queueing…"},
    "board.queued": {"pt": "{n} na fila…", "en": "{n} queued…"},
    "board.purge_confirm": {
        "pt": "apagar {n} para sempre?", "en": "delete {n} forever?",
    },
    "board.purged": {"pt": "{n} apagada(s)", "en": "{n} deleted"},
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
    # ── Bot (Channel) ───────────────────────────────────────────────────────
    "bot.paired": {
        "pt": "Pronto, agora eu te reconheço. Tudo o que você mandar aqui vira nota.",
        "en": "Done, I know you now. Everything you send here becomes a note.",
    },
    "bot.captured": {"pt": "Anotado · #{id}", "en": "Noted · #{id}"},
    "bot.captured_due": {"pt": "Anotado · #{id} · prazo {due}", "en": "Noted · #{id} · due {due}"},
    "bot.unsupported": {
        "pt": "Por enquanto eu só leio texto.",
        "en": "For now I can only read text.",
    },
    "bot.unknown_command": {
        "pt": "Não conheço esse comando. Mande só o texto que eu anoto.",
        "en": "I don't know that command. Just send the text and I'll note it.",
    },
    "bot.board_link": {
        "pt": "O seu board: {url}\nO link vale por 5 minutos e funciona uma vez só. "
              "Depois disso o navegador já fica lembrando de você.",
        "en": "Your board: {url}\nThe link works once, for 5 minutes. "
              "After that the browser remembers you.",
    },
    "bot.board_unreachable": {
        "pt": "O board só abre nesta máquina: o daemon está em loopback. "
              "Para abrir no celular, veja TA_HOST e TA_TOKEN.",
        "en": "The board only opens on this machine: the daemon is on loopback. "
              "To open it on a phone, see TA_HOST and TA_TOKEN.",
    },
    "bot.voice_listening": {"pt": "Ouvindo…", "en": "Listening…"},
    "bot.voice_placeholder": {
        "pt": "🎤 áudio sem transcrição ({reason}) · arquivo: {path}",
        "en": "🎤 untranscribed audio ({reason}) · file: {path}",
    },
    "bot.voice_kept": {
        "pt": "Não consegui transcrever ({reason}). Guardei o áudio e deixei a nota #{id} "
              "para você não perder.",
        "en": "I couldn't transcribe it ({reason}). I kept the audio and left note #{id} "
              "so it isn't lost.",
    },
    "bot.voice_download": {"pt": "não consegui baixar o áudio", "en": "could not download it"},
    "bot.voice_unavailable": {
        "pt": "transcrição não instalada no servidor", "en": "transcription is not installed",
    },
    "bot.voice_too_long": {"pt": "áudio longo demais", "en": "the audio is too long"},
    "bot.voice_failed": {"pt": "a transcrição falhou", "en": "transcription failed"},
    "bot.member_paired": {
        "pt": "{who} acabou de entrar no bot. Pode mandar notas a partir de agora.",
        "en": "{who} just joined the bot, and can send notes from now on.",
    },
    "bot.voice_heard": {"pt": "🎤 “{text}”", "en": "🎤 “{text}”"},
    "bot.captured_offline": {
        "pt": "{line}\n(o modelo está fora agora, então só anotei)",
        "en": "{line}\n(the model is unavailable right now, so I only noted it)",
    },
    "bot.confirm_needed": {
        "pt": "Antes de fazer, preciso da sua confirmação: {action} ({args}).",
        "en": "Before I do it, I need your confirmation: {action} ({args}).",
    },
    "agent.sources": {"pt": "Fontes: {list}", "en": "Sources: {list}"},
    "bot.hello": {
        "pt": "Oi! Tudo o que você mandar aqui vira nota.",
        "en": "Hi! Everything you send here becomes a note.",
    },
    # ── CLI ─────────────────────────────────────────────────────────────────
    # A linha de uma nota no `ta note` e no `ta list`. Estava fixa em português —
    # `tarefa, prazo`, `lembrete`, `prio` — então em `TA_LANG=en` o comando mais
    # usado do projeto respondia no idioma errado.
    #
    # O VALOR da prioridade também passa pelo catálogo: ele é canônico em inglês no
    # banco (`high`), e imprimi-lo cru mostrava `prio high` para quem lê português.
    "note.task": {"pt": "tarefa, prazo {due}", "en": "task, due {due}"},
    "note.reminder": {"pt": "lembrete {at}", "en": "reminder {at}"},
    "note.priority": {"pt": "prio {value}", "en": "prio {value}"},
    "cli.sent_unconfirmed": {
        "pt": "enviado, estado não confirmado", "en": "sent, state not confirmed",
    },
    "cli.unconfirmed": {"pt": "não confirmado", "en": "not confirmed"},
    "cli.turned_off": {"pt": "apagado", "en": "turned off"},
    "cli.nothing_was_on": {"pt": "nada estava aceso.", "en": "nothing was on."},
    "cli.skipped_pinned": {
        "pt": "{n} respeitada(s): você as arrastou à mão.",
        "en": "{n} left alone: you dragged them by hand.",
    },
    "cli.priorities_exist": {
        "pt": "já existe. Use --force para responder de novo, ou "
              "`ta priorities \"instrução\"` para ajustar por prompt.",
        "en": "it already exists. Use --force to answer again, or "
              "`ta priorities \"instruction\"` to adjust it by prompt.",
    },
    "cli.priorities_unset": {
        "pt": "(ainda não definido — rode `ta init`)",
        "en": "(not set yet — run `ta init`)",
    },
    # As linhas de contexto e os rótulos do `ta doctor`. Estavam fixos em inglês,
    # então em `TA_LANG=pt` a tela saía metade traduzida: rótulos ingleses e o
    # resumo em português, na mesma tabela.
    #
    # `Home Assistant`, `Lighter` e `Gemini` são nomes de produto e ficam iguais
    # nos dois idiomas — traduzir nome próprio atrapalha quem vai procurar por ele.
    "doctor.language": {"pt": "idioma", "en": "language"},
    "doctor.config": {"pt": "config", "en": "config"},
    "doctor.daemon": {"pt": "daemon", "en": "daemon"},
    "doctor.does_not_exist": {"pt": "(não existe)", "en": "(does not exist)"},
    "doctor.from": {"pt": "de", "en": "from"},
    "doctor.exposed": {"pt": "[aberto para a rede]", "en": "[open to the network]"},
    "doctor.local_only": {"pt": "[só local]", "en": "[local only]"},
    "doctor.read_env": {"pt": "li {path}", "en": "read {path}"},
    "cap.notes": {"pt": "Notas", "en": "Notes"},
    "cap.calendar": {"pt": "Agenda", "en": "Calendar"},
    "cap.mic": {"pt": "Microfone", "en": "Microphone"},
    "cap.home": {"pt": "Home Assistant", "en": "Home Assistant"},
    "cap.lighter": {"pt": "Lighter (ringlight)", "en": "Lighter (ringlight)"},
    "cap.ai": {"pt": "IA", "en": "AI"},
    "cap.telegram": {"pt": "Telegram (bot)", "en": "Telegram (bot)"},
    "cap.voice": {"pt": "Voz (transcrição)", "en": "Voice (transcription)"},
    "cli.doctor_summary": {
        "pt": "{live}/{total} disponíveis. O que está marcado com — é opcional.",
        "en": "{live}/{total} available. Anything marked with — is optional.",
    },
    "cli.not_an_event": {
        "pt": "não parece um compromisso com data e hora.",
        "en": "this does not look like an appointment with a date and time.",
    },
    "cli.event_title": {"pt": "título", "en": "title"},
    "cli.event_confidence": {"pt": "confiança", "en": "confidence"},
    "cli.no_dedicated_calendar": {
        "pt": "não achei a agenda dedicada da conta {account}.",
        "en": "could not find the dedicated calendar for the {account} account.",
    },
    "cli.event_prompt": {
        "pt": "[s] criar  [n] só nota  > ", "en": "[y] create  [n] note only  > ",
    },
    # A tecla que o prompt acima manda apertar. Estava fixa em `"s"` no código,
    # então em inglês o prompt dizia `[y] create` e quem digitava `y` tinha o
    # evento recusado em silêncio — o prompt mentia.
    "cli.confirm_key": {"pt": "s", "en": "y"},
    # A palavra inteira, para a única ação sem volta: uma letra é fácil de apertar
    # por engano.
    "cli.purge_word": {"pt": "apagar", "en": "delete"},
    "cli.purge_prompt": {
        "pt": "Digite '{word}' para confirmar: ",
        "en": "Type '{word}' to confirm: ",
    },
    "cli.no_way_back": {"pt": "Isto NÃO tem volta.", "en": "This CANNOT be undone."},
    "cli.cancelled": {"pt": "cancelado.", "en": "cancelled."},
    "cli.nothing_created": {"pt": "nada foi criado.", "en": "nothing was created."},
    "cli.nothing_to_capture": {
        "pt": 'nada para capturar. Exemplo: ta note "ligar dentista @sexta #saude !alta"',
        "en": 'nothing to capture. For example: ta note "call dentist @friday #health !high"',
    },
    "cli.nothing_to_review": {
        "pt": "nada aberto para revisar.", "en": "nothing open to review.",
    },
    "cli.queued_reviewing": {
        "pt": "{n} nota(s) na fila. Revisando…", "en": "{n} note(s) queued. Reviewing…",
    },
    "cli.trash_empty": {"pt": "lixeira vazia.", "en": "the trash is empty."},
    "cli.trash_count": {
        "pt": "{n} nota(s) na lixeira:", "en": "{n} note(s) in the trash:",
    },
    "cli.purged_one": {
        "pt": "apagada em definitivo: #{id}", "en": "permanently deleted: #{id}",
    },
    "cli.purged": {
        "pt": "{n} nota(s) apagada(s) em definitivo.",
        "en": "{n} note(s) permanently deleted.",
    },
    "cli.which_note": {
        "pt": "diga qual nota apagar, ou use `ta rm --list`.",
        "en": "say which note to delete, or use `ta rm --list`.",
    },
    "cli.calendar_empty": {
        "pt": "agenda: nada marcado.", "en": "calendar: nothing scheduled.",
    },
    "cli.all_day": {"pt": "dia inteiro", "en": "all day"},
    "cli.event_when": {"pt": "quando", "en": "when"},
    "cli.interview_intro": {
        "pt": "Quatro perguntas. Responder vazio deixa em branco.",
        "en": "Four questions. Answering with nothing leaves it blank.",
    },
    "cli.rules_loaded": {
        "pt": "{n} regra(s), {bad} com erro.", "en": "{n} rule(s), {bad} with errors.",
    },
    # O que o `ta doctor` relata ao migrar o layout do ADR 0014. Roda uma vez por
    # máquina, e é justamente na primeira execução que a mensagem tem de ser clara.
    "doctor.rules_copied": {
        "pt": "copiadas {n} regra(s) para {path}",
        "en": "copied {n} rule(s) to {path}",
    },
    "doctor.created": {"pt": "criado {path}", "en": "created {path}"},
    "cli.lang_set": {"pt": "idioma: {code}  ({path})", "en": "language: {code}  ({path})"},
    "cli.lang_after_restart": {
        "pt": "vale para a captura e para o mural depois de:",
        "en": "applies to capture and to the board after:",
    },
    # O rótulo curto de prioridade que o `ta list` alinha em coluna. Os três TÊM
    # que ter a mesma largura nos dois idiomas: a coluna não é uma tabela, é
    # espaçamento fixo, e um rótulo de 4 letras desalinha a lista inteira.
    "priority.short.high": {"pt": "!ALTA", "en": "!HIGH"},
    "priority.short.medium": {"pt": "!med ", "en": "!med "},
    "priority.short.low": {"pt": "!bax ", "en": "!low "},
    "cli.install_zenity": {
        "pt": "zenity não encontrado. `sudo apt install zenity`.",
        "en": "zenity not found. `sudo apt install zenity`.",
    },
    "cli.timeout": {
        "pt": "daemon não respondeu em {s}s ({url}).",
        "en": "the daemon did not answer in {s}s ({url}).",
    },
    "cli.refused": {
        "pt": "daemon recusou ({code}): {detail}", "en": "daemon refused ({code}): {detail}",
    },
    "cli.calendar_warming": {
        "pt": "a agenda estava aquecendo; as próximas chamadas são instantâneas",
        "en": "the calendar was warming up; the next calls are instant",
    },
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
