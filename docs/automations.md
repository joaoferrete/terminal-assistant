# Automations

This is the guide to the part that motivated the whole project: **combining the
state of your computer with the state of your house.** No off-the-shelf engine
does it, because Home Assistant does not know you just joined a call and GNOME
does not know how to turn on a lamp.

## The mental model

An automation is a **Rule**, and a Rule has three parts:

```
   trigger          condition           actions
   ───────          ─────────           ───────
   I joined     +   (none)         →    light to 100%
   a call                               and the ringlight on

   I left       +   and it is      →    light back to a
   the call         still early         living level
```

The trigger is the **when**, the condition is the **only if**, and the actions
are the **then**. Only the trigger is required.

Rules are Python files in `~/.config/ta/rules/`. Not a template language — real
Python, with `if`, with variables, with whatever you want. The reasoning is in
[ADR 0002](adr/0002-motor-de-automacao-proprio-em-python.md): a YAML dialect
would mean writing an interpreter before the first rule could run.

## Your first rule, in three minutes

Create any file in `~/.config/ta/rules/`. The name does not matter, only the
extension:

```python
# ~/.config/ta/rules/goodnight.py
from ta.engine import at_time, rule


@rule(on=at_time("23:00"))
async def goodnight(ctx):
    """At 11pm, dim the light instead of killing it. Killing it would be hostile."""
    await ctx.home.switch_on("light.bedroom_lamp", 15)
    await ctx.notify.send("Good night", "light dimmed")
```

Validate without booting anything:

```bash
ta rules check
```

```
  ok   meeting  (mic=True)
  ok   meeting_end  (mic=False)
  ok   goodnight  (time=23:00)

3 rule(s), 0 with errors.
```

And put it live:

```bash
systemctl --user restart ta
ta rules              # confirms the daemon loaded it
```

⚠️ **The daemon does not reload rules on its own.** Editing the file is not
enough. That is deliberate — hot-reloading code in a resident process is a source
of hard bugs, and a restart costs under a second.

`examples/rules/` in the repository has a complete working rule to copy from. It
is versioned as documentation and **never loaded**, because it targets one
specific house's inventory.

## Available triggers

| Trigger | Fires when | Where the signal comes from |
|---|---|---|
| `mic_active()` | the microphone starts being used | PipeWire, `pw-dump`, once a second |
| `mic_inactive()` | the microphone stops being used | same |
| `at_time("HH:MM")` | the clock hits that minute | internal scheduler |
| `reminder_due()` | one of your Reminders comes due | your own notes |
| `entity_state("entity_id")` | that entity changes state | Home Assistant polling, every 5s |

Details that matter in practice:

- **`mic_*` has a two-second debounce.** A system beep does not start a meeting.
  And `gnome-shell` and `gsd-media-keys` are ignored on purpose: they open the
  microphone without anybody being in a call.
- **`at_time` is minute-resolution**, and **never fires on the first tick** after
  the daemon starts. Without that guard, starting at 23:00:30 would fire the
  23:00 rule late and wrong.
- **`entity_state` only sees the second reading onwards.** The first establishes
  the baseline, so starting the daemon does not produce a flood of "it changed".

## Conditions

Conditions go in `when=` and receive the same `ctx`:

| Condition | True when |
|---|---|
| `after("16:00")` | it is 16:00 or later |
| `before("09:00")` | it is before 09:00 |
| `all_of(a, b, ...)` | all of them are true |
| `any_of(a, b, ...)` | any of them is true |

`when=` accepts **any** function taking `ctx` and returning a bool, so you are
not limited to those four:

```python
from ta.engine import all_of, at_time, before, rule


def weekday(ctx):
    return ctx.now.weekday() < 5


@rule(on=at_time("08:30"), when=all_of(weekday, before("12:00")))
async def workday_morning(ctx): ...
```

## What a Rule receives: `ctx`

| Field | What it is |
|---|---|
| `ctx.now` | `datetime` of the moment it fired |
| `ctx.trigger` | the trigger that fired (`.kind` and `.value`) |
| `ctx.home` | the house, through Home Assistant |
| `ctx.lighter` | the ringlight |
| `ctx.notify` | desktop notification |
| `ctx.calendar` | your calendars |
| `ctx.note` | the note — **only** when the trigger is `reminder_due()` |
| `ctx.extra` | trigger-specific data (below) |

`ctx.extra` changes with the trigger:

