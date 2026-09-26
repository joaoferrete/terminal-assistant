"""What the config page reads and writes (F8, D38).

Everything `ta` is configured with, in two files:

- `config.toml` — edited **in place** with tomlkit, so the comments and the order a
  person wrote survive. Scalar settings are form fields; the tables (aliases,
  Grants, Members…) are edited as TOML text, table by table. Every change is
  validated with the same rules the daemon applies when it reads the file, before
  anything is written, and the file is backed up first.
- `.env` — plain settings are shown and edited; **secrets are write-only**. The
  page learns only whether one is set, never its value.

The page is the Owner's only, behind two factors: their board session, and a
config password stored as a scrypt hash. The config session is short — thirty
minutes — because it can rewrite who may do what in the house.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import tomlkit

# ── The password and the config session ─────────────────────────────────────
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1
CONFIG_TTL = 30 * 60
CONFIG_COOKIE = "ta_config"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        kind, n, r, p, salt, digest = (stored or "").split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                             n=int(n), r=int(r), p=int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, base64.b64decode(digest))


def _mac(key: str, payload: str) -> str:
    return hmac.new(key.encode(), f"config:{payload}".encode(), hashlib.sha256).hexdigest()


def config_cookie(key: str, member_id: int, now: float) -> str:
    """Signed with the password hash itself: changing the password logs every
    config session out, and the hash never leaves the server."""
    issued = int(now)
    return f"c1.{member_id}.{issued}.{_mac(key, f'{member_id}:{issued}')}"


def config_member(key: str | None, value: str | None, now: float) -> int | None:
    if not key or not value:
        return None
    try:
        version, member, issued, mac = value.split(".")
        member_id, at = int(member), int(issued)
    except ValueError:
        return None
    if version != "c1" or not hmac.compare_digest(mac, _mac(key, f"{member_id}:{at}")):
        return None
    return member_id if 0 <= now - at <= CONFIG_TTL else None


# ── config.toml ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Field:
    path: str               # dotted, in config.toml
    kind: str               # text | longtext | number | choice | list | intlist
    choices: tuple = ()
    restart: bool = False    # applies only after a restart


# Scalars: one form field each.
FIELDS: tuple[Field, ...] = (
    Field("lang", "choice", ("", "pt", "en"), restart=True),
    Field("chat.bot_name", "text"),
    Field("chat.bot_personality", "longtext"),
    Field("chat.house_rules", "longtext"),
    Field("chat.daily_usd_per_member", "number"),
    Field("chat.monthly_usd_household", "number"),
    # Empty is "not set": the default applies (deepseek, D3). Without the empty
    # choice the page drew a blank select that could not be put back.
    Field("llm.default", "choice", ("", "deepseek", "gemini"), restart=True),
    Field("llm.fallback", "choice", ("", "deepseek", "gemini"), restart=True),
    Field("digest.place", "text"),
    Field("digest.latitude", "number"),
    Field("digest.longitude", "number"),
    Field("channel.telegram.owner", "text", restart=True),
    Field("channel.telegram.groups", "intlist"),
    Field("rag.folders", "list"),
)

# Tables: edited as TOML text, one each. `restart` for those read only at boot.
TABLES: dict[str, bool] = {
    "aliases": False, "groups": False, "sensors": False, "lists": True,
    "members": False, "grants": False, "llm.tasks": True, "llm.prices": True,
}


class Invalid(ValueError):
    """What is wrong, in words for the page. Nothing was written."""


def _get(doc, dotted: str):
    node = doc
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set(doc, dotted: str, value) -> None:
    *parents, leaf = dotted.split(".")
    node = doc
    for part in parents:
        if part not in node or not isinstance(node[part], dict):
            node[part] = tomlkit.table()
        node = node[part]
    if value is None or value == "" or value == []:
        if leaf in node:
            del node[leaf]           # an empty field removes the setting
    else:
        node[leaf] = value


def read(path: Path) -> dict:
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    fields = {}
    for f in FIELDS:
        v = _get(doc, f.path)
        fields[f.path] = v.unwrap() if hasattr(v, "unwrap") else v
    tables = {}
    for name in TABLES:
        t = _get(doc, name)
        tables[name] = tomlkit.dumps(t).strip() if t is not None else ""
    return {"fields": fields, "tables": tables,
            "restart": [f.path for f in FIELDS if f.restart]
            + [n for n, r in TABLES.items() if r]}


def _field_value(f: Field, raw):
    if raw is None or raw == "":
        return None
    if f.kind == "number":
        try:
            n = float(raw)
        except (TypeError, ValueError):
            raise Invalid(f"{f.path}: not a number") from None
        if f.path.startswith("chat.") and n <= 0:
            raise Invalid(f"{f.path}: must be above zero")
        return n
    if f.kind == "choice":
        if raw not in f.choices:
            raise Invalid(f"{f.path}: must be one of {', '.join(c for c in f.choices if c)}")
        return raw
    if f.kind in ("list", "intlist"):
        items = raw if isinstance(raw, list) else [x.strip() for x in str(raw).split("\n")]
        items = [x for x in items if str(x).strip()]
        if f.kind == "intlist":
            try:
                return [int(x) for x in items]
            except ValueError:
                raise Invalid(f"{f.path}: one chat id per line, as a number") from None
        return [str(x) for x in items]
    return str(raw)


def _check_table(name: str, table: dict) -> None:
    """The same rules the daemon applies on read — but here they refuse, where the
    daemon would log and ignore: the page is where a mistake is cheapest to fix."""
    from .config import SENSOR_ROLES
    from .llm import TASKS
    from .providers import PROVIDER_NAMES

    def strings(v, where):
        if not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            raise Invalid(f"{where}: must be a list of text")

    for key, value in table.items():
        where = f"{name}.{key}"
        if name == "aliases" and not isinstance(value, str):
            raise Invalid(f"{where}: must be an entity_id, as text")
        if name == "groups":
            strings(value, where)
        if name == "sensors" and (key not in SENSOR_ROLES or not isinstance(value, str)):
            raise Invalid(f"{where}: roles are {', '.join(SENSOR_ROLES)}")
        if name == "lists" and value != "household":
            raise Invalid(f'{where}: only "household" Lists are declared here')
        if name == "members":
            if not isinstance(value, dict) or set(value) - {"grants"}:
                raise Invalid(f"{where}: a table with `grants = [...]` only")
            strings(value.get("grants", []), f"{where}.grants")
        if name == "grants":
            if not isinstance(value, dict) or set(value) - {"entities", "lists", "tools",
                                                            "admin"}:
                raise Invalid(f"{where}: entities, lists, tools and admin only")
            for k in ("entities", "lists", "tools"):
                if k in value:
                    strings(value[k], f"{where}.{k}")
            if "admin" in value and not isinstance(value["admin"], bool):
                raise Invalid(f"{where}.admin: true or false")
        if name == "llm.tasks" and (key not in TASKS or value not in PROVIDER_NAMES):
            raise Invalid(f"{where}: tasks are {', '.join(TASKS)}; providers "
                          f"{', '.join(PROVIDER_NAMES)}")
        numeric = isinstance(value, dict) and all(
            isinstance(value.get(k), int | float) for k in ("input", "output"))
        if name == "llm.prices" and not numeric:
            raise Invalid(f"{where}: needs numeric input and output")


def write(path: Path, changes: dict, *, now: datetime | None = None) -> Path | None:
    """Validate everything, then back up, then write. Returns the backup, if any.

    All or nothing: one invalid field and the file is not touched.
    """
    doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    by_path = {f.path: f for f in FIELDS}
    staged = []
    for dotted, raw in (changes.get("fields") or {}).items():
        f = by_path.get(dotted)
        if f is None:
            raise Invalid(f"{dotted}: not a setting")
        staged.append((dotted, _field_value(f, raw)))
    for name, text in (changes.get("tables") or {}).items():
        if name not in TABLES:
            raise Invalid(f"{name}: not a table")
        try:
            parsed = tomlkit.parse(text or "")
        except Exception as e:     # tomlkit's parse errors carry line and column
            raise Invalid(f"{name}: {e}") from None
        _check_table(name, parsed.unwrap())
        table = tomlkit.table()
        for k, v in parsed.body:
            if k is None:
                table.add(v)            # comments and blank lines, kept
            else:
                table.add(k, v)
        staged.append((name, table if parsed.unwrap() else None))

    for dotted, value in staged:
        _set(doc, dotted, value)
    backup = _backup(path, now)
    _atomic_write(path, tomlkit.dumps(doc), mode=0o644)
    return backup


# ── .env ────────────────────────────────────────────────────────────────────
ENV_FIELDS: dict[str, bool] = {      # name → applies only after a restart
    "TA_HOST": True, "TA_PORT": True, "TA_PUBLIC_URL": False, "HA_URL": True,
    "TA_AUTO_REVIEW": True, "TA_GEMINI_MODEL": True, "TA_DEEPSEEK_MODEL": True,
    "TA_WHISPER_MODEL": True, "TA_WHISPER_COMPUTE": True, "TA_WHISPER_MAX_SECONDS": True,
    "ADGUARD_URL": False, "TA_ECHOS": True, "TA_SERVER": True,
}
SECRETS = ("HA_TOKEN", "TA_TOKEN", "GEMINI_API_KEY", "DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN",
           "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ADGUARD_USER", "ADGUARD_PASSWORD")


def _env_lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def read_env(path: Path) -> dict:
    values = {}
    for line in _env_lines(path):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v
    return {
        "fields": {k: values.get(k, "") for k in ENV_FIELDS},
        # Set or not, and nothing else: a value never leaves the server (D38).
        "secrets": {k: bool(values.get(k)) for k in SECRETS},
    }


def write_env(path: Path, updates: dict[str, str], *, now: datetime | None = None) -> Path | None:
    allowed = set(ENV_FIELDS) | set(SECRETS) | {"TA_ADMIN_PASSWORD_HASH"}
    for key, value in updates.items():
        if key not in allowed:
            raise Invalid(f"{key}: not a setting")
        if "\n" in value or "\r" in value:
            raise Invalid(f"{key}: one line only")
    lines = _env_lines(path)
    for key, value in updates.items():
        lines = [x for x in lines if not x.startswith(f"{key}=")]
        if value != "":
            lines.append(f"{key}={value}")
    backup = _backup(path, now)
    _atomic_write(path, "\n".join(lines) + "\n", mode=0o600)
    return backup


# ── Files ───────────────────────────────────────────────────────────────────
def _backup(path: Path, now: datetime | None) -> Path | None:
    if not path.exists():
        return None
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, target)
    return target


def _atomic_write(path: Path, text: str, *, mode: int) -> None:
    """A half-written config would stop the daemon's next boot; a rename is atomic."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    os.chmod(path, mode)
