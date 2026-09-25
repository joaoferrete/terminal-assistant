# Installing

Installation here is a **tree, not a line**. The core needs nothing; each
integration is a separate decision with its own requirements. So this page is
written in layers: take Layer 0, then only the ones you actually want.

Every layer ends with *how to know it worked*, and the answer is almost always
`ta doctor`.

> Every environment variable mentioned here is documented in full in
> [configuration.md](configuration.md). When something breaks,
> [troubleshooting.md](troubleshooting.md) is organised by symptom.

---

## Layer 0 — The core

**You need this one.** Notes, the board, the trigger engine, the scheduler, the
export. Requires Python 3.12+ and somewhere to write a file. Nothing else.

```bash
git clone https://github.com/joaoferrete/terminal-assistant
cd terminal-assistant
make install
```

`make install` creates a virtualenv and installs the package in editable mode. It
uses **the system Python on purpose** (`/usr/bin/python3`), with
`--system-site-packages`. If you never plan to use the calendar, that does not
matter and any Python 3.12+ works. If you might, it matters a lot — see
[Layer 3](#layer-3--calendar).

Not using `make`? The equivalent is:

```bash
/usr/bin/python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e ".[dev]"
```

### Did it work?

```bash
ta doctor
```

You want `ok` on the **Notes** line. Everything else can say `—`; that is the
normal state of a fresh clone, and `ta doctor` exits `0` anyway because only the
core is essential.

`ta doctor` also creates `~/.config/ta/` on its first run — your config file and
your rules directory. It copies, never moves, and never overwrites.

Then actually use it:

```bash
ta note "my first note @tomorrow !high"
ta list
```

If `ta` is not found, the package installed but the venv is not on your `PATH`.
See [Layer 1](#layer-1--the-service-and-the-path) — this is the single most
common stumble, and it has nothing to do with the code.

---

## Layer 1 — The service, and the `PATH`

**Take this if** you want reminders to fire, automations to run, or the board to
be there when you open it. Without a running daemon, `ta note` has nothing to
talk to.

### The daemon

```bash
make install-service
```

That writes a systemd **user** unit (not system — it needs your session bus and
your home directory), enables it, and starts it. Check:

```bash
systemctl --user status ta
journalctl --user -u ta -f          # the only place to look, on purpose
```

### The `PATH`

The binary lives in the venv. Add it to your shell so `ta` works from anywhere:

```bash
echo 'export PATH="$HOME/terminal-assistant/.venv/bin:$PATH"' >> ~/.zshrc
# or ~/.bashrc, and adjust the path to wherever you cloned it
```

Skipping this step produces a very confusing failure: everything is installed and
working, and nothing responds. It happened to the author.

### Did it work?

```bash
systemctl --user is-active ta       # active
ta board                            # opens your browser
```

---

## Layer 2 — Home Assistant

**Take this if** you want to control lights, plugs and sensors, or write rules
that act on your house.

This one has enough detail to deserve its own page:
**[home-assistant.md](home-assistant.md)** — getting a token, finding your
`entity_id`s, setting up aliases and groups, and the two traps that cost the
author real time.

The short version:

```bash
cp .env.example .env
# put HA_TOKEN and HA_URL in it
systemctl --user restart ta
```

### Did it work?

```bash
ta doctor          # Home Assistant: ok
ta entities        # your real inventory, from your HA
```

---

## Layer 3 — Calendar

**Take this if** you want `ta today` to show your actual day, or rules that know
which meeting you are in.

This reads your local Evolution Data Server through PyGObject. Nothing is fetched
over the network by this project — GNOME already syncs it, and this just reads
what is on disk ([ADR 0004](adr/0004-calendar-through-gnome-online-accounts.md)).

```bash
sudo apt install gir1.2-ecal-2.0 gir1.2-edataserver-1.2
```

Then connect your accounts in **Settings → Online Accounts**. GNOME handles
OAuth and token renewal; this project never sees a credential.

**The venv must have been created with `--system-site-packages`.** PyGObject is
installed by `apt`, outside your venv, and that flag is what lets the venv see
it. If you created the venv another way, delete it and run `make install` again.

Optionally, create a calendar named `Terminal Assistant` in your provider. It is
where detected events get written — a separate, disposable layer, so a wrong
guess is never mixed into your real calendar
([ADR 0007](adr/0007-propose-and-confirm-before-writing-to-the-calendar.md)).

### Did it work?

```bash
make check-gi      # ECal OK, EDataServer OK
ta doctor          # Calendar: ok
ta today
```

The **first** `ta today` after a restart takes a few seconds and tells you it is
warming up. That is expected: each cloud calendar pays a fixed connection
timeout, and the result is cached. Subsequent calls take milliseconds.

If `ta doctor` says something about D-Bus, you are in a session without a user
bus — SSH without a graphical session, or a container. The calendar cannot work
there, and nothing else is affected.

---

## Layer 4 — AI

**Take this if** you want the second pass over captures, `ta organize`,
`ta today` in prose, or event detection.

It is **optional in the strong sense**: nothing you rely on stops working without
it, and nothing calls a model unless you ask. See [ai.md](ai.md) for what each
feature costs in model calls.

```bash
# either one is enough; with both, DeepSeek answers and Gemini is the fallback
echo "DEEPSEEK_API_KEY=your-key" >> .env    # https://platform.deepseek.com/api_keys
echo "GEMINI_API_KEY=your-key" >> .env      # https://aistudio.google.com/apikey
systemctl --user restart ta
```

To keep the key but stop the automatic second pass over every capture:
`TA_AUTO_REVIEW=0`.

### Did it work?

```bash
ta doctor          # AI: ok
ta init            # the priorities interview
```

---

## Layer 5 — The desktop extras

Global keyboard shortcuts, the quick-capture popup, and the ringlight.

See **[shortcuts.md](shortcuts.md)** for the keybindings, including a GNOME trap
that will silently eat your existing shortcuts if you are not careful.

The ringlight is the [Lighter][lighter] GNOME extension, by the same author.
Install it from its own repository; this project drives it through `gsettings`
and does nothing if it is absent.

---

## Optional: the board on your phone

By default the daemon listens on `127.0.0.1` only. Opening it to your network
takes **two** deliberate steps, because it exposes every note you have written
and the token that controls your house
([ADR 0012](adr/0012-loopback-by-default-and-a-token-to-leave-it.md)):

```bash
echo "TA_HOST=0.0.0.0" >> .env
echo "TA_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" >> .env
systemctl --user restart ta
```

Without `TA_TOKEN` the daemon **refuses to start** on a non-loopback address, and
tells you this. Then open, once:

```
http://<your-machine-ip>:7777/board?token=<the-token>
```

The token is stored in the browser and stripped from the address bar, so it does
not end up in your history. Read [`SECURITY.md`](../SECURITY.md) before doing
this on a network you do not control.

---

## Alternative: on a home server

**Take this if** you have an always-on box at home — an old PC, a mini PC — and
you want `ta` to keep running with your laptop closed. It is optional. The
single-machine install above is still the default and still complete.

On a server you get the notes, the board, the rules, the scheduler, Home Assistant
and the AI. You do not get the desktop-shaped parts: the calendar (it reads your
GNOME session), meeting detection (your laptop's microphone), the ringlight and
desktop notifications. The plan to bring those back from the laptop is the
*Satellite* in [PLAN.md](PLAN.md); until it lands, **the CLI on another machine
cannot reach the server**, so you use the board from the browser.

This was written from a real migration onto DietPi (Debian 12). Other Debian-like
systems behave the same.

### Python 3.12 without upgrading the system

Debian 12 ships Python 3.11, and the project needs 3.12+. A server does not need
the system Python — that rule exists for the calendar's PyGObject
([ADR 0005](adr/0005-the-system-python-because-of-pygobject.md)), and the calendar
does not run there. So take a standalone Python from `uv`, which installs into
your home and touches no system package:

```bash
sudo apt install git make curl
curl -LsSf https://astral.sh/uv/install.sh | sh
~/.local/bin/uv python install 3.12

git clone https://github.com/joaoferrete/terminal-assistant
cd terminal-assistant
make install SYS_PYTHON="$(~/.local/bin/uv python find 3.12)"
```

On a system whose own Python is already 3.12+, plain `make install` is fine.

### A system service, not a user one

```bash
make install-server-service
journalctl -u ta -f
```

The desktop install uses a systemd **user** unit because the daemon needs your
session. A server has no session to need, and DietPi ships without
`systemd-logind` and without a system D-Bus, so `systemctl --user` and
`loginctl enable-linger` fail with *Failed to connect to bus*. This target
generates a system unit from the same file, running as whoever runs `make`.

### Open it to the network

A server is reached from other machines, so it needs both `TA_HOST` and
`TA_TOKEN` in `.env` — see [the board on your phone](#optional-the-board-on-your-phone).
Then restart with `sudo systemctl restart ta`.

### Home Assistant next to it

If Home Assistant runs on the same box (a container with `--network host` is the
simplest), point `HA_URL` at it locally:

```bash
HA_URL=http://localhost:8123
```

Moving an existing Home Assistant container is a matter of stopping the old one,
copying its whole config directory (`.storage/` belongs to root, so use `sudo`),
and starting the **same image version** on the new box. The long-lived token
travels inside the config, so `HA_TOKEN` keeps working. Do not keep both running:
two instances will fight over the same devices. Update the version afterwards,
separately.

### Moving an existing installation

Stop the old daemon first, then copy the database with SQLite's backup API rather
than as a raw file. A raw copy of a database in WAL mode can leave the last writes
behind:

```bash
systemctl --user disable --now ta                    # on the old machine
python3 -c "import sqlite3; s=sqlite3.connect('$HOME/.local/share/ta/ta.db'); \
d=sqlite3.connect('/tmp/ta.db'); s.backup(d); d.close()"
```

Then move `/tmp/ta.db` to `~/.local/share/ta/ta.db` on the server, and
`~/.config/ta/` and `.env` alongside. On a DietPi box, **`scp` fails** with
*Connection closed*: its SSH server is Dropbear, which has no `sftp-server`, and
modern `scp` speaks SFTP. Copy through `ssh` instead:

```bash
ssh you@server 'mkdir -p ~/.local/share/ta && cat > ~/.local/share/ta/ta.db' < /tmp/ta.db
tar -C ~/.config -cz ta | ssh you@server 'tar -C ~/.config -xz'
```

### Did it work?

```bash
ta doctor                    # on the server: notes and home ok; calendar,
                             # microphone and ringlight unavailable, with reasons
```

And open `http://<server-ip>:7777/board?token=<the-token>` from any machine on
your network.

---

## Choosing a language

The tool follows your system locale, so there is usually nothing to do. To
override:

```bash
ta lang            # shows the language and where it came from
ta lang en
systemctl --user restart ta
```

The language is a property of the **installation**, not of each command:
`TA_LANG=en ta note …` will not change how the note is parsed, because the daemon
does the parsing. This is deliberate
([ADR 0013](adr/0013-one-language-at-a-time.md)) — if it varied per call, two notes
captured on the same day would read `@03/04` as different dates.

## Updating

```bash
git pull
make install
systemctl --user restart ta
```

Your configuration and rules live in `~/.config/ta/`, and your notes in
`~/.local/share/ta/`, so nothing you own is inside the repository and `git pull`
never conflicts with your setup. Database migrations run automatically on start,
and a copy of the database is made before any migration that changes data.

## Uninstalling

```bash
systemctl --user disable --now ta
rm ~/.config/systemd/user/ta.service
rm -rf ~/.config/ta ~/.local/share/ta     # your config and your notes
rm -rf <the clone>
```

Export first if you want to keep anything: `ta export > notes.md`. That escape
hatch is the reason the storage is a plain SQLite file.

[lighter]: https://github.com/joaoferrete/Lighter
