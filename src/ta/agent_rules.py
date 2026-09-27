"""The house's state, and chat Rules (F10, D46, D52c, D54).

`home_status` answers "is the living room light on?" and gives the agent the real
entity ids a Rule needs, so "when the door opens" becomes `binary_sensor.porta`
and not a guess. A Member reads the Entities their Grant covers; admins read all.
"""

from __future__ import annotations

import json
import re
import unicodedata

from . import chat_rules
from .builtin_tools import ToolContext, _home, _targets
from .tools import ToolResult, allowed, registered, tool

MAX_LISTED = 40
# Not an action of a Rule: the same reason as a Routine's steps, plus Rules
# themselves — a Rule that creates Rules is an automation rewriting automations.
_NOT_AN_ACTION = ("rule_", "routine_create", "routine_delete", "member_", "schedule_")


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text.lower())
                   if not unicodedata.combining(c))


def _readable(ctx: ToolContext, entity_id: str) -> bool:
    p = ctx.turn.permissions
    return p.is_admin or p.entity(entity_id)


def _name(e: dict) -> str:
    return e.get("attributes", {}).get("friendly_name") or e["entity_id"]


@tool(
    description="Read the state of the house: which lights and plugs are on, a sensor, a "
    "door. Empty target lists what is on. Also use it to find the entity_id of a device "
    "before creating a Rule about it.",
    args={"target": "a room, a device name, an entity_id, or empty"},
)
async def home_status(ctx: ToolContext, target: str = "") -> ToolResult:
    try:
        states = await _home(ctx).states()
    except Exception as e:
        return ToolResult(text=f"Home Assistant cannot be reached: {type(e).__name__}")
    visible = [e for e in states if _readable(ctx, e["entity_id"])]
    key = _fold(target.strip())
    if key:
        chosen = [e for e in visible if key in _fold(e["entity_id"]) or key in _fold(_name(e))]
    else:
        chosen = [e for e in visible if e["entity_id"].startswith(("light.", "switch."))
                  and e["state"] == "on"]
        if not chosen:
            return ToolResult(text="no light or plug is on")
    if not chosen:
        return ToolResult(text=f"nothing you may see matches {target!r}")
    return ToolResult(text="\n".join(f"{_name(e)} ({e['entity_id']}): {e['state']}"
                                     for e in chosen[:MAX_LISTED]))


def _action_tool(ctx: ToolContext, name: str):
    chosen = registered().get(name)
    if (chosen is None or not chosen.changes_state or chosen.destructive
            or chosen.name.startswith(_NOT_AN_ACTION) or not allowed(chosen, ctx.turn)):
        return None
    return chosen


def _hhmm(text: str) -> str | None:
    m = re.fullmatch(r"(\d{1,2})(?:[:h](\d{2}))?h?", text.strip().lower())
    if not m or int(m[1]) > 23 or int(m[2] or 0) > 59:
        return None
    return f"{int(m[1]):02d}:{int(m[2] or 0):02d}"


