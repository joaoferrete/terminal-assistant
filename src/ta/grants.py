"""What each Member may do, from `config.toml` (D12, ADR 0016).

    [members.ana]            # the Telegram username the Owner invites
    grants = ["morador"]

    [grants.morador]
    entities = ["light.sala", "luz"]   # entity_ids, domains ("switch.") or groups
    lists = ["compras"]
    tools = []                          # agent Tools, from F4
    admin = false                       # server health, media, the ringlight

**Deny by default**: a Member with no Grant can capture and read their own Notes,
and nothing else. The Owner holds every Grant without declaring any.

A Grant is a *Grant* and not a *role* because "role" already means a Note's
role — Task, Reminder (CONTEXT.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger("ta")


@dataclass(frozen=True)
class Grant:
    name: str
    entities: tuple[str, ...] = ()
    lists: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    admin: bool = False


@dataclass(frozen=True)
class Permissions:
    """The union of a Member's Grants, with the Owner's all-access as a flag."""

    owner: bool = False
    entity_patterns: tuple[str, ...] = ()
    lists: frozenset[str] = field(default_factory=frozenset)
    tools: frozenset[str] = field(default_factory=frozenset)
    admin: bool = False

    def entity(self, entity_id: str) -> bool:
        if self.owner:
            return True
        return any(
            entity_id == p or (p.endswith(".") and entity_id.startswith(p))
            for p in self.entity_patterns
        )

    def list_(self, name: str) -> bool:
        return self.owner or name.lower() in self.lists

    def tool(self, name: str) -> bool:
        return self.owner or name in self.tools

    @property
    def is_admin(self) -> bool:
        return self.owner or self.admin


OWNER_PERMISSIONS = Permissions(owner=True)
NOTHING = Permissions()


def _strings(value, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    log.warning("config.toml: %s must be a list of strings; ignored", where)
    return ()


def load(raw: dict) -> dict[str, Grant]:
    """`[grants.*]` → Grants. A malformed one is dropped loudly, never widened."""
    out: dict[str, Grant] = {}
    for name, body in (raw.items() if isinstance(raw, dict) else ()):
        if not isinstance(body, dict):
            log.warning("config.toml: grants.%s must be a table; ignored", name)
            continue
        out[name] = Grant(
            name=name,
            entities=_strings(body.get("entities"), f"grants.{name}.entities"),
            lists=_strings(body.get("lists"), f"grants.{name}.lists"),
            tools=_strings(body.get("tools"), f"grants.{name}.tools"),
            admin=body.get("admin") is True,
        )
    return out


def invited(raw: dict) -> dict[str, tuple[str, ...]]:
    """`[members.*]` → {handle: grant names}. Handles are Telegram usernames."""
    out: dict[str, tuple[str, ...]] = {}
    for handle, body in (raw.items() if isinstance(raw, dict) else ()):
        names = _strings((body or {}).get("grants") if isinstance(body, dict) else None,
                         f"members.{handle}.grants")
        out[str(handle).lstrip("@").lower()] = names
    return out


def permissions(
    handle: str | None,
    *,
    is_owner: bool,
    members: dict[str, tuple[str, ...]],
    grants: dict[str, Grant],
    groups: dict[str, tuple[str, ...]],
) -> Permissions:
    if is_owner:
        return OWNER_PERMISSIONS
    names = members.get((handle or "").lower(), ())
    unknown = [n for n in names if n not in grants]
    if unknown:
        # Named but never defined: deny, and say which, rather than a silent
        # member-with-nothing that looks like a bug in the bot.
        log.warning("config.toml: member %s names undefined grant(s) %s", handle, unknown)
    chosen = [grants[n] for n in names if n in grants]
    patterns: list[str] = []
    for g in chosen:
        for e in g.entities:
            # A group name (`luz`, `tudo`, or the user's own) expands to its
            # domain prefixes, so a Grant can say "the lights" in the words the
            # command line already uses.
            patterns += list(groups.get(e, (e,)))
    return Permissions(
        entity_patterns=tuple(patterns),
        lists=frozenset(n.lower() for g in chosen for n in g.lists),
        tools=frozenset(t for g in chosen for t in g.tools),
        admin=any(g.admin for g in chosen),
    )
