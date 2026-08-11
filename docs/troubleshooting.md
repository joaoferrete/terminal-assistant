# When something does not work

Organised by **symptom**, because that is what you have.

Almost every entry here is a bug that actually happened, usually costing more
time than it should have. They are written down so they cost you less.

## Start here

```bash
ta doctor
```

It tells you which integrations are live on this machine, why the others are not,
and the exact command to fix each one. It does **not** need the daemon running —
that is deliberate, since the people who most need a diagnosis are the ones whose
daemon will not start.

If you are about to report a bug, paste its output. It answers the first round of
questions on its own.

---

## Nothing responds at all

### `ta: command not found`

The package installed fine; the virtualenv is just not on your `PATH`.

```bash
export PATH="$HOME/terminal-assistant/.venv/bin:$PATH"
```

Put it in your `~/.zshrc` or `~/.bashrc` to make it stick.

This is worth checking **first**, always. It once looked like a broken Home
Assistant integration for an entire debugging session, and the cause was this.

### `The daemon is not running`

```bash
systemctl --user status ta
systemctl --user start ta
journalctl --user -u ta -n 50
```

If it refuses to start and mentions `TA_HOST`, it is doing that on purpose:
you asked it to listen on a network address without setting `TA_TOKEN`, and it
will not expose your notes and your house without a credential. The message
contains the fix.

---

## The board

### I changed the code and the board did not change

Restart the daemon:

```bash
systemctl --user restart ta
```

The board HTML is served with `no-store` and updates immediately; the Python
routes only change when the process restarts. So the page you are looking at can
be **newer than the daemon serving it**.

The board detects this and shows a red banner with that exact command, and
`ta today` says the same in one line. That guard exists because the failure
without it is the worst kind: the board drew the *wrong order with the right
look*, and it read as "the change didn't work".

### Everything is in the wrong language

The language belongs to the **daemon**, not to your shell. `TA_LANG=en ta note …`
does nothing, because the CLI is a thin client and the daemon does the parsing.

```bash
ta lang            # shows the language AND where it came from
ta lang en
systemctl --user restart ta
```

### The board is empty, but I have notes

Check the filter selectors in the header. An active filter is outlined in the
accent colour — that outline exists precisely because "there are no notes" and
"the filter is hiding everything" looked identical, and the second one is the
alarming one.

### From my phone: `credential does not match`

You reached the board over the network, so it needs the token:

```
http://<machine-ip>:7777/board?token=<TA_TOKEN from your .env>
```

Once is enough — it is stored in the browser and removed from the address bar.
If you rotated `TA_TOKEN`, open the `?token=` URL again.

---

## The house

### `ta on` reports the old state, or "not confirmed"

Expected, and handled. A command to a cloud-connected device round-trips through
the vendor's servers and takes 300–800 ms, so reading the state immediately after
sending returns the **previous** value.

`ta on` and `ta off` wait for the state to actually change, with a ceiling, and
say `not confirmed` if it does not. **Failing to confirm is not the same as
failing to command** — the light usually did turn on. Before this, `ta on`
reported `off` and looked broken.

### `ta on tudo` turned on something weird

Groups and rooms deliberately skip **configuration** entities. A smart plug often
exposes two entities: the socket, and its child lock. Turning on "everything"
should not engage a child lock.

The Home Assistant REST API does not expose `entity_category`, so the signal used
is the presence of `device_class`. A real socket has one; a child lock does not.
If one of your devices is skipped and should not be, name it explicitly —
`ta on switch.the_exact_thing` always does what you asked.

### A light responds with 200 and does nothing

If you are calling it by `entity_id`, check the domain. Calling the `light`
service on a `switch` entity gets a cheerful `200` from Home Assistant and no
action at all. The code derives the domain from the `entity_id` now, but a
hand-written rule can still get this wrong.

### The ringlight does not react

```bash
gsettings get org.gnome.shell.extensions.lighter enabled
```

Two likely causes, in order:

1. **The extension is not enabled.** Check GNOME Extensions.
2. **You changed the extension and are on Wayland.** GNOME Shell does not reload
   extensions without a full logout/login. The installed copy is updated; the
   running session is not.

A schema-not-found error means `gsettings` cannot see the extension's schema — it
lives inside the extension directory, not on the default search path. The code
discovers `--schemadir` at runtime for this reason.

---

## The calendar

### `import gi` fails

The virtualenv was created with the wrong interpreter, or without
`--system-site-packages`. PyGObject is installed by `apt` outside your venv, and
that flag is what lets the venv see it.