| Trigger | `extra` contains |
|---|---|
| `mic_*` | `{"apps": ["chrome", ...]}` — who is holding the microphone |
| `entity_state` | `{"from": "off", "to": "on"}` |

### `ctx.home` — the house

```python
await ctx.home.switch_on("light.bedroom_lamp", 100)   # brightness only on light.
await ctx.home.switch_on("switch.fan_socket_1")       # no brightness
await ctx.home.turn_off("light.bedroom_lamp")
state = await ctx.home.state("light.bedroom_lamp")    # the HA dict
everything = await ctx.home.entities("light.", "switch.")
house = await ctx.home.sensors()                      # weather, router, plug
```

⚠️ `switch_on` derives the domain from the `entity_id`. Do not call
`light/turn_on` on a `switch` — Home Assistant answers **200 and does nothing**,
which is the worst kind of failure. It was a real bug here.

### `ctx.lighter` — the ringlight

The ringlight is [Lighter][lighter], a GNOME extension by the same author. The
daemon drives it through `gsettings`.

```python
await ctx.lighter.apply_profile("Meet")   # by name, never by uid
await ctx.lighter.enable(False)
await ctx.lighter.toggle()
profiles = await ctx.lighter.profiles()
```

**Calibration lives in the extension**, not here. A Rule says *which* profile;
Lighter knows *what it looks like*. Duplicating the nine edge values into a Rule
would create a second source of truth.

