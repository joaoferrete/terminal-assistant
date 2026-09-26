"""Configuration: one place, read from the environment.

Secrets never come from a versioned file. The systemd unit points an
`EnvironmentFile` at the `.env` in the root, which is in `.gitignore`.

Variable names have to be valid for both the shell and systemd: letters, digits
and `_`. A hyphen works in neither — that was a real mistake early in the
project, and it cost an afternoon.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("ta")

# Loopback by default. The board on a phone is still possible, but it now takes
# two deliberate acts — `TA_HOST` and `TA_TOKEN` — because the daemon exposes
# every note, the command over the house and the model key, all with no
# credential (ADR 0012). Whoever reads SECURITY.md already worries; whoever
# follows the step-by-step is the one who does not know they should.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7777

# Addresses where no other machine's network reaches the daemon. Outside this
# list `TA_TOKEN` is mandatory and the daemon refuses to start without it.
LOOPBACK = ("127.0.0.1", "::1", "localhost")


class ConfigError(RuntimeError):
    """Configuration that cannot be fixed at runtime. The daemon does not start."""


def env_file() -> Path:
    """The project's `.env` — the same one systemd's `EnvironmentFile` points at."""
    return Path(__file__).resolve().parents[2] / ".env"


def load_env_file() -> Path | None:
    """Load `.env` into the process environment, without overwriting what is there.

    The daemon does **not** need this: systemd already hands it the file. Who
    needs it is the CLI, and `ta doctor` in particular — without it, it reported
    `HA_TOKEN is not set` on a machine where the token was configured and
    working, because the CLI process simply cannot see the file.

    A false negative in a diagnosis is worse than no diagnosis: it sends people
    to fix what is not broken.

    Not overwriting the existing environment matters: `TA_LANG=en ta doctor` has
    to keep winning over the line in the file.
    """
    path = env_file()
    if not path.exists():
        return None
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip("\"'")
    except OSError as e:
        log.warning("%s could not be read: %s", path, e)
        return None
    return path


@dataclass(frozen=True)
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    ha_url: str = "http://localhost:8123"
    ha_token: str | None = None
    gemini_api_key: str | None = None
    deepseek_api_key: str | None = None
    telegram_token: str | None = None
    # The address a phone uses to reach the board, for the link the bot sends.
    # Unset, it is guessed from this machine's LAN address, which is right for a
    # home server and wrong behind a reverse proxy.
    public_url: str | None = None
    # The daemon's OWN credential, not a third party's. Only required when the
    # bind leaves loopback; on loopback it stays None and local use is unchanged.
    token: str | None = None
    # Echo device(s) for voice announcements. Empty = nobody to speak to, and the
    # Reminder still warns on screen — the reliable path never depends on this.
    echo_entities: tuple[str, ...] = ()

    # The LLM's second pass over each captured Note. On by default; it costs one
    # model call per capture, and `TA_AUTO_REVIEW=0` turns it off without
    # touching code. Off, capture still works with the regex alone.
    auto_review: bool = True

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            host=os.environ.get("TA_HOST", DEFAULT_HOST),
            port=int(os.environ.get("TA_PORT", DEFAULT_PORT)),
            ha_url=os.environ.get("HA_URL", "http://localhost:8123").rstrip("/"),
            ha_token=os.environ.get("HA_TOKEN") or None,
            gemini_api_key=os.environ.get("GEMINI_API_KEY") or None,
            deepseek_api_key=os.environ.get("DEEPSEEK_API_KEY") or None,
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
            public_url=os.environ.get("TA_PUBLIC_URL") or None,
            token=os.environ.get("TA_TOKEN") or None,
            auto_review=os.environ.get("TA_AUTO_REVIEW", "1") not in ("0", "false", "no"),
            echo_entities=tuple(
                e.strip() for e in os.environ.get("TA_ECHOS", "").split(",") if e.strip()
            ),
        )

    @property
    def base_url(self) -> str:
        """The address the CLI uses to reach the daemon."""
        host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host  # noqa: S104
        return f"http://{host}:{self.port}"

    @property
    def exposed(self) -> bool:
        """Whether the bind reaches another machine. `0.0.0.0` and a LAN IP do."""
        return self.host not in LOOPBACK

    def check(self) -> None:
        """Refuse a configuration that would expose the daemon with no credential.

        Fails loudly and early, with the fix in the message — the same spirit as
        `make check-gi`. A daemon that starts and is only discovered to be open
        afterwards is worse than one that does not start: nobody rereads the boot
        log.
        """
        if self.exposed and not self.token:
            from .i18n import t

            raise ConfigError(t("config.exposed_without_token", host=self.host))


# ── User configuration ──────────────────────────────────────────────────────
# Aliases and groups live in `~/.config/ta/config.toml`, not here. While the
# repository belonged to one person, having `bedroom = light.lamp` fixed in the
# source was practical. Opened up, it would mean configuring your own house
# requires editing the installed package — a change lost on any reinstall
# (ADR 0014).
#
# The path follows the same pattern as `db.default_db_path()`, which was already
# right.
def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ta"


def config_file() -> Path:
    return config_dir() / "config.toml"


# Groups by domain: `ta on luz` turns on every light. Unlike the aliases, these
# are not personal — they hold for any house — so they ship built in and the
# user's file only adds to them.
#
# The KEYS stay in Portuguese: they are what the user types, the same category as
# `!alta` and `@sexta`. Renaming them would break a command somebody has in their
# fingers, which is exactly what the alias table exists to prevent.
DEFAULT_GROUPS: dict[str, tuple[str, ...]] = {
    "luz": ("light.",),
    "luzes": ("light.",),
    "tomada": ("switch.",),
    "tomadas": ("switch.",),
    "tudo": ("light.", "switch."),
}


@lru_cache(maxsize=1)
def _user_config() -> dict:
    """Read `config.toml` once. Missing or unreadable is not an error.

    A house with no aliases works: `ta on light.whatever` is still exact, and
    `ta on bedroom` matches by substring against Home Assistant's own names.
    Failing the daemon over a convenience file would be disproportionate — but
    failing **silently** over broken TOML would be worse, so that becomes a log.
    """
    path = config_file()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("%s ignored: %s", path, e)
        return {}


def entity_aliases() -> dict[str, str]:
    """Short names for `entity_id`, from the user's file. May be empty."""
    raw = _user_config().get("aliases", {})
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def groups() -> dict[str, tuple[str, ...]]:
    """The built-in groups, plus the user's. The user's wins on a conflict."""
    from_user = _user_config().get("groups", {})
    extras = (
        {str(k): tuple(v) for k, v in from_user.items() if isinstance(v, list)}
        if isinstance(from_user, dict)
        else {}
    )
    return {**DEFAULT_GROUPS, **extras}