@tool(
    description="Create a Rule that acts on its own when something happens in the house: "
    "'when binary_sensor.porta becomes on after 22:00, turn on the hall light'. The trigger "
    "is an entity reaching a state (find the entity_id with home_status first) or, for the "
    "owner, the microphone. The action is one of your acting tools, or routine_run.",
    args={"name": "a short name for the Rule",
          "when_entity": "the entity_id that triggers it, or empty",
          "when_state": "the state it must reach (on, off, open, home…), or empty for any change",
          "when_mic": "on or off to trigger on the owner's microphone, else empty",
          "after": "only after HH:MM, or empty", "before": "only before HH:MM, or empty",
          "if_entity": "only if this entity_id…, or empty", "if_state": "…is in this state",
          "tool": "the action: an acting tool", "tool_args": "its arguments, as a JSON object",
          "household": "yes to share it with the house, else empty"},
    changes_state=True,
)
async def rule_create(ctx: ToolContext, name: str, tool: str, tool_args: object = "{}",
                      when_entity: str = "", when_state: str = "", when_mic: str = "",
                      after: str = "", before: str = "", if_entity: str = "",
                      if_state: str = "", household: str = "") -> ToolResult:
    from . import members as members_mod

    if not name.strip():
        return ToolResult(text="a Rule needs a name")
    if chat_rules.by_name(ctx.conn, ctx.turn.member_id, name) is not None:
        return ToolResult(text=f"there is already a Rule named {name}")
    if bool(when_entity.strip()) == bool(when_mic.strip()):
        return ToolResult(text="give exactly one trigger: when_entity or when_mic")
    if when_mic.strip():
        member = members_mod.get(ctx.conn, ctx.turn.member_id)
        # The microphone signal is the Owner's laptop (F6): a Rule of someone else's
        # on it would act on the Owner's meetings.
        if not (member and member.is_owner):
            return ToolResult(text="only the owner can make Rules on the microphone")
        if when_mic.strip().lower() not in ("on", "off"):
            return ToolResult(text="when_mic is on or off")
    for entity in (when_entity.strip(), if_entity.strip()):
        if entity and ("." not in entity or not _readable(ctx, entity)):
            return ToolResult(text=f"{entity!r} is not an entity_id you may see; use "
                                   "home_status to find it")
    if if_entity.strip() and not if_state.strip():
        return ToolResult(text="if_entity needs if_state")
    window = []
    for raw in (after, before):
        value = _hhmm(raw) if raw.strip() else None
        if raw.strip() and value is None:
            return ToolResult(text=f"not a time: {raw!r}; use HH:MM")
        window.append(value)
    chosen = _action_tool(ctx, tool.strip())
    if chosen is None:
        return ToolResult(text=f"{tool!r} cannot be a Rule's action: only your acting tools "
                               "or routine_run")
    try:
        args = json.loads(tool_args) if isinstance(tool_args, str) else dict(tool_args or {})
    except ValueError:
        return ToolResult(text="tool_args is not a JSON object")
    if not isinstance(args, dict) or set(args) - set(chosen.args):
        return ToolResult(text=f"{chosen.name} takes only: {', '.join(chosen.args)}")
    if chosen.grant == "home" and not await _targets(ctx, str(args.get("target", ""))):
        return ToolResult(text=f"nothing you may switch matches {args.get('target', '')!r}")
    rid = chat_rules.create(
        ctx.conn, owner_id=ctx.turn.member_id, name=name,
        trigger_kind="mic" if when_mic.strip() else "state",
        trigger_entity=when_entity.strip() or None,
        trigger_to=(when_mic or when_state).strip().lower() or None,
        cond_after=window[0], cond_before=window[1], cond_entity=if_entity.strip() or None,
        cond_state=if_state.strip().lower() or None, action_tool=chosen.name,
        action_args={k: str(v) for k, v in args.items()},
        household=household.strip().lower() in ("yes", "sim", "true"))
    rule = chat_rules.get(ctx.conn, rid)
    return ToolResult(text=f"created Rule {name}: {rule.describe()}",
                      receipt={"summary": f"created Rule {name}",
                               "undo": {"tool": "rule_delete", "args": {"name": name}}})


async def _rule_change(ctx: ToolContext, name: str, action) -> tuple[chat_rules.ChatRule | None,
                                                                    bool]:
    from . import members as members_mod

    rule = chat_rules.by_name(ctx.conn, ctx.turn.member_id, name)
    member = members_mod.get(ctx.conn, ctx.turn.member_id)
    if rule is None:
        return None, False
    return rule, action(rule.id, bool(member and member.is_owner))


@tool(
    description="Switch one of the asker's chat Rules on or off, without deleting it",
    args={"name": "the Rule's name", "on": "yes to switch it on, no to switch it off"},
    changes_state=True,
)
async def rule_toggle(ctx: ToolContext, name: str, on: str = "no") -> ToolResult:
    enabled = on.strip().lower() in ("yes", "sim", "on", "true")
    rule, ok = await _rule_change(ctx, name, lambda rid, owner: chat_rules.set_enabled(
        ctx.conn, rid, enabled, member_id=ctx.turn.member_id, is_owner=owner))
    if not ok:
        return ToolResult(text=f"no Rule {name!r} you may change")
    return ToolResult(text=f"Rule {rule.name} is {'on' if enabled else 'off'}",
                      receipt={"summary": f"Rule {rule.name} {'on' if enabled else 'off'}",
                               "undo": {"tool": "rule_toggle",
                                        "args": {"name": rule.name,
                                                 "on": "no" if enabled else "yes"}}})


@tool(
    description="Delete one of the asker's chat Rules (its creator, or the owner)",
    args={"name": "the Rule's name"},
    changes_state=True,
)
async def rule_delete(ctx: ToolContext, name: str) -> ToolResult:
    rule, ok = await _rule_change(ctx, name, lambda rid, owner: chat_rules.delete(
        ctx.conn, rid, member_id=ctx.turn.member_id, is_owner=owner))
    if not ok:
        return ToolResult(text=f"no Rule {name!r} you may delete")
    return ToolResult(text=f"deleted Rule {rule.name}",
                      receipt={"summary": f"deleted Rule {rule.name}"})