```bash
rm -rf .venv && make install && make check-gi
```

`make check-gi` prints the exact `apt` command if a typelib is missing, rather
than letting you discover it as an `ImportError` in the middle of an automation.

### `Cannot autolaunch D-Bus without X11 $DISPLAY`

You are in a session with no user bus — SSH without a graphical login, a
container, or CI. The calendar cannot be read there. Nothing else is affected,
and `ta doctor` says so instead of crashing. (It used to crash, which made the
diagnostic tool useless in exactly the environments that needed it.)

### `ta today` lists no events

Your accounts are connected in **Settings → Online Accounts**, but Evolution has
not synced yet — or was never asked to. Open the Evolution calendar once and let
it sync.

### The first `ta today` after a restart is slow

Expected. Each cloud calendar source pays a fixed connection timeout on first
contact. Warming happens in the background on boot and the result is cached, so
subsequent calls take milliseconds. `ta today` waits for the warm-up and tells
you it is doing so, rather than failing on a timeout.

### A recurring event shows the wrong date

If you are reading the calendar from your own code, make sure you expand
recurrences. The API that returns the *master* component gives you the start of
the whole series, so a daily event appears once, on the wrong day.

### Re-tagging a note created a second calendar event

Fixed, but worth knowing the shape: a note that already has a calendar link never
creates another. Duplicating an appointment is worse than not updating its title,
so the guard errs that way.

---

## Capture

### `!!22:00` breaks in zsh

Use `@@22:00`.

`!!` triggers history expansion when zsh reads the line, and the command dies
with `zsh: no such word in event` before it ever reaches the parser. `!!` still
works in the board, which has no shell — but the documented syntax is `@@`, and
that was measured in a real interactive shell rather than assumed.

### A date in my note was ignored

The parser recognises one language at a time — the one from `ta lang`. Words like
`@today` work in both, but `@friday` only works in English mode and `@sexta` only
in Portuguese mode.

Numeric dates follow the active language: `@03/04` is **3 April** in Portuguese
and **4 March** in English. This is the whole reason only one language is active
at a time; accepting both would make that token silently wrong for half the
world ([ADR 0013](adr/0013-um-idioma-por-vez.md)).

Unrecognised marks are not errors: `@home` is not a date, so it stays in your
text rather than failing the capture.

### "today I learned X" became a task

It should not — there is a curated list of retrospective verbs guarding exactly
this. If one slipped through, the second AI pass fixes intent when it is enabled,
because a regex gets the *form* right and the *intention* wrong.

### A note captured offline never got reviewed

It is queued, not lost. Each capture with network drains a few of the backlog,
and `ta revise` pushes everything open back into the queue.

---

## The AI

### Everything AI says it is not configured

```bash
ta doctor          # AI (Gemini): —
```

Set `GEMINI_API_KEY` in `.env` and restart. Or do not — capture, the board, the
rule engine, reminders and export never touch a model
([ADR 0003](adr/0003-llm-fora-do-caminho-critico.md)).

To keep the key but stop the automatic pass over each capture: `TA_AUTO_REVIEW=0`.

### `ta organize` did not visibly change the board

The general view uses the position you dragged things to; your hand always beats
the model. Look at the **list** view to see the ordering.

Also: `organize` refines order *within* a due-date band, never across bands. The
clock owns the deadline ([ADR 0010](adr/0010-o-relogio-ordena-o-quadro.md)).

---

## Automations

### A rule fired at the wrong time, or not at all

```bash
ta rules            # what the daemon actually loaded
ta rules check      # validate without booting anything
journalctl --user -u ta -f
```

There is exactly one place to look, and that is a design decision
([ADR 0002](adr/0002-motor-de-automacao-proprio-em-python.md)).

A rule file with a syntax error does not take the daemon down — the others still
load, and the failure is logged with the filename.

### The mic trigger never fires

```bash
which pw-dump      # needed for microphone detection
```

There is a two-second debounce, so a system beep does not count as a meeting.

### My rules stopped loading after an update

They live in `~/.config/ta/rules/` now. If you used a very early version with
rules inside the repository, they still load from there and `ta doctor` copies
them to the new location without deleting the originals.

---

## Still stuck?

Open an issue with the output of `ta doctor` and the relevant lines from
`journalctl --user -u ta`. If it is a security problem, use GitHub's private
vulnerability reporting instead — see [`SECURITY.md`](../SECURITY.md).