# Which `entity_id`s feed `ta temp` and `ta router`. They were HARDCODED in
# `actuators/home.py` — eight ids from one specific house — which ADR 0014 had
# already forbidden for the aliases and did not catch here. For anybody else, the
# two commands returned nothing but nulls, silently.
#
# Empty is the default and is a valid state: with this unset, `ta temp` says
# there is no sensor rather than lying with blanks.
SENSOR_ROLES = (
    "weather", "external_ip", "download", "upload",
    "outlet", "watts", "volts", "amps", "kwh_total",
)


def sensors() -> dict[str, str]:
    """From sensor role to `entity_id`, from the user's file."""
    raw = _user_config().get("sensors", {})
    if not isinstance(raw, dict):
        return {}
    return {k: str(v) for k, v in raw.items() if k in SENSOR_ROLES and v}


def _strip_accents(s: str) -> str:
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
    )


def _commandable(e: dict) -> bool:
    """Whether the Entity is an appliance, rather than a setting of one.

    A smart plug can expose two Entities: the socket, with `device_class: outlet`,
    and the child lock, with **no device_class at all**. `ta on tudo` was turning
    on the lock, which is configuration and not an appliance.

    Home Assistant's REST API does not expose `entity_category`, so the presence
    of `device_class` is the available signal — and it is a signal of principle,
    not a name match. It applies only to groups and rooms: naming the Entity
    explicitly still turns on exactly what you asked for.
    """
    if e["entity_id"].startswith("light."):
        return True
    return bool(e.get("attributes", {}).get("device_class"))


