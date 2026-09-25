# Configuration

Two places, with a clean split:

- **`.env`** — secrets and anything the systemd unit needs at boot. Never
  committed; the unit reads it as an `EnvironmentFile`.
- **`~/.config/ta/config.toml`** — preferences that are not secret. Aliases for
  your house, extra groups, the language.

Neither is required. A fresh install with neither file works — it just has no
Home Assistant, no AI, and no short names for your lamps.

Copy [`.env.example`](../.env.example) and
[`examples/config.toml`](../examples/config.toml) to get started; `ta doctor`
creates the second one for you.

## Environment variables

Every one of these is optional.

### The daemon

| Variable | Default | What it does |
|---|---|---|
| `TA_HOST` | `127.0.0.1` | Address to listen on. Anything else **requires** `TA_TOKEN` or the daemon refuses to start |
| `TA_PORT` | `7777` | Port |
| `TA_TOKEN` | *(none)* | Shared secret required from any client that is not on this machine. See [`SECURITY.md`](../SECURITY.md) |
| `TA_PUBLIC_URL` | *(guessed)* | The address a phone uses to reach the board, for the link the Telegram bot sends with `/board`. Unset, it is this machine's LAN address and `TA_PORT`, which is right for a home server and wrong behind a reverse proxy |
| `TA_DB` | `~/.local/share/ta/ta.db` | Where the SQLite file lives. `make demo` uses this to stay away from your real notes |
| `TA_LANG` | your locale | `pt` or `en`. Governs the capture parser, the interface and the model's output language ([ADR 0013](adr/0013-one-language-at-a-time.md)) |

`TA_HOST` and `TA_TOKEN` travel together on purpose. The daemon has no
authentication of its own, and it serves every note you have written plus the
credential that controls your house.

### Home Assistant

| Variable | Default | What it does |
|---|---|---|
| `HA_URL` | `http://localhost:8123` | Where your Home Assistant is |
| `HA_TOKEN` | *(none)* | A long-lived access token. Without it, the `home` commands say so and exit |
| `TA_ECHOS` | *(none)* | Comma-separated `media_player` entities for voice announcements |

The name uses an underscore for a boring but real reason: `HA-KEY` with a hyphen
is not a valid environment variable name, and neither your shell nor systemd will
load it. That cost an afternoon once.

### AI

| Variable | Default | What it does |
|---|---|---|
| `DEEPSEEK_API_KEY` | *(none)* | Enables DeepSeek, the default provider for every AI task |
| `TA_DEEPSEEK_MODEL` | `deepseek-flash` | Pins a DeepSeek model |
| `GEMINI_API_KEY` | *(none)* | Enables Gemini, the fallback provider. Either key alone is enough to turn the AI features on; without both they fail with a message saying so, and everything else is unaffected |
| `TA_GEMINI_MODEL` | `gemini-flash-latest` | Pins a specific model. The default is a moving alias — it never goes stale, at the cost of being able to change behaviour on its own |
| `TA_AUTO_REVIEW` | `1` | `0` keeps the key but stops the automatic second pass over each capture |

See [ai.md](ai.md) for what each feature costs in model calls.

### Telegram bot

| Variable | Default | What it does |
|---|---|---|
| `TA_WHISPER_MODEL` | `small` | The faster-whisper model for voice notes. `base` is faster and worse; `medium` needs more RAM than a small server has to spare |
| `TA_WHISPER_COMPUTE` | `int8` | Its precision on the CPU. `int8` is what keeps `small` near a gigabyte |
| `TA_WHISPER_MAX_SECONDS` | `600` | Longer audio is kept and captured as a placeholder instead of transcribed, so one long recording cannot hold the only transcription slot |
| `TELEGRAM_BOT_TOKEN` | *(none)* | The token @BotFather gives you. The bot also needs an owner in `config.toml` (below); with only one of the two it does not poll at all |

Which provider answers which task is set in `config.toml` (below). A provider
with no key is skipped, not failed, so a Gemini-only installation keeps working
exactly as before ([ADR 0018](adr/0018-llm-providers-routed-per-task.md)).

## `~/.config/ta/config.toml`

Respects `XDG_CONFIG_HOME` if you set it.

```toml
# Short names for entity_ids, so the command line does not need the full thing.
# `ta entities` lists what your Home Assistant actually exposes.
[aliases]
bedroom = "light.bedroom_lamp"
fan = "switch.fan_socket_1"

# Added to the built-ins, which you never need to declare:
#   luz, luzes  -> light.
#   tomada(s)   -> switch.
#   tudo        -> light. and switch.
# Repeating a built-in name replaces it.
[groups]
office = ["light.", "switch."]

# Overridden by TA_LANG if that is set. `ta lang en` writes this line for you.
lang = "en"

# Which AI provider answers. These are the defaults; you only need the section
# to change them. `fallback = ""` turns the fallback off.
[llm]
default = "deepseek"
fallback = "gemini"

# Per task, overriding the default. The tasks: review_capture (the second pass
# over each capture), organize, detect_event, digest_prose, priorities.
[llm.tasks]
organize = "gemini"

# The Telegram username allowed to talk to the bot. It is only used once: the first
# message from it binds that account's numeric id, and from then on the id is what
# counts, so a changed or stolen username does not change who the bot obeys.
[channel.telegram]
owner = "your_username"

# USD per million tokens, for the cost the Digest reports. DeepSeek's models ship
# with their peak price (an upper bound: off-peak is half). Gemini ships with none,
# because the free tier costs nothing and this cannot tell which tier you are on;
# an unpriced call is counted as "unknown", never as free.
[llm.prices.gemini-flash-latest]
input = 0.30
output = 2.50
```

A provider name that does not exist is ignored with a warning in the log, rather
than quietly sending that task to the fallback forever.

**No secrets here.** This file is not committed, but it is not treated as
sensitive either — it exists to be readable and edited by hand.

A broken TOML file does not stop the daemon: it logs a warning and carries on
with no aliases. Failing to boot over a convenience file would be
disproportionate; failing *silently* would be worse, because the symptom is
`ta on bedroom` quietly not working any more.

## Where everything lives

| What | Where | Why |
|---|---|---|
| Your notes | `~/.local/share/ta/ta.db` | XDG data. A plain SQLite file — `ta export` is the escape hatch |
| Your config | `~/.config/ta/config.toml` | XDG config |
| Your rules | `~/.config/ta/rules/*.py` | Yours, so `git pull` never conflicts with them |
| Secrets | `<repo>/.env` | Gitignored; read by the systemd unit |
| Example rules | `<repo>/examples/rules/` | Versioned documentation — **not loaded** |
| Backups before migrations | next to the database | Made automatically when a migration is pending |

Nothing you own lives inside the repository
([ADR 0014](adr/0014-user-config-and-rules-in-xdg.md)). If you used a very
early version and had rules in `<repo>/rules/`, they still load — `ta doctor`
copies them to the new place without deleting the originals.

## Resolution order

For the language, most specific wins:

```
TA_LANG  →  config.toml  →  your system locale  →  en
```

`ta lang` with no argument prints the language **and where it came from**, which
is the part you actually want when it is not what you expected.

For entity names, `ta on <term>` stops at the first match:

1. A literal `entity_id` — `ta on light.bedroom_lamp`
2. A group — `ta on luz`, `ta on tudo`
3. An alias from `config.toml` — `ta on bedroom`
4. A room, by substring of the entity's name or id — `ta on kitchen`

Step 4 prefers lights: "turn on the kitchen" means the lamp, not a plug that
happens to be in there.