# ── The Satellite catalogue (D43, D56) ──────────────────────────────────────
async def _computer(ctx: ToolContext, request: dict, machine: str) -> dict | str:
    """Ask one of the asker's own computers. Returns its answer, or why not."""
    if ctx.turn.in_group:
        return "your computer is private; ask in a private chat"
    app = ctx.services.get("app")
    hub = ctx.services.get("hub") or (getattr(app.state, "hub", None) if app else None)
    if hub is None or not hub.connected(ctx.turn.member_id):
        return "your computer is not connected right now (off, asleep, or its Satellite stopped)"
    online = [m["machine"] for m in hub.machines(ctx.turn.member_id) if m["online"]]
    chosen = machine.strip() or None
    if chosen and chosen not in online:
        return f"{chosen!r} is not on; connected now: {', '.join(online) or 'none by name'}"
    if not chosen and len(online) > 1:
        return f"which computer? connected now: {', '.join(online)}"
    try:
        return await hub.ask(ctx.turn.member_id, {"kind": "act", **request}, machine=chosen)
    except TimeoutError:
        return "your computer did not answer; its Satellite may need updating"


@tool(
    slow=True,
    description="What the asker's computer can do from here: its actions (lock, volume, "
    "media, open, screenshot, suspend) and the scripts it offers",
    args={"machine": "the computer's name (see satellites_list), or empty for the only one"},
)
async def computer_actions(ctx: ToolContext, machine: str = "") -> ToolResult:
    answer = await _computer(ctx, {"op": "offer"}, machine)
    if isinstance(answer, str) or answer.get("error"):
        return ToolResult(text=answer if isinstance(answer, str) else answer["error"])
    scripts = [f"{n}{' (safe)' if e['safe'] else ''}: {e['description']}"
               for n, e in answer.get("scripts", {}).items()]
    return ToolResult(text=f"actions: {', '.join(answer.get('actions', [])) or 'none'}"
                           + (f"\nscripts: {'; '.join(scripts)}" if scripts else ""))


@tool(
    slow=True,
    description="Do something on the asker's computer: lock (the screen), volume (up, "
    "down, mute, or 0-100), media (play, pause, next, previous), open (a web address or an "
    "app like firefox), or run one of its scripts marked safe",
    args={"action": "lock, volume, media, open, or script",
          "value": "for volume/media/open/script: what to do or which one",
          "machine": "the computer's name, or empty for the only one"},
    changes_state=True,
)
async def computer_act(ctx: ToolContext, action: str, value: str = "",
                       machine: str = "") -> ToolResult:
    action = action.strip().lower()
    if action not in ("lock", "volume", "media", "open", "script"):
        return ToolResult(text="for screenshot or suspend use computer_confirmed")
    answer = await _computer(ctx, {"op": action, "value": value}, machine)
    if isinstance(answer, str):
        return ToolResult(text=answer)
    if answer.get("error"):
        # A script not marked safe comes back here: the laptop wants the button.
        return ToolResult(text=answer["error"] + ("; use computer_confirmed for it"
                                                  if action == "script" else ""))
    return ToolResult(text=answer.get("text", "done"),
                      receipt={"summary": f"computer: {action} {value}".strip()})


@tool(
    slow=True,
    description="On the asker's computer, what ALWAYS needs their button: screenshot (sent "
    "to them here), suspend, or a script not marked safe",
    args={"action": "screenshot, suspend, or script", "value": "the script's name, for script",
          "machine": "the computer's name, or empty for the only one"},
    destructive=True,
)
async def computer_confirmed(ctx: ToolContext, action: str, value: str = "",
                             machine: str = "") -> ToolResult:
    import base64

    action = action.strip().lower()
    if action not in ("screenshot", "suspend", "script"):
        return ToolResult(text="only screenshot, suspend or script here")
    # `confirmed` is true only because tools.run let a destructive Tool through,
    # which it does only after the button (D56). The laptop checks it again.
    answer = await _computer(ctx, {"op": action, "value": value, "confirmed": True}, machine)
    if isinstance(answer, str) or answer.get("error"):
        return ToolResult(text=answer if isinstance(answer, str) else answer["error"])
    if action == "screenshot":
        channel = ctx.services.get("channel") or (
            getattr(ctx.services.get("app").state, "channel", None)
            if ctx.services.get("app") else None)
        if channel is None or not hasattr(channel, "send_document"):
            return ToolResult(text="this chat cannot receive the picture")
        await channel.send_document(ctx.turn.conversation_id, answer["name"],
                                    base64.b64decode(answer["data"]))
        return ToolResult(text="sent the screenshot here", receipt={"summary": "screenshot"})
    return ToolResult(text=answer.get("text", "done"),
                      receipt={"summary": f"computer: {action} {value}".strip()})