def resolve_entity(name: str) -> str:
    """Translate an alias into an entity_id. A value with a dot already is one."""
    if "." in name:
        return name
    return entity_aliases().get(name, name)


def resolve_targets(term: str, entities: list[dict]) -> list[str]:
    """Resolve a term into a LIST of entity_id.

    The order matters, most specific to broadest:

      1. `light.lamp`   — already an entity_id
      2. `luz`, `tudo`  — a group by domain
      3. `bedroom`      — an explicit alias
      4. `kitchen`      — a room, by substring of the name

    A room matches by substring because Home Assistant does not expose areas over
    the REST API, and because it picks up future lights in the same place without
    anyone updating a list. The search ignores accents and case.
    """
    if "." in term:
        return [term]

    key = _strip_accents(term)

    all_groups = groups()
    if key in all_groups:
        prefixes = all_groups[key]
        return [
            e["entity_id"]
            for e in entities
            if e["entity_id"].startswith(prefixes) and _commandable(e)
        ]

    aliases = entity_aliases()
    if key in aliases:
        return [aliases[key]]

    def matches(e: dict, prefix: str) -> bool:
        if not e["entity_id"].startswith(prefix) or not _commandable(e):
            return False
        name = _strip_accents(e.get("attributes", {}).get("friendly_name") or "")
        return key in _strip_accents(e["entity_id"]) or key in name

    # Lights first: "turn on the bedroom" means the lamp, not the fan. It only
    # falls through to switches if no light matches.
    lights = [e["entity_id"] for e in entities if matches(e, "light.")]
    if lights:
        return lights
    return [e["entity_id"] for e in entities if matches(e, "switch.")]


# Which provider answers each LLM task (ADR 0018). DeepSeek is the default
# because it is cheaper, Gemini the fallback; `config.toml` can change both and
# route single tasks elsewhere:
#
#     [llm]
#     default = "deepseek"
#     fallback = "gemini"
#     [llm.tasks]
#     organize = "gemini"
DEFAULT_LLM_ROUTING = ("deepseek", "gemini")


def llm_routing() -> tuple[str, str | None, dict[str, str]]:
    """(default, fallback, per-task routes), with unknown names dropped loudly.

    A typo such as `deepseak` would otherwise route the task to a provider that
    does not exist, which the chain skips — and the task would quietly run on the
    fallback forever, with nobody knowing the setting was ignored.
    """
    from .providers import PROVIDER_NAMES

    raw = _user_config().get("llm", {})
    raw = raw if isinstance(raw, dict) else {}

    def known(value, where: str) -> str | None:
        if value in PROVIDER_NAMES:
            return value
        log.warning("config.toml: %s = %r is not a provider %s; ignored", where, value,
                    PROVIDER_NAMES)
        return None

    default = DEFAULT_LLM_ROUTING[0]
    if "default" in raw:
        default = known(raw["default"], "llm.default") or default
    fallback: str | None = DEFAULT_LLM_ROUTING[1]
    if "fallback" in raw:
        fallback = known(raw["fallback"], "llm.fallback") if raw["fallback"] else None

    tasks = raw.get("tasks", {})
    routes = {}
    for task, value in (tasks.items() if isinstance(tasks, dict) else ()):
        if (name := known(value, f"llm.tasks.{task}")) is not None:
            routes[str(task)] = name
    return default, fallback, routes


def llm_prices() -> dict:
    """USD per million tokens, per model: the built-ins, plus `[llm.prices]`.

        [llm.prices.gemini-flash-latest]
        input = 0.30
        output = 2.50

    A malformed entry is dropped with a warning rather than priced at zero,
    because zero would read as "free" in the Digest.
    """
    from .usage import DEFAULT_PRICES, Price

    raw = _user_config().get("llm", {})
    raw = raw.get("prices", {}) if isinstance(raw, dict) else {}
    prices = dict(DEFAULT_PRICES)
    for model, entry in (raw.items() if isinstance(raw, dict) else ()):
        try:
            prices[str(model)] = Price(input=float(entry["input"]), output=float(entry["output"]))
        except (KeyError, TypeError, ValueError):
            log.warning("config.toml: llm.prices.%s needs numeric input and output; ignored",
                        model)
    return prices


