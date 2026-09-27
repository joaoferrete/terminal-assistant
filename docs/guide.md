# The household assistant: a guide

This guide explains what the bot does and how to do each thing, for the people
who use it and for whoever runs it. The bot reads it too: when you ask how to do
something, it looks the topic up here (`help`) and answers in your language, so
what it tells you is what is written here.

Each `##` section is one topic. The bot looks topics up by their title.

*Keep it true:* every feature a person can see is added here in the same commit
([AGENTS.md](../AGENTS.md)). A test fails when a Tool or a command is missing
here, or when a config setting shown here does not exist.

## What I can do

Talk to the bot as you would to a person. Anything that is not a question or a
request becomes a note.

- **Notes and tasks:** "comprar presente da Ana sexta", "o que tenho vencido?",
  "terminei o relatório", "muda o prazo do boleto pra dia 10".
- **Lists:** "põe leite na lista de compras", "o que tem na lista?", "comprei o
  leite".
- **Reminders and timers:** "me lembra às 15h de ligar pro dentista", "timer de
  10 min", "quanto falta?".
- **The house:** "apaga a luz da sala", "deixa a luz do quarto azul em 10%",
  "liga a luz daqui 10 min", "todo dia às 7h acende a luz do quarto".
- **Calendar:** "o que tenho amanhã?", "marca janta com a Ana sexta às 20h".
- **Email:** "tem e-mail do banco essa semana?", "responde que pago amanhã".
  It writes drafts. You send them.
- **Your computer:** "o que tem na minha pasta Downloads?", "me manda o PDF do
  contrato", "como eu resolvi aquele erro do Kafka?".
- **The web:** "vai chover amanhã?", "a farmácia abre domingo?".
- **Daily summary:** "me manda o resumo agora", "muda o resumo pras 8h".
- **Links and setup:** "me manda o link do board", "conecta meu e-mail", "como
  eu libero a Ana?".

