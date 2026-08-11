# Home Assistant

This is the layer that makes `ta` able to touch your house. It is **optional** —
without it, notes, the board and the rule engine are untouched, and the `home`
commands tell you what is missing instead of failing obscurely.

The design decision behind all of it: **Home Assistant is the only thing that
knows about device protocols.** This project speaks REST to it and knows nothing
about Zigbee, Tuya, Matter or anything else. Swapping a bulb for a different
protocol is a pairing job in Home Assistant, not a code change here
([ADR 0001](adr/0001-home-assistant-como-camada-de-device.md)).

## From zero

### 1. Have a Home Assistant

If you do not already run one, the [official installation
guide](https://www.home-assistant.io/installation/) is the place to start. Any
installation method works — this project only needs to reach its HTTP API.

Adopt your devices there first. Get to the point where you can turn a light on
from the Home Assistant web interface. Nothing here helps until that works.

### 2. Create a long-lived access token

In Home Assistant: your **profile** (bottom left) → **Security** → **Long-lived
access tokens** → *Create token*.

Copy it immediately; it is shown once.

> **This token is the most dangerous credential in this project.** It can control
> every device your Home Assistant knows about, not only the ones you use here.
> Scope it as narrowly as your setup allows, and rotate it if it ever leaves your
> machine. See [`SECURITY.md`](../SECURITY.md).

### 3. Tell the daemon

```bash
cp .env.example .env
```

Then edit `.env`:

```
HA_TOKEN=<the token you just created>
HA_URL=http://localhost:8123
```

`HA_URL` is wherever your Home Assistant answers — a container on the same
machine, another box on your LAN, whatever.

```bash
systemctl --user restart ta
ta doctor          # Home Assistant: ok
```

The variable name uses an underscore for a boring reason: a hyphen is not valid
in an environment variable name, and neither your shell nor systemd would load
it. That was a real afternoon.

### 4. Find your entities

```bash
ta entities
```

That lists what your Home Assistant actually exposes, with current state. These
`entity_id`s are the only names this project knows.

```
      on  light.bedroom_lamp
     off  switch.fan_socket_1
     off  switch.fan_child_lock
```

You can now command anything by its full id:

```bash
ta on light.bedroom_lamp
ta luz light.bedroom_lamp 40      # with brightness, 0–100
ta off
```

`ta light` is the same command as `ta luz` — the Portuguese name came first and
is kept, so nobody's muscle memory breaks.

### 5. Give them short names

Typing `light.bedroom_lamp` gets old. Add aliases to
`~/.config/ta/config.toml`:

```toml
[aliases]
bedroom = "light.bedroom_lamp"
fan = "switch.fan_socket_1"
```

Now `ta on bedroom` works. `ta doctor` creates that file for you on first run;
the full reference is in [configuration.md](configuration.md).

## Device vs Entity

Worth getting straight, because it is the distinction that makes commands
predictable.

A **Device** is a physical thing — a lamp, a plug. An **Entity** is the smallest
controllable or observable unit Home Assistant exposes. **One Device can expose
several Entities**, and a command always addresses an Entity, never a Device.

The canonical example, from the author's actual house: a smart plug exposes
`switch.fan_socket_1` (the socket) *and* `switch.fan_child_lock` (its child lock
setting). Same Device. Two Entities. Asking to "turn off the plug" without saying
which one is genuinely ambiguous — which is why [`CONTEXT.md`](../CONTEXT.md)
keeps the two words apart.

## Groups and rooms

`ta on <term>` resolves in four steps and stops at the first match:

| Step | Example | What it hits |
|---|---|---|
| 1. A literal `entity_id` | `ta on light.bedroom_lamp` | exactly that Entity |
| 2. A group | `ta on luz` · `tudo` · `tomada` | every Entity in the domain |
| 3. An alias | `ta on bedroom` | what you mapped in `config.toml` |
| 4. A room, by substring | `ta on kitchen` | anything whose id or friendly name contains it |

Step 4 **prefers lights**: "turn on the kitchen" means the lamp, not a plug that
happens to be in there. Plugs are only considered if no light matches.

Rooms match by substring rather than by a list because Home Assistant does not
expose areas over the REST API — and because it picks up future lights in the
same room without anyone updating a list. Accents and case are ignored.

Add your own groups in `config.toml`:

```toml
[groups]
office = ["light.", "switch."]
```

## The two traps

Both of these were paid for in real debugging time.

### Groups skip configuration entities

`ta on tudo` used to turn on the child lock of the plug — which is a *setting*,
not an appliance anyone wants to "turn on".

The REST API does not expose `entity_category`, so there is no way to ask "is
this configuration?" directly. The available signal is the presence of
`device_class`: a real socket has `outlet`, the child lock has none. So groups
and rooms require a `device_class` on `switch` entities.

**Naming an Entity explicitly always does what you asked.** Groups are
conservative; explicit is exact.

If one of your devices is being skipped and should not be, give it a
`device_class` in Home Assistant, or name it directly.

### The state you read is the state from before

Commands to cloud-connected devices round-trip through the vendor's servers and
take 300–800 ms. Reading the state immediately after sending returns the **old**
value.

So `ta on` and `ta off` wait for the state to actually change, with a ceiling,
and report `not confirmed` if it does not flip in time.

**Failing to confirm is not failing to command.** The light very likely did turn
on; we just could not prove it before giving up. This matters because the
previous behaviour — reporting `off` right after turning something on — read as
"the tool is broken".

## Sensors

Sensor entities are not commandable, but two commands surface the useful ones.
The selection is **curated on purpose**: a raw dump is mostly sun-position and
backup entities, which is not what anyone means by "how is the house?".

```bash
ta router      # external IP, download, upload
ta temp        # temperature, conditions, humidity, wind — and plug consumption
```

The temperature also appears at the top of `ta today`, and it degrades silently:
if Home Assistant is down, the digest still works, just without that line.

A plug that measures current lets you answer something a switch cannot: whether
the fan is actually *drawing power*, as opposed to merely being "on".

## Media and voice

If you have Echo devices exposed through `alexa_media_player`, they are ordinary
`media_player` entities and need **zero Alexa-specific code**
([ADR 0009](adr/0009-alexa-entra-como-media-player-do-home-assistant.md)):

```bash
ta media play
ta media pause
ta media <entity> vol 30
```

Set `TA_ECHOS` to the entity ids you want voice announcements on. Note that
desktop notifications are **never replaced** by voice, only complemented — a
reminder that only speaks is a reminder you miss when the room is noisy.

`alexa_media_player` authenticates with a session cookie and is the least
reliable integration in this whole stack. Treat it as a nice-to-have.

## Using it from a rule

Inside a rule, `ctx.home` is the whole surface:

```python
await ctx.home.switch_on("light.bedroom_lamp", 100)   # brightness optional
await ctx.home.turn_off("switch.fan_socket_1")
state = await ctx.home.state("light.bedroom_lamp")
```

See [automations.md](automations.md) for the rest of the context object and six
worked recipes.

## When it does not work

See [troubleshooting.md](troubleshooting.md#the-house) — it covers the stale
state, the child lock, the 200-and-nothing-happens case, and what to check when
`ta doctor` says Home Assistant is unavailable.