Applying a profile from outside did not work until a [PR to the
extension](https://github.com/joaoferrete/Lighter/pull/3): the `active-profile`
key existed, but only the preferences process listened to it, and only to update
its own interface. Writing the key from outside changed nothing on screen.

### `ctx.calendar` — the calendar

```python
event = await ctx.calendar.agora()    # the event happening now, or None
events = await ctx.calendar.hoje()    # today's
```

Each event is a dict with `summary`, `start`, `end`, `all_day`, `calendar`.

The calendar is **context, not a trigger**
([ADR 0008](adr/0008-microfone-como-gatilho-de-reuniao.md)): the microphone says
you are in a meeting, the calendar says *which*. An event on the calendar does
not prove you joined it.

### `ctx.notify` — the screen

```python
await ctx.notify.send("Title", "body", urgency="critical")
```

`urgency` takes `low`, `normal`, `critical`. It **never raises**: a notification
that fails should not take down the Rule that asked for it.

## Recipes

Copy, adjust the entity ids to your own, save in `~/.config/ta/rules/`.

### 1. Meeting (the one that ships as an example)

The original case, in `examples/rules/reuniao.py`. Note the two things that are
only possible because the app is the brain: the time condition, and consulting
the calendar to treat a 1:1 differently.

The clock does not decide **whether** the rule runs — it decides **what it does**,
because what `16:00` really means is "it is dark now"
([ADR 0011](adr/0011-a-hora-decide-o-que-a-regra-faz.md)):

| Effect | Before 16:00 | After |
|---|---|---|
| Light to 100% on entry | yes | yes |
| Ringlight | no | yes |
| Light back to living level on exit | yes | no |

```python
from ta.engine import after, mic_active, mic_inactive, rule

LIGHT = "light.bedroom_lamp"
DARK_AFTER = "16:00"


def _is_dark(ctx) -> bool:
    """One place decides what "16:00" means, and both uses read from here."""
    return after(DARK_AFTER)(ctx)


@rule(on=mic_active())
async def meeting(ctx):
    event = await ctx.calendar.agora() if ctx.calendar else None
    title = (event or {}).get("summary", "")

    if title and "1:1" in title:        # a 1:1 needs no production
        return

    await ctx.home.switch_on(LIGHT, 100)   # the light helps at any hour
    if _is_dark(ctx):                      # daylight already does the job
        await ctx.lighter.apply_profile("Meet")


@rule(on=mic_inactive())
async def meeting_end(ctx):
    # The 1:1 is checked here too: if nothing was turned on, nothing should be
    # undone. `enable(False)` is unconditional on purpose — a meeting that starts
    # at 15:50 and ends at 16:10 never turned the ring on, but must be able to
    # turn it off.
    await ctx.lighter.enable(False)
    if not _is_dark(ctx):
        await ctx.home.switch_on(LIGHT, 40)
```

### 2. A reminder that flashes the light

Closes the loop between the two halves: one of your notes becomes an action in
your house.

```python
import asyncio

from ta.engine import reminder_due, rule


@rule(on=reminder_due())
async def flashing_reminder(ctx):
    """A reminder tagged #urgent flashes the light, on top of the screen alert."""
    if "urgent" not in (ctx.note or {}).get("tags", []):
        return

    for _ in range(3):
        await ctx.home.switch_on("light.bedroom_lamp", 100)
        await asyncio.sleep(0.4)
        await ctx.home.switch_on("light.bedroom_lamp", 20)
        await asyncio.sleep(0.4)
```

No need to notify: the daemon already did that **before** calling your Rule. The
desktop alert is the guaranteed path and never depends on a rule existing.

### 3. A spoken morning digest

```python
from ta.engine import at_time, rule


@rule(on=at_time("08:30"))
async def good_morning(ctx):
    events = await ctx.calendar.hoje()
    if not events:
        return

    first = events[0]
    when = first["start"][11:16]
    await ctx.notify.send(
        "Good morning",
        f"{len(events)} events. First: {first['summary']} at {when}",
    )
```

### 4. React to something changing in the house

```python
from ta.engine import entity_state, rule


@rule(on=entity_state("switch.fan_socket_1"))
async def fan_changed(ctx):
    if ctx.extra.get("to") != "on":
        return
    house = await ctx.home.sensors()
    temp = house["weather"]["temperature"]
    if temp is not None and temp < 20:
        await ctx.notify.send("Fan came on", f"but it is {temp}°C outside")
```

This fires on **any** change to that entity, including ones made from the vendor
app or a voice assistant — polling does not know who sent the command.

### 5. The meeting is about to start

```python
from ta.engine import at_time, rule


@rule(on=at_time("08:55"))
async def standup_warning(ctx):
    for e in await ctx.calendar.hoje():
        if "Daily" in e["summary"]:
            await ctx.notify.send("Daily in 5 min", e["summary"], urgency="critical")
            await ctx.home.switch_on("light.bedroom_lamp", 100)
            return
```

### 6. Your own condition, mixing the mic and the calendar

```python
from ta.engine import all_of, mic_active, rule


def in_a_browser_call(ctx):
    """Only counts if the microphone was opened by a browser."""
    return ctx.extra.get("apps") and any(
        "chrome" in app or "meet" in app for app in ctx.extra["apps"]
    )


@rule(on=mic_active(), when=all_of(in_a_browser_call))
async def focus(ctx):
    await ctx.home.switch_on("switch.fan_socket_1")   # less heat, less background noise
```

## When it does not work

**The rule does not show in `ta rules`.** Run `ta rules check` — it validates
without the daemon and shows the traceback. Files starting with `_` are ignored
on purpose.

**It shows up but does nothing.** An exception inside a Rule is logged and
**swallowed**, so a bad rule does not take the others down. The log has it:

```bash
journalctl --user -u ta -f
```

**A file with a syntax error does not take the daemon down.** It removes that
file and the others keep loading. That is the guarantee from
[ADR 0002](adr/0002-motor-de-automacao-proprio-em-python.md), and it is why
`load_rules` catches `BaseException` — even a stray `exit()` in a rule file is
contained.

**I changed the rule and nothing changed.** You need
`systemctl --user restart ta`.

**The ringlight rule does not apply the profile.** Two possible causes, in this
order:

1. On Wayland, GNOME Shell does not reload extensions, so **changing the code**
   of [Lighter][lighter] only takes effect after logout/login. Changing its
   *configuration* takes effect immediately.
2. The extension's own auto-switch is on and overwrites what the Rule did at the
   next window focus change. The daemon turns that key off while it is up and
   hands it back on exit — if it was killed, the key stays off.

## Today's limits

Worth knowing what does **not** exist, so you do not go looking:

- **No window or focused-app trigger.** On Wayland no external process can read a
  window title. The Lighter extension *can* — it runs inside the Shell — and that
  is the path if it ever becomes necessary. Today the meeting signal is the
  microphone.
- **No camera trigger.** `/dev/video*` would work and was not needed: in a work
  call the microphone opens and the camera often does not.
- **No day-of-week recurrence in the `at_time` signature.** Do it in `when=`,
  like recipe 3.
- **No pushed state from Home Assistant.** It is 5-second polling
  ([ADR 0001](adr/0001-home-assistant-como-camada-de-device.md)). If sub-second
  latency is ever needed, a WebSocket goes into `actuators/home.py` without
  changing any caller.

[lighter]: https://github.com/joaoferrete/Lighter