Every change comes with an [Undo] button. After the bot reads something written
by someone else (a web page, an email, a housemate's note), any change it
proposes waits for your button.

## Notes and tasks

Anything you send that is not a question becomes a **note**. A note with a
deadline is a task. A note with a time is a reminder. The bot reads dates in
plain language ("sexta", "amanhã", "dia 10", "às 15h") and marks such as `!alta`,
`#trabalho` and `@sexta`.

- **Find:** "o que eu anotei sobre o carro?" (`notes_search`); "o que está
  vencido / pra hoje / pra semana?" (`notes_due`).
- **Change:** "muda o texto da #12", "tira o prazo da #12", "põe prioridade
  alta" (`notes_edit`).
- **Progress:** "comecei o relatório" (in progress), "deixa a #12 em espera"
  (`notes_status`); "terminei", "feito" (`notes_done`).
- **Delete:** "apaga a #12" moves it to the trash (`notes_delete`). "O que tem
  na lixeira?" (`trash_list`), and "restaura a #12" (`notes_restore`). The bot
  never deletes for good. Emptying the trash is done on the board.
- **Not a note?** If the bot captured something you meant as a question, press
  [Not a note].

## Lists

Lists hold things with no deadline, such as shopping. **Household Lists** (like
`compras`) are shared by everyone in the house. **Personal Lists** are yours
alone.

- "põe leite e pão na lista de compras" (`list_add`)
- "o que tem na lista de compras?", "quais listas existem?" (`list_show`)
- "comprei o leite" marks it done (`notes_done`). A done item leaves the List.
- "cria uma lista de filmes" makes a personal List (`list_create`).

Household Lists are set up by the owner in the config (see
[Members and permissions](#members-and-permissions)):

```toml
[lists]
compras = "household"
```

Adding to a household List, or marking someone else's item done, needs that List
in your permission.

## Reminders and timers

"Me lembra às 15h de ligar pro dentista", "daqui 10 min tirar o bolo", "timer
de 15 min", "em meia hora ver o forno". The reply says when it will ring: "te
lembro às 14:10 ⏰". If the time is wrong, say so right away.

When it is due, the bot messages **you**, in private, with [Done] and
[+10 min]. The owner's reminders also pop up on their computer and on the
house speakers. Nobody else's do.

"Quanto falta?" lists your timers and scheduled actions with the time left
(`schedule_list`).

## Scheduled actions

The bot can do something later: "liga a luz da sala daqui 10 min", "todo dia às
7h acende a luz do quarto", "de segunda a sexta às 22h30 apaga tudo"
(`schedule_action`). A repeat runs once, daily, on weekdays or on weekends.

- The confirmation has [Cancel schedule]. "Cancela o agendamento 3" also
  works (`schedule_cancel`).
- "O que está agendado?" lists everything with the time left (`schedule_list`).
  `/agendado` does the same with no model.
- It runs with your permissions **as they are when it fires**.
- If the server was down and an action is more than 15 minutes late, it is
  skipped, and you are told.

To change the daily summary's hour, see [Daily summary](#daily-summary).

## The house

Lights and plugs go through Home Assistant.

- "apaga a luz", "acende a luz da sala", "desliga tudo" (`home_off`, `home_on`).
  Simple commands like these need no model: they are instant and free.
- Colours: "deixa a luz do quarto azul", "vermelho em 30%", "luz quente"
  (2700K), "luz fria" (6500K) (`home_on`). Plugs just turn on.
- Music on the house speakers: "pausa a música", "próxima" (`media_control`,
  for admins). It needs the speakers in `TA_ECHOS`.
- The owner's desk ring light: "liga a ring light", "perfil Meet"
  (`ringlight`, for admins). It needs the owner's computer on.

What each person may switch is set by their permission. Names like "quarto" come
from Home Assistant, or from aliases in the config:

```toml
[aliases]
quarto = "light.lampada_do_quarto"
```

## Calendar

Each person connects their own Google Calendar.

- **Connect:** say "conecta minha agenda" or send `/conectar_agenda`
  (`connect_calendar`). Open the link, allow access, and at the end the browser
  shows a page error. That is expected. Copy the address bar and paste it back
  into the chat.
- **Read:** "o que tenho amanhã?" (`calendar_day`).
- **Create:** "marca dentista sexta às 14h" (`calendar_create`). Events go to a
  calendar named **Terminal Assistant**, which you create once in Google
  Calendar, and never have guests.
- Notes that look like appointments ("dentista sexta 14h") also become events
  on their own. The bot tells you "📅 Criei na agenda…" with [Undo].

The server needs a Google OAuth client first. See [Google setup](#google-setup).

## Email

Each person connects their own Gmail.

- **Connect:** "conecta meu e-mail" or `/conectar_email` (`connect_email`),
  with the same paste-back as the calendar.
- **Search:** "tem e-mail do banco essa semana?" (`mail_search`), with Gmail's
  search words: `from:`, `subject:`, `newer_than:7d`, `is:unread`.
- **Read:** "abre esse" (`mail_read`).
- **Draft:** "responde que pago amanhã", "escreve pra ana@exemplo.com
  convidando pra janta" (`mail_draft`). The draft waits in Gmail's Drafts. **The
  bot never sends email.**

The bot reads your mail only when you ask, only in a private chat, and keeps
nothing. What an email says can never make it act without your button. To
answer you, the part of an email it uses goes to the AI model the server uses.

## Your computer

A **Satellite** is your own computer connected to the server.

- **Pair it:** ask "me dá o código do satellite" or send `/satellite`
  (`satellite_code`). On the computer, with `TA_SERVER` set to the server's
  address, run `ta satellite login <code>`, then `make install-satellite`.
- **Files:** "o que tem em Downloads?" (`files_list`), "lê o arquivo X"
  (`files_read`), "me manda o PDF do contrato" (`files_send`). This works only
  for the folders your computer shares, read-only, and only while it is on.
  Choose them on the computer:

  ```toml
  [files]
  folders = ["~/Documentos", "~/Downloads"]
  ```

- **Search your documents by meaning:** "como eu resolvi aquele erro do
  Kafka?" (`docs_search`). Choose the folders to index on the computer, then run
  `ta satellite sync`:

  ```toml
  [rag]
  folders = ["~/notas"]
  ```

"Quais computadores estão conectados?" or `/satellites` (`satellites_list`) shows
each computer by name, whether it is on now, and when it was last seen. The owner
sees everyone's.

Hidden files and anything named like a secret never leave the computer. Each
person reaches only their own computer.

## Daily summary

Every morning the bot sends a summary: calendar, what is due, Lists, the
weather, and for admins the server's health, AI cost and the AdGuard numbers.

- "muda o resumo pras 8h", "desliga o resumo", "só agenda e tarefas"
  (`digest_set`).
- "me manda o resumo agora" (`digest_now`).

The owner sets the place for the weather in the config:

```toml
[digest]
latitude = -23.55
longitude = -46.63
place = "São Paulo"
```

## The board

The board is a page with your notes as post-its, a kanban, a list and your
Lists. "Me manda o link do board" or `/board` (`board_link`) sends a one-time
link that logs your browser in. From outside the house network, the owner sets
`TA_PUBLIC_URL`.

## How the bot talks to you

"Me chama de João", "fala mais curto" (`persona_set`). That is yours, in every
chat. The bot's own name and personality are the owner's, in the config (`[chat]
bot_name`, `bot_personality`).

In a conversation, the bot remembers what was said recently, and can look further
back in the same chat when you refer to it (`memory_search`). What you tell it in
private never shows up in a group.

## Web search

"Vai chover amanhã?", "que horas abre a farmácia?" (`web_search`). The answer
comes with its links. What a page says can never make the bot act without your
button.

## Routines

A **Routine** is a named sequence of actions, started by a phrase you choose.

- **Create:** "cria uma rotina chegada que liga a luz da sala e o ventilador,
  quando eu disser cheguei em casa" (`routine_create`). Up to 10 steps, each one
  something the bot can do, such as lights and plugs, colours, Lists or music.
- **Run:** say one of its phrases exactly ("cheguei em casa"). It runs at once
  with no AI, so it is free. Or say it in other words ("tô chegando, prepara a
  casa"), or "roda a rotina chegada" (`routine_run`). A scheduled action can run
  one too: "todo dia às 7h roda a rotina manhã".
- **The answer** lists each step, with [Undo all].
- **See and delete:** "quais rotinas eu tenho?" (`routine_list`), "apaga a rotina
  chegada" (`routine_delete`).

A Routine is **yours** unless you say "da casa" when creating it. Then anyone in
the house can start it, each with their own permissions: a step someone may not
do is skipped, and the answer says so. Only you, or the owner, can delete it.

## Automations

"O que está automatizado?" (`rules_list`) lists your scheduled actions and
timers, plus the house's file Rules (for admins).

Your **Priorities** are what matters to you, used to order your notes. See them
with "quais são minhas prioridades?" (`priorities_show`). They are set with `ta
init` or on the board.

## Members and permissions

The bot answers only people the owner allowed.

**To allow someone from the chat** (owner only): "libera o bot pra @ana como
morador" (`member_add`), "tira o acesso da Ana" (`member_remove`), "quem usa o
bot?" (`members_list`, or `/moradores`, which also shows when each person last
talked to the bot). It always asks you to confirm, and it
applies at once with no restart. It can only give an existing Grant. What a Grant
allows, and `admin`, are set on the config page.

When someone who is not allowed messages the bot, they get a polite "no access"
reply, and you get a message with [Allow] and [Ignore], at most once per person
per day.

**Or by the config:** they need a Telegram **@username** (Settings → Username).
The owner adds them on the config page (Members) or in `config.toml`:

```toml
[members.ana_silva]
grants = ["morador"]

[grants.morador]
entities = ["light.sala", "switch.tomada"]
lists = ["compras"]
admin = false
```

Restart the daemon (the config page has [Restart]), and the person sends the bot
any message. The owner is told when they connect. Removing their line revokes
them on their next message, and what they wrote stays theirs.

**What a permission (Grant) holds:** `entities` (lights and plugs they may
switch: ids, domains like `switch.`, or groups), `lists` (household Lists they may
add to), `tools` (extra Tools by name), and `admin` (media, the ring light, server
health). With no Grant, a person can still take notes and talk about their own.

What each person writes is **private**: nobody else sees their notes, reminders,
calendar or email, the owner included. Household Lists are shared. One exception,
told to each person when they pair a computer: the owner can search the folders
every computer indexes for search by meaning.

## The config page

Everything `ta` is configured with, on a page:
`http://<server>:7777/config`. Only the owner can open it, from the browser
where their board is logged in, plus a password set once on the server with
`ta passwd`. Secrets (API keys, tokens) can be replaced there but are never
shown. Settings marked "restart" apply after [Restart].

The bot never changes the config itself. It explains how, from this guide.

## Models and cost

The bot uses remote AI models: DeepSeek by default, with Gemini as the
fallback, and Gemini for web search. Simple commands ("apaga a luz") and plain
notes use no model at all.

Tasks come in two **Tiers**. `pro` covers the chat, organizing and Priorities,
and `lite` covers the passes over every note and group message. The owner picks
a model for each:

```toml
[llm.tiers]
lite = "deepseek:deepseek-flash"
pro  = "deepseek:deepseek-v4-pro"
```

Spending has ceilings: one per person per day and one for the house per month.
When one is spent, chat and search stop until it resets, and notes are still
taken.

```toml
[chat]
daily_usd_per_member = 0.50
monthly_usd_household = 10.0
```

## Google setup

Once per household, the owner creates an OAuth client in the Google Cloud
Console for the calendar and email. The full walkthrough is in
[calendar.md](calendar.md#on-a-server-google-calendar). In short: enable the
Calendar and Gmail APIs, set up the consent screen as External with four scopes,
fill in Branding (a home page and a privacy policy), publish it to Production,
create a *Desktop app* client, and put `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` in the server's `.env`. A work account may be blocked by
its admin, so try the personal one first.

## The household group

The bot can live in a family group. It reads the group, puts things like
"acabou o leite" on the household List by itself (with [Undo]), and answers when
mentioned. It never shows anyone's private notes there.

1. At @BotFather: `/setprivacy`, pick the bot, **Disable**.
2. Add the bot to the group, and put the group's chat id in the config:

```toml
[channel.telegram]
groups = [-1001234567890]
```

## Commands

Everything also works by just asking. Commands are shortcuts, and `/` in
Telegram shows the menu.

| Command | What it does |
|---|---|
| `/start` | says hello |
| `/help` | what the bot can do |
| `/board` | a link to your board |
| `/agendado` | your scheduled actions and timers |
| `/conectar_agenda` | connect Google Calendar |
| `/conectar_email` | connect Gmail |
| `/satellite` | a code to pair a computer |
| `/satellites` | your connected computers, and whether they are on now |
| `/moradores` | who may use the bot, and when each last talked (owner only) |

## When something does not work

- **"Nothing you may switch matches"**: the name is not a light you are
  allowed, or Home Assistant does not know it. Try the room's name, or ask the
  owner for an alias.
- **The calendar or email says it is not connected**: connect it again. In
  Google's *Testing* mode, connections expire every seven days, so the owner
  should publish the app to Production.
- **"Your computer is not connected"**: the computer is off or asleep, or its
  Satellite is stopped (`systemctl --user restart ta-satellite`).
- **A reminder did not ring**: check the time in the reply that confirmed it,
  and ask "quanto falta?".
- **The bot says the AI is unavailable**: the note was still taken. The owner
  checks the API keys and the spending ceilings.
