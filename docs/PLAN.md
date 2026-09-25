# V2 plan: the household assistant

This is the implementation plan for V2, and the place every session resumes from.
V1 made `ta` a personal assistant for one machine. V2 makes it **the assistant of a
household**: the core runs on a small always-on home server, the laptop becomes a
Satellite, a messaging Channel (Telegram first) is the front door, and several
people share it with private data and configurable permissions.

The decisions below came out of one long design interview. They are settled —
each one carries its reason so that nobody has to re-derive it. Reopening one is a
question to the user, never a silent change in a diff.

Vocabulary is in [`CONTEXT.md`](../CONTEXT.md); the decisions that are hard to
reverse are also ADRs, linked where they apply.

## How to use this document

1. **Start** by reading [Now](#now). It names the next task.
2. **Work** on that task; its acceptance criteria say when it is done.
3. **Close the iteration**, in the same commit as the code: tick the task, rewrite
   *Now* to name the next one, and append to [Discoveries](#discoveries) anything
   that contradicted the plan. [`AGENTS.md`](../AGENTS.md) makes this mandatory.

## Now

- **Phase:** F1, on branch `feat/llm-providers` (stacked on `docs/v2-plan`).
  T1.1 is done. F0 was finished on 2026-09-25.
- **Next agent action:** T1.2, usage accounting. `LLM.on_usage` already reports
  `(provider, task, Usage)` after every answered call; T1.2 adds the migration
  and points the hook at it in `daemon.create_app`.
- **Waiting on the user:** a DeepSeek API key and a Telegram bot token, needed from
  T1.4 on. They go straight into the server's `.env`, never into the chat.
- **Rule:** never run a command on the server without the user's yes, read-only
  ones included.

## Decisions

### Topology and infrastructure

**D1 — The core lives on the server; the laptop is a Satellite.** The database,
board, scheduler, Rules, the bot and every model call run on the home server
(a DietPi box, always on). The laptop keeps only what physically depends on it.
*Why:* a Channel must answer at 3am with the laptop closed, and there must be one
source of truth for Notes. *Consequence:* the "single machine" premise of V1 is
gone — see [ADR 0015](adr/0015-a-server-and-its-satellites.md).

**D18 — Home Assistant moves to the server first, in Docker, from a backup.**
*Why:* controlling the house from the bot is pointless if HA sleeps with the
laptop, and `ta` only knows an `HA_URL`, so the move costs almost no code.
*Consequence:* ~0.5–1 GB of the server's 8 GB; the Tuya/HACS/Alexa integrations
come across in the backup and must be checked, not assumed.

**D19 — The Satellite queues capture when the server is away.** `ta note` on the
laptop always answers instantly; if the server does not, the Note goes into a small
local queue and is sent later with its original timestamp. Only capture queues —
`ta list` and the board say they are offline. The meeting Rule runs on the server:
the Satellite reports the microphone, and receives local actions (Lighter,
desktop notification) back. *Why:* [ADR 0003](adr/0003-the-llm-is-never-on-the-critical-path.md)
promised capture never waits for the network, and the server *is* the network now.
A full replica with two-way sync was rejected as a project of its own.

### Models

**D2 — Speech-to-text is local; the LLM is pluggable.** Whisper
(faster-whisper `small`, int8) runs on the server, so audio never leaves the
house. The LLM sits behind a provider interface. *Why:* the server is an i5 6th gen
with 8 GB and no usable GPU — good enough for Whisper, too weak for a local LLM that
understands Portuguese dates and the user's Priorities.

**D3 — DeepSeek is the default provider; Gemini is the fallback; routing is per
task, in config.** Each LLM task (`review_capture`, `organize`, `detect_event`,
`digest_prose`, Priorities rewrite, the agent) names its provider in `config.toml`.
There is no dynamic "complexity" classifier. *Why:* DeepSeek is cheaper; a
classifier costs a call per message and fails invisibly. *Consequence:* DeepSeek has
JSON mode but no response schema, so structured output is validated on our side
with Pydantic, retried once, then sent to the fallback — see
[ADR 0018](adr/0018-llm-providers-routed-per-task.md).

### The Channel and intent

**D4 — A Channel interface, Telegram first, by long polling.** WhatsApp can come
later behind the same interface. *Why:* long polling opens no port, needs no public
URL or certificate, and leaves [ADR 0012](adr/0012-loopback-by-default-and-a-token-to-leave-it.md)'s
threat model intact. WhatsApp's official API needs a public HTTPS webhook and paid
templates; the unofficial libraries risk a ban on a personal number.

**D8 / D21 — Access is an allowlist; identity is the numeric user id.** The Owner
invites Members by `@username` in config. On first contact the bot binds that
username to the numeric Telegram user id and stores it; from then on only the id
counts. Groups are allowlisted by chat id. The Owner gets a private message on every
new pairing. Everything else is ignored without a reply. *Why:* usernames are
optional and mutable — a released username can be claimed by a stranger, who would
inherit the Member's Grants. Asking people for their numeric id was rejected as
friction.

**D9 — A deterministic pre-router, then an agent; every message is handled or
captured.** Slash commands and the existing home grammar resolve with no model. The
rest goes to an LLM agent with Tools. If the model fails or times out, the message
becomes a Note with a short "saved; the model is unavailable". *Why:* keeps ADR
0003's promise in its new form — see
[ADR 0017](adr/0017-the-bot-handles-or-captures.md).

**D5 — One message is one Note; splitting is proposed.** A message carrying three
things becomes one Note immediately through the deterministic parser. The review
then *proposes* a split, with buttons. *Why:* the model does not create Notes on
its own, same pattern as [ADR 0007](adr/0007-propose-and-confirm-before-writing-to-the-calendar.md).

**D10 / D11 — In an allowlisted group the bot reads everything, and captures what is
actionable directly into a household List.** A cheap classifier looks at every group
message. Actionable ones go straight into a household List. The bot reacts with ✅
and offers undo. Everything else is only kept as Conversation Memory. Every bot
action leaves a **Receipt** tied to the message that caused it. Replying to that
message and asking what the bot did reads the Receipt, and undo reads the same one.
*Why:* this is the "middleware that listens" that makes the tool frictionless. It is
an explicit exception to D5, allowed because adding a List item is cheap and
reversible. Proposing with buttons was rejected as group noise nobody answers.

### People, data and permissions

**D6 — Notes are private to their author; household things are born shared.** A
Member's Notes are visible only to that Member. Household Lists are visible to every
Member. **A reply in a group never contains private data.**

**D7 — List is a new entity: named, scoped (household or personal).** Items are
Notes linked to a List; the List decides visibility. The LLM proposes which List an
item belongs to, as a machine decision the user can override (same mechanism as
`tags_by_user`). Lists sit outside the Horizon ordering. *Why:* a special tag
would hide a visibility rule inside a tag; a third Note role would be a type in
disguise, against [ADR 0006](adr/0006-one-note-entity-with-roles.md).

**D12 — Grants in config; every action runs with the author's Grant.** A Grant is
a named set of permissions (Entities and groups, Lists, Tools, admin) assigned to
Members in `config.toml`. Deny by default. The Owner holds everything. *Why:* the
bot is shared, the house has personal Entities (the Owner's ring light), and
proactive capture means a misread sentence must not reach further than its author
could — see [ADR 0016](adr/0016-members-private-by-default-acting-as-the-author.md).

**D13 — Conversation Memory is scoped to its conversation; Persona is per Member.**
What was said in a private chat is searchable only there. What was said in a group
is searchable by any Member, in that group. Memory is reached through a Tool, never
injected into every prompt, and has configurable retention. A **Persona** (the name
the bot uses and its tone for a Member) belongs to the Member and applies in every
conversation. *Why:* Persona is form, not content, so it can cross contexts safely;
memory is content, so it cannot.

**D20 — The board is entered through a magic link from the Channel.** `/board`
returns, privately, a one-time link valid for five minutes that becomes a session
cookie for that Member. Satellites get a per-Member token the same way. *Why:* the
Channel already proves identity, and Notes are now private, so the board must know
who is looking. Amends ADR 0012: the single token goes away, and loopback on the
server no longer means "the owner".
*Pulled forward (user, 2026-09-25):* the bot sends the link on first contact and
on `/board` from F1 onwards (T1.7), before Members exist.

**D22 — One Language per installation; Priorities per Member.** [ADR 0013](adr/0013-one-language-at-a-time.md)
stands. Persona changes the name and the tone, never the language. Each Member has
their own Priorities, and the existing ones become the Owner's.

### New capabilities

**D14 — Calendar through the Google Calendar API, with OAuth per Member.** The
server reads and creates events around the clock. Connecting runs through the
Channel. `/conectar_agenda` sends the consent link with a `localhost` redirect; the
phone's browser fails to load it, as intended; the Member pastes the address back
into the chat and the bot extracts the code. *Why:* GNOME Online Accounts lives on
the laptop and only serves the Owner. *Consequences:* Google's device flow does not
allow Calendar scopes (verified), hence the paste-back. The OAuth app must be in
*Production*: in *Testing*, refresh tokens expire after seven days. Amends
[ADR 0004](adr/0004-calendar-through-gnome-online-accounts.md). ADR 0007 still
holds.
**Open risk, raised by ADR 0004 itself:** a Workspace admin can block unverified
third-party apps, so a *work* Google account may refuse the consent. T5.1 tests
this first. If it is refused, the user decides between verifying the app and keeping
that one calendar read through the Owner's Satellite.

**D15 — The Digest is pushed per Member.** Each Member picks whether, when (07:00
by default) and which sections: calendar, chaseable Notes, household Lists, weather,
and, for admins only, server health (CPU, temperature, RAM, disk, uptime, LLM cost
per provider). The facts are deterministic; the model only writes the opening
prose, and a Digest without prose still goes out. Weather: Open-Meteo, free and
keyless (terms to confirm in T5.3).

**D16 — The agent executes only Tools, written in Python.** A Tool is a function
with `@tool` in `~/.config/ta/tools/`, mirroring `@rule` ([ADR 0002](adr/0002-our-own-automation-engine-in-python.md)).
Each declares the Grant it needs and whether it is destructive. Destructive Tools
require a confirmation button and are never reachable from proactive capture. **No
Tool runs a shell string.** Home control, Lists and memory reach the agent as
built-in Tools under the same mechanism. *Why:* a shell allowlist turns
model-supplied arguments into command injection. Free shell with confirmation
relies on someone reading every command carefully on a phone, forever.

**D17 — Search now, RAG later.** For now, full-text search (SQLite FTS5) over Notes
and Conversation Memory, as an agent Tool that respects visibility and conversation
scope. RAG over the laptop's folders and repositories (sync + embeddings) is a
separate future phase.

### Delivery

**D23 — Vertical slice, Owner first.** The Owner uses a Telegram capture on the
server before the multi-Member model exists. *Why:* weeks of foundation with
nothing new to use is how this kind of project gets abandoned. Adding `owner_id`
later is a cheap backfill.

**D24 — This document is tracked and in English.** *Why:* sessions will run on the
server and on fresh clones, and [CONTRIBUTING](../CONTRIBUTING.md#language) makes
tracked documentation English. It therefore carries **no personal data**: no
resident names, addresses, IPs, chat ids or usernames. Use roles.

## Invariants

Each of these must be pinned by a test before the phase that introduces it closes.

1. Every message addressed to the bot is either handled or becomes a Note — including
   when every provider is down or returns malformed JSON.
2. Every action runs with the Grant of the message's author, never the bot's.
3. A group reply never contains private data: Notes, memory, calendar.
4. Identity is the numeric user id. A username taken over by another account is
   ignored.
5. Capture on a Satellite never waits for the server.
6. Opening the board never calls a model.
7. No Tool runs a shell string. Destructive Tools never run from proactive capture.

## Hardware budget

The server: Intel i5 6th gen (4 cores, AVX2, integrated GPU unusable for inference),
8 GB RAM, shared with AdGuard.

| Tenant | Estimated RAM |
|---|---|
| AdGuard | ~100 MB |
| Home Assistant (Docker) | 0.5–1 GB |
| `ta` daemon | ~150 MB |
| faster-whisper `small` int8, while transcribing | ~1 GB |

LLMs are remote. The estimates are unmeasured; T2.3 replaces them with numbers.

## Phases

Order is dependency order. `[ ]` pending, `[x]` done. Each task names where it lands
and when it is done.

Documentation is part of every task, not a phase at the end. A task that changes
how `ta` is installed, configured or used also updates the README,
[`install.md`](install.md) or [`configuration.md`](configuration.md).

### F0 — Server ready *(manual, user)*

- [x] **T0.1 Walkthrough.** With the `wizard` skill: Docker on DietPi, HA Container
      restored from a backup of the current HA, integrations checked one by one.
      *Done when* `curl` to `/api/` on the server's port 8123, with the token,
      returns 200 and a light answers `ta on`.
- [x] **T0.2 `ta` on the server.** Debian's Python ≥ 3.12, **without** `gi` (the
      calendar moves in F5). systemd user service. *Done when* `ta doctor` on the
      server shows notes and home alive, and the calendar reported unavailable
      with its reason.
- [x] **T0.3 Data cutover.** Copy `ta.db` and the rules to the server; stop the
      laptop daemon. **The user's call and the user's hands** ([AGENTS §6](../AGENTS.md#6-dont-touch-the-authors-running-installation)).
- [x] **T0.4 Document the server install.** The README says, near the top, that
      `ta` runs either on one machine (as today) or on a home server with
      Satellites, and that the server is optional. [`install.md`](install.md) gains a
      *Server* layer written from what F0 actually took: prerequisites, Docker and
      Home Assistant, the service with lingering, `TA_HOST`/`TA_TOKEN`, and moving
      an existing database. It is a guide for a stranger, not a copy of the wizard.
      *Done when* someone with a Debian box can follow it without this conversation.

### F1 — Owner-only Telegram capture, and LLM providers

- [x] **T1.1 Provider interface.** *(Landed as `src/ta/providers.py` beside `llm.py`, not a package: see Discoveries.)* `src/ta/llm.py` becomes a package: a `Provider`
      protocol (`complete_json(prompt, schema) -> BaseModel`, `complete_text`),
      `GeminiProvider` (today's code, moved), `DeepSeekProvider` (OpenAI-compatible
      over `httpx`; JSON mode, Pydantic validation, one retry). `LLM` keeps its
      public task methods and routes each through `[llm.tasks]` in `config.toml`.
      The default is `deepseek` and the fallback `gemini`. *Done when* the existing
      tests pass unchanged (including `SpyLLM` in `tests/test_docs.py`, which
      overrides `_structured`), and new tests cover malformed JSON → retry →
      fallback.
- [ ] **T1.2 Usage accounting.** New migration in `db.py` (never edit a released
      one): `llm_usage(provider, task, tokens_in, tokens_out, cost, at)`, with
      prices from config. *Done when* each task call writes one row.
- [ ] **T1.3 Capabilities.** `deepseek` and `telegram` in `capabilities.py`, each
      with why-not and how-to-fix, visible in `ta doctor` and `/health` (presence of
      a secret, never its value).
- [ ] **T1.4 Channel.** `src/ta/channel/`: a `Channel` protocol (inbound message,
      reply, reaction, buttons) and `TelegramChannel` by long polling over `httpx`.
      Add an SDK only if it pays for itself, and say why in the commit. It runs as a
      background task in the daemon loop, under the existing `background` flag, so
      tests never touch the network.
- [ ] **T1.5 Owner capture.** `[channel.telegram] owner = "@…"`, bound to the user
      id on first contact (the seed of D21). Text → `notes.py` parser → `store.py`
      capture → confirmation reply; the asynchronous review as today.
- [ ] **T1.7 Board link from the bot** (D20, pulled forward). On the Owner's first
      contact, and on `/board`, the bot replies privately with
      `/board?code=<one-time code>`. The code is valid for 5 minutes and is
      consumed on first use. The board trades it for the credential it keeps in
      `localStorage`. `TA_TOKEN` itself never goes through the Channel, because a
      chat is stored on the Channel's servers and that token controls the house.
      F6 replaces what the code is traded for (a per-Member session) without
      changing the flow. The trade answers with an **HttpOnly, SameSite=Strict
      session cookie** that `TokenAuth` accepts, because a reload cannot send what
      is in `localStorage` (see Discoveries). *Done when* a test proves a code works
      once, fails after 5 minutes, and fails the second time, and another proves a
      reload of `/board` with the cookie and no query answers 200.
- [ ] **T1.6 Strings.** Every string the bot sends lives in `i18n.py` ([AGENTS §1](../AGENTS.md#1-never-write-a-user-visible-string-in-english-or-in-portuguese)).
- **F1 is done when** a message sent from the phone appears on the board (open it and
  look — [AGENTS §5](../AGENTS.md#5-returns-200-and-contains-the-string-is-not-interface-verification)),
  and `llm_usage` shows its review ran on DeepSeek.

### F2 — Voice

- [ ] **T2.1** `faster-whisper` as an optional extra, plus a `whisper` Capability;
      model and compute type in config.
- [ ] **T2.2** Telegram voice (OGG/Opus) → transcription in a worker thread → the
      same capture path; the transcript is stored with the Note. On failure the
      audio file is kept and the reply says so (invariant 1).
- [ ] **T2.3** Measure the latency and peak RAM on the server; record them in
      Discoveries and correct the hardware budget.

### F3 — Members, Grants, visibility, Lists

- [ ] **T3.1 Schema.** Migration: `members` (channel, external id, username,
      owner flag, persona, created), `notes.owner_id` backfilled to the Owner,
      `lists` (name, scope, owner), `notes.list_id`, Priorities per Member.
- [ ] **T3.2 Visibility.** Every read path in `store.py` filters to own +
      household. *Done when* one test covers list, Digest, reminders, review queue
      and export together — the same shape as the `deleted_at` test.
- [ ] **T3.3 Grants.** `[grants.<name>]` (Entities/groups, Lists, Tools, admin)
      and `[members]` mapping usernames to Grants. Deny by default; invariant 2
      tested.
- [ ] **T3.4 Pairing.** Invited usernames pair on first contact; the Owner is
      notified; groups allowlisted by chat id. Invariant 4 tested.
- [ ] **T3.5 Board.** A List view outside the Horizon ordering. Look at it.

### F4 — Agent, Tools, Receipts, groups

- [ ] **T4.1 `@tool`.** Decorator and loader for `~/.config/ta/tools/`, loading
      each file in isolation like `engine.load_rules`. Metadata: Grant, destructive,
      description, argument schema.
- [ ] **T4.2 Built-in Tools.** Home (over `actuators/home.py`, Grant-checked),
      List add/remove/show, Note search (FTS5 migration over Notes and memory),
      memory search, Persona get/set, and the Satellite's Lighter (once F6 exists).
- [ ] **T4.3 Pre-router and agent loop.** Slash commands and the home grammar
      already in `cli.py`/`config.resolve_targets` resolve with no model. The rest
      goes to tool calling on the routed provider. Timeout or failure → capture.
      Invariant 1 tested with every provider down.
- [ ] **T4.4 Receipts.** `receipts` (conversation, message id, Member, action,
      payload, undo payload, at). An undo button, and "what did you do here?" as a
      reply to the message.
- [ ] **T4.5 Conversation Memory.** Stored per conversation with retention; search
      confined to the current conversation. Invariant 3 tested.
- [ ] **T4.6 Proactive group capture.** Classifier per group message →
      actionable into a household List with ✅ and undo; the rest to memory only.
      Invariant 7 tested.
- [ ] **T4.7 Split proposal** (D5), with buttons.

### F5 — Calendar and the pushed Digest

- [ ] **T5.1 Google Calendar.** A provider behind the interface
      `sensors/calendar.py` already exposes to rules (`now`/`today` and their
      Portuguese aliases must keep working — [AGENTS §3](../AGENTS.md#3-a-rename-can-break-code-that-is-not-in-this-repository)).
      OAuth paste-back through `/conectar_agenda`; refresh token per Member, file
      mode 0600 at minimum.
- [ ] **T5.2** ADR 0007's propose-and-confirm through Channel buttons.
- [ ] **T5.3 Digest sections** as independent sources: calendar, chaseable Notes,
      household Lists, weather (confirm Open-Meteo's terms), server health from
      `/proc`, `/sys/class/thermal`, disk and `llm_usage` (admins only).
- [ ] **T5.4 Schedule** per Member in `scheduler.py`, with no retroactive flood
      after downtime. The prose is optional.

### F6 — Satellite

- [ ] **T6.1** Per-Member Satellite token issued through the Channel; the CLI
      addresses the remote server.
- [ ] **T6.2** Local capture queue, flushed with original timestamps. Invariant 5
      tested.
- [ ] **T6.3** Satellite process: microphone watcher → server; a persistent
      outbound connection receives local actions (Lighter, notification). The
      meeting Rule works again, running on the server.
- [ ] **T6.4** Magic link for the board (D20).
- [ ] **T6.5 Document Satellites.** In [`install.md`](install.md) and the README:
      what a Satellite is, how to add a computer (install, get the token from the
      Channel, point it at the server), what works offline, and how to remove one.
      Update [`configuration.md`](configuration.md) with every new variable.

### F7 — RAG over Satellite folders *(future, deliberately unplanned)*

### F8 — Web configuration *(requested 2026-09-25, not yet designed)*

The user asked for a web page to change the configuration, locked behind a login
and password. If handling the password properly is hard, a credential defined in
`.env` is acceptable to them.

What is settled: it is a web page, and it has its own login. It is **not**
cryptographically hard. The password is stored as a salted hash (`hashlib.scrypt`,
standard library), never encrypted and never in plain text, so the hash can live in
`.env` (`TA_ADMIN_PASSWORD_HASH`, set by a CLI command that prompts for the
password).

**Open, and to be asked of the user before designing:**
1. What it edits: everything in `config.toml` (aliases, groups, Grants, Members,
   LLM routes, Digest schedule), or only part of it. Secrets in `.env` stay out of
   the page either way, which is the proposal.
2. Who logs in: only the Owner, or any Member whose Grant says admin.
3. How it relates to D20. The board is entered by magic link from the Channel.
   Should the config page reuse that session and ask for the password on top
   (proposed: two factors for the part that controls permissions), or stand on
   its own?
4. Whether it writes `config.toml` in place, which loses the comments in it, or
   keeps settings in the database with `config.toml` as seed.

## Discoveries

What contradicted the plan, as it happened. Decide per row whether it becomes an
ADR amendment, a new decision (ask the user), or just a note.

| Date | Task | What contradicted the plan | Consequence |
|---|---|---|---|
| 2026-09-25 | D14 | Google's device authorisation flow does not allow Calendar scopes | OAuth by pasting back the `localhost` redirect; the app must be in Production, or refresh tokens expire in seven days |
| 2026-09-25 | D14 | ADR 0004 had already rejected our own OAuth, partly because a Workspace admin may block an unverified app on the work account | Recorded as an open risk on D14, and T5.1 checks the work account before anything else |
| 2026-09-25 | T0.3 | The CLI cannot reach a remote daemon: `Config.base_url` always resolves to this machine, and the CLI sends no `TA_TOKEN` | The user accepted losing `ta` on the laptop, and the meeting Rule, from the cutover until F6. Pulling T6.1's CLI half forward into F1 is cheap if that loss starts to hurt |
| 2026-09-25 | T0.2 | The server runs Debian 12 (Bookworm), with no Python at all, and Bookworm's Python is 3.11, below `requires-python` | The user chose a uv-managed Python 3.12 in `~/.local`, installed with `make install SYS_PYTHON=…`, over a distribution upgrade of the box that serves the house DNS. The server needs no `gi`, so ADR 0005's reason for the system Python does not apply there. T0.4 must document it |
| 2026-09-25 | T0.1 | DietPi ships Dropbear as its SSH server, with no `sftp-server`, so modern `scp` (which speaks SFTP) fails with "Connection closed" | The wizard copies over `ssh` with `cat`/`tar`. T0.4 must not tell people to `scp` to a DietPi box |
| 2026-09-25 | T0.2 | `loginctl enable-linger` fails with "Failed to connect to bus": DietPi ships without `systemd-logind` and without a system D-Bus, so there is no `systemctl --user` on it | On the server `ta` runs as a **system** unit generated from `systemd/ta.service` (`User=` added, `WantedBy=multi-user.target`). The user unit exists for the desktop session (notifications, Lighter, calendar), none of which exists on a server. A `make install-server-service` target and T0.4 should make this the documented path |
| 2026-09-25 | T1.7 | On a phone the board 401s on every reload. `TokenAuth` guards `/board` itself, the board strips `?token=` from the address bar (ADR 0012), and a page load cannot send the header the JS builds from `localStorage`. `test_security.py` pins the 401 on a bare `/board` | T1.7's code exchange sets a session cookie, so the credential travels with the page load. The alternative, serving the HTML with no credential because it holds no data, was left for the user to weigh if the cookie proves awkward |
| 2026-09-25 | T1.1 | The plan said `llm.py` would become a package. Tests subclass `LLM` and override `_structured(prompt, schema, system)`, so the task could not be a new argument | Providers went to `src/ta/providers.py`, and `LLM`'s public surface is unchanged: every pre-existing test passed untouched. The task travels in a `ContextVar`, set by a decorator on each task method and by `for_task()` in `priorities.py` |
| 2026-09-25 | T1.1 | DeepSeek's current model is `deepseek-flash`, not the `deepseek-chat` older examples use. Its JSON mode needs the word "json" in the prompt, and its docs warn the content can come back empty | Default `deepseek-flash`, pinnable with `TA_DEEPSEEK_MODEL`. The schema is sent in the system prompt, and an empty answer counts as one more invalid attempt |
| 2026-09-25 | T1.3 | With DeepSeek as the default, the `ai` Capability still only looked at `GEMINI_API_KEY` | Folded into T1.1: `ai` is alive with either key. T1.3 keeps the `telegram` Capability |