def telegram_owner() -> str | None:
    """The Owner's Telegram username from `[channel.telegram] owner`, normalised.

    Only used to *pair* (D21): the first message from this username binds its
    numeric id, and from then on the id is what counts. `@` and case are dropped
    because Telegram usernames are case-insensitive and people type the `@`.
    """
    raw = _user_config().get("channel", {})
    raw = raw.get("telegram", {}) if isinstance(raw, dict) else {}
    owner = raw.get("owner") if isinstance(raw, dict) else None
    if not isinstance(owner, str) or not owner.strip().lstrip("@"):
        return None
    return owner.strip().lstrip("@").lower()


def grants_config() -> tuple[dict, dict]:
    """(`[members]`, `[grants]`) from config.toml, parsed by `grants.py`."""
    from . import grants

    raw = _user_config()
    return grants.invited(raw.get("members", {})), grants.load(raw.get("grants", {}))


DEFAULT_LISTS = {"compras": "household"}


def lists_config() -> dict[str, str]:
    """`[lists]` — name → scope. `compras` exists unless the file says otherwise.

    Declared by the Owner (decided 2026-09-25), so the model can only put items in
    Lists that exist, and "Compras", "compras do mês" and "mercado" do not grow
    side by side. Only household Lists are declared here for now: a personal List
    belongs to one Member, and creating those is F4's, from the chat or the board.
    """
    raw = _user_config().get("lists")
    if raw is None:
        return dict(DEFAULT_LISTS)
    out = {}
    for name, scope in (raw.items() if isinstance(raw, dict) else ()):
        if scope == "household":
            out[str(name).lower()] = scope
        else:
            log.warning("config.toml: lists.%s = %r; only \"household\" is declared here "
                        "(personal Lists are created from the chat, F4)", name, scope)
    return out


def telegram_groups() -> set[str]:
    """`[channel.telegram] groups` — the chat ids of groups the bot listens to.

    By id, never by title: a group's title is editable by any of its members.
    """
    raw = _user_config().get("channel", {})
    raw = raw.get("telegram", {}) if isinstance(raw, dict) else {}
    groups = raw.get("groups", []) if isinstance(raw, dict) else []
    return {str(g) for g in groups} if isinstance(groups, list) else set()


def chat_config() -> dict:
    """`[chat]` — the soft guardrail and the hard ceilings (D29, D30).

        [chat]
        house_rules = "Não dê diagnóstico médico; sugira procurar um profissional."
        daily_usd_per_member = 0.50
        monthly_usd_household = 10.0

    A ceiling of 0 or less, or a malformed one, falls back to the default rather
    than to "no ceiling": a typo must not remove the limit.
    """
    raw = _user_config().get("chat", {})
    raw = raw if isinstance(raw, dict) else {}

    def ceiling(key: str, default: float) -> float:
        value = raw.get(key, default)
        if isinstance(value, int | float) and value > 0:
            return float(value)
        log.warning("config.toml: chat.%s = %r is not a positive number; using %s",
                    key, value, default)
        return default

    rules = raw.get("house_rules", "")
    return {
        "house_rules": rules if isinstance(rules, str) else "",
        "daily_usd_per_member": ceiling("daily_usd_per_member", 0.50),
        "monthly_usd_household": ceiling("monthly_usd_household", 10.0),
    }


def digest_weather() -> dict | None:
    """`[digest]` latitude/longitude/place, for the weather section. Unset, the
    Digest simply has no weather — the house's location is not something to guess.

        [digest]
        latitude = -23.55
        longitude = -46.63
        place = "São Paulo"
    """
    raw = _user_config().get("digest", {})
    raw = raw if isinstance(raw, dict) else {}
    lat, lon = raw.get("latitude"), raw.get("longitude")
    if not (isinstance(lat, int | float) and isinstance(lon, int | float)):
        return None
    return {"latitude": lat, "longitude": lon, "place": str(raw.get("place", ""))}
