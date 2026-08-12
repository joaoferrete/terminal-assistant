"""CLI: a thin HTTP client of the daemon.

Deliberately dumb. All the logic lives in the daemon, so that there is only one
place where things happen. The only real work here is failing readably when the
daemon is not up — never hanging, never dumping a traceback.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import webbrowser

import httpx

from .config import Config, load_env_file
from .i18n import LANGS, lang, lang_source, reset_cache, t

# The daemon answers fast on everything deterministic. The routes that call the
# LLM are the exception, which is why the timeout is per call rather than global:
# 5s is right for saying "the daemon is down", and wrong for a model call.
TIMEOUT = 5.0
TIMEOUT_LLM = 120.0
# The first `ta today` after a restart waits for the calendar to warm: several
# Evolution sources, and a freshly created one pays up to 10s to connect. 5s
# there made the command fail every time right after `systemctl restart`.
TIMEOUT_CAL = 60.0


class Problem(Exception):
    """An error to show the user, with no traceback."""


def _request(
    cfg: Config,
    method: str,
    path: str,
    payload: dict | None = None,
    *,
    timeout: float = TIMEOUT,
) -> httpx.Response:
    url = f"{cfg.base_url}{path}"
    try:
        r = httpx.request(method, url, json=payload, timeout=timeout)
    except httpx.ConnectError as e:
        raise Problem(f"{t('daemon.down')} ({cfg.base_url})") from e
    except httpx.TimeoutException as e:
        raise Problem(t("cli.timeout", s=f"{timeout:.0f}", url=cfg.base_url)) from e

    if r.status_code >= 400:
        try:
            detail = r.json().get("error", r.text)
        except json.JSONDecodeError:
            detail = r.text
        raise Problem(t("cli.refused", code=r.status_code, detail=detail))
    return r


# The same symbols as `ta export`, so the two cannot diverge.
STATUS_MARK = {"todo": "[ ]", "doing": "[~]", "hold": "[-]", "done": "[x]", "cancelled": "[/]"}


def _fmt_note(n: dict) -> str:
    """One line per Note, naming the roles with the glossary's vocabulary."""
    marks = []
    if n["roles"]["task"]:
        marks.append(t("note.task", due=n["due"]))
    if n["roles"]["reminder"]:
        marks.append(t("note.reminder", at=n["remind_at"]))
    if n["priority"]:
        # The VALUE goes through the catalogue too: it is canonical English in the
        # database (`high`), and printing it raw showed `prio high` to somebody
        # reading Portuguese.
        marks.append(t("note.priority", value=t(f"priority.{n['priority']}")))
    if n["tags"]:
        marks.append(" ".join(f"#{t}" for t in n["tags"]))
    # A marker per state, not binary: a cancelled note came out as `[ ]` and read
    # as open. The symbols are the same as `ta export`.
    box = STATUS_MARK.get(n["status"], "[ ]")
    suffix = f"  ({'; '.join(marks)})" if marks else ""
    return f"{box} #{n['id']} {n['text']}{suffix}"


def cmd_note(cfg: Config, args) -> int:
    text = " ".join(args.text).strip()
    if not text:
        raise Problem(t("cli.nothing_to_capture"))
    note = _request(cfg, "POST", "/notes", {"text": text}).json()
    print(_fmt_note(note))
    return 0


def cmd_list(cfg: Config, args) -> int:
    suffix = "?done=1" if args.all else ""
    notes = _request(cfg, "GET", f"/notes{suffix}").json()["notes"]
    if not notes:
        print(t("cli.no_notes"))
        return 0
    for n in notes:
        print(_fmt_note(n))
    return 0


def cmd_done(cfg: Config, args) -> int:
    note = _request(cfg, "POST", f"/notes/{args.note_id}/done").json()
    print(_fmt_note(note))
    return 0


def cmd_export(cfg: Config, args) -> int:
    print(_request(cfg, "GET", "/export").text, end="")
    return 0


def cmd_board(cfg: Config, args) -> int:
    # Confirm the daemon is up before opening the browser: a tab with a
    # connection error is worse than a message in the terminal.
    _request(cfg, "GET", "/health")
    url = f"{cfg.base_url}/board"
    print(f"abrindo {url}")
    webbrowser.open(url)
    return 0


def cmd_light(cfg: Config, args) -> int:
    payload = {"entity": args.entity}
    if args.brightness is not None:
        payload["brightness"] = args.brightness
    # A longer timeout: confirming the state waits for the vendor cloud round
    # trip, and a group multiplies that by the number of targets.
    r = _request(cfg, "POST", "/home/light", payload, timeout=60.0).json()
    for res in r["results"]:
        note_ = "" if res.get("confirmed", True) else f"  ({t('cli.sent_unconfirmed')})"
        print(f"{res['entity_id']}: {res['state']}{note_}")
    return 0


def cmd_on(cfg: Config, args) -> int:
    """Turn on any Entity. `ta luz` is the same command, kept for familiarity."""
    return cmd_light(cfg, args)


def _fmt_num(v, casas=1):
    try:
        return f"{float(v):.{casas}f}"
    except (TypeError, ValueError):
        return "?"


def cmd_router(cfg: Config, args) -> int:
    r = _request(cfg, "GET", "/sensors").json()["router"]
    print(f"  IP externo:  {r['external_ip'] or '?'}")
    print(f"  download:    {_fmt_num(r['download_kib_s'])} KiB/s")
    print(f"  upload:      {_fmt_num(r['upload_kib_s'])} KiB/s")
    return 0


def cmd_temp(cfg: Config, args) -> int:
    s = _request(cfg, "GET", "/sensors").json()
    w, o = s["weather"], s["outlet"]
    print(f"  {_fmt_num(w['temperature'])}{w['unit'] or '°C'}  {w['condition'] or ''}")
    if w["humidity"] is not None:
        print(f"  umidade {w['humidity']}%   vento {_fmt_num(w['wind_speed'])} km/h")
    # The plug measures current: it can tell whether the fan is actually drawing
    # power, which is different from the switch being on.
    if o["state"] is not None:
        pulling = (float(o["watts"] or 0) > 0.5)
        print(
            f"  ventilador: {o['state']}"
            f"{' (pulling ' + _fmt_num(o['watts']) + ' W)' if pulling else ' (sem consumo)'}"
        )
    return 0


def cmd_off(cfg: Config, args) -> int:
    r = _request(
        cfg, "POST", "/home/off", {"entity": args.entity} if args.entity else {}, timeout=30.0
    ).json()
    if not r["turned_off"]:
        print(t("cli.nothing_was_on"))
        return 0
    for res in r.get("results", []):
        note_ = "" if res.get("confirmed", True) else f"  ({t('cli.unconfirmed')})"
        print(f"{t('cli.turned_off')}: {res['entity_id']}{note_}")
    return 0


def cmd_entities(cfg: Config, args) -> int:
    for e in _request(cfg, "GET", "/home/entities").json()["entities"]:
        print(f"{e['state']:>8}  {e['entity_id']}")
    return 0


def cmd_lighter(cfg: Config, args) -> int:
    payload: dict = {"toggle": True} if args.action == "toggle" else {"on": args.action == "on"}
    if args.profile:
        payload = {"profile": args.profile}
    r = _request(cfg, "POST", "/lighter", payload).json()
    print(f"lighter: enabled={r['enabled']}")
    return 0


MEDIA_ACTIONS = ("play", "pause", "stop", "next", "previous")


def cmd_media(cfg: Config, args) -> int:
    """`ta media pause` and `ta media echo pause` are both valid.

    The two positionals were ambiguous to argparse — `ta media pause` landed in
    `entity="pause"` and Home Assistant returned an unexplained 400. Here the
    action is recognised by value, and whatever is not an action is the target.
    """
    target, action = "", None
    for termo in args.alvos:
        if termo in MEDIA_ACTIONS:
            action = termo
        else:
            target = termo

    payload: dict = {"entity": target}
    if args.volume is not None:
        payload["volume"] = args.volume
    elif args.play:
        payload["play"] = " ".join(args.play)
    elif args.announce:
        payload["announce"] = " ".join(args.announce)
    else:
        payload["action"] = action or "play"
    _request(cfg, "POST", "/media", payload)
    print("ok")
    return 0


# A fixed-width label, so the lines align without a table. Formatting only: the
# order arrives ready from the daemon, and the CLI has no rank table of its own.
def _prio_label(priority: str | None) -> str:
    """The fixed-width priority label, from the catalogue.

    It was a dict of Portuguese literals, so `ta list` printed `!ALTA` under
    `TA_LANG=en`. The width is load-bearing — see the note on the keys.
    """
    return t(f"priority.short.{priority}") if priority else " " * 5


def cmd_revise(cfg: Config, args) -> int:
    """The same as the board's "review all" button.

    It puts every open Note back in the queue and follows it until it drains. Done
    and cancelled ones stay out: reviewing the deadline of something already
    finished spends a model call for nothing.
    """
    r = _request(cfg, "POST", "/review-all", timeout=TIMEOUT_LLM).json()
    total = r["queued"]
    if not total:
        print(t("cli.nothing_to_review"))
        return 0
    print(t("cli.queued_reviewing", n=total))

    import time

    last = total
    for _ in range(240):                       # teto de ~8 min
        time.sleep(2)
        s = _request(cfg, "GET", "/review-status").json()
        if s["pending"] != last:
            print(f"  faltam {s['pending']}")
            last = s["pending"]
        if not s["pending"] and not s["running"]:
            break
    print("pronto. `ta list` para ver o resultado.")
    return 0


def cmd_rm(cfg: Config, args) -> int:
    """Delete reversibly. `ta rm --list` shows the trash."""
    if args.purge:
        return _purge(cfg, args)
    if args.list:
        notes = _request(cfg, "GET", "/notes?deleted=1").json()["notes"]
        if not notes:
            print(t("cli.trash_empty"))
            return 0
        for n in notes:
            print(f"[x] #{n['id']} {n['text']}  ({t('cli.deleted')} {n['deleted_at']})")
        print("\n`ta restore <id>` traz de volta.")
        return 0
    if args.note_id is None:
        raise Problem(t("cli.which_note"))
    n = _request(cfg, "DELETE", f"/notes/{args.note_id}").json()
    print(f"{t('cli.deleted')}: #{n['id']} {n['text']}  (`ta restore {n['id']}` {t('cli.undoes')})")
    return 0


def _purge(cfg: Config, args) -> int:
    """Delete permanently. It only reaches what is already in the trash.

    It asks first, by default. This is the only operation with no way back, and a
    confirmation is cheap compared to losing a note you thought was safe. `--yes`
    exists for scripts; interactive `ta` always asks.
    """
    if args.note_id is not None:
        r = _request(cfg, "DELETE", f"/notes/{args.note_id}/purge")
        print(t("cli.purged_one", id=args.note_id))
        return 0

    notes = _request(cfg, "GET", "/notes?deleted=1").json()["notes"]
    if not notes:
        print(t("cli.trash_empty"))
        return 0

    print(t("cli.trash_count", n=len(notes)))
    for n in notes:
        print(f"  #{n['id']} {n['text'][:60]}")

    if not args.yes:
        print("\n" + t("cli.no_way_back"), end=" ")
        word = t("cli.purge_word")
        try:
            answer = input(t("cli.purge_prompt", word=word)).strip()
        except (EOFError, KeyboardInterrupt):
            print("\n" + t("cli.cancelled"))
            return 1
        if answer != word:
            print(t("cli.cancelled"))
            return 1

    r = _request(cfg, "DELETE", "/trash", {"confirmed": True}).json()
    print(t("cli.purged", n=r["purged"]))
    return 0


def cmd_restore(cfg: Config, args) -> int:
    n = _request(cfg, "POST", f"/notes/{args.note_id}/restore").json()
    print(f"de volta: #{n['id']} {n['text']}")
    return 0


def cmd_today(cfg: Config, args) -> int:
    """The Digest. Deterministic and instant — the LLM does not come in (ADR 0003)."""
    d = _request(
        cfg, "GET", f"/today{'?date=' + args.date if args.date else ''}", timeout=TIMEOUT_CAL
    ).json()
    print(f"— {d['date']} —")
    if d.get("calendar_warming"):
        print(f"  ({t('cli.calendar_warming')})")
    if d.get("weather") and d["weather"].get("temperature") is not None:
        w = d["weather"]
        print(f"  {_fmt_num(w['temperature'])}{w['unit'] or '°C'}, {w['condition'] or ''}"
              f"{', umidade ' + str(w['humidity']) + '%' if w.get('humidity') is not None else ''}")

    if not d["calendar_available"]:
        print(f"  {d['calendar_error']}")
    elif not d["events"]:
        print("  " + t("cli.calendar_empty"))
    else:
        print("  agenda:")
        for e in d["events"]:
            when = t("cli.all_day") if e["all_day"] else f"{e['start'][11:]}-{e['end'][11:]}"
            print(f"    {when:14} {e['summary']}")

    if not d["tasks"]:
        print(f"  {t('cli.tasks')}: {t('cli.nothing_due')}")
    else:
        print(f"  {t('cli.tasks')}:")
        # `ta` runs from the same source tree, so the CLI is always the new code
        # while the daemon is whatever was there at the last restart. Without this
        # line, an old daemon would give a `KeyError` on `horizon` — loud, and
        # useless. Saying what to do beats a traceback, and beats degrading
        # silently.
        if "horizon" not in d["tasks"][0]:
            print("    (" + t("daemon.outdated").replace("\n", " ") + ")")
        # No `sorted`: the daemon already returns display order — overdue first,
        # priority within the band (ADR 0010). The label stays at the front
        # because at the end of the line it was easy to miss.
        for n in d["tasks"]:
            late = f"  {t('cli.overdue')}" if n.get("horizon") == "overdue" else ""
            prio = _prio_label(n["priority"])
            print(f"    {prio} #{n['id']} {n['text']}{late}")
    return 0


def cmd_rules(cfg: Config, args) -> int:
    """`ta rules check` validates without booting; without `check`, it lists what is live."""
    if args.check:
        # Loads locally, without talking to the daemon: it is the check that works
        # to run before restarting the service.
        from pathlib import Path

        from .daemon import user_rules_dir
        from .engine import load_rules

        # `user_rules_dir()`, and NOT `Path.cwd() / "rules"`. ADR 0014 moved the
        # Rules to `~/.config/ta/rules/` and turned the repository's `rules/` into
        # `examples/rules/`, so the old default pointed at a directory that no
        # longer exists — from anywhere but the repository root it pointed at
        # nothing at all.
        #
        # The failure mode is the bad kind: `load_rules` treats a missing directory
        # as "no rules", so the command printed `0 rule(s), 0 with errors` and
        # exited 0. This is the check you run BEFORE `systemctl --user restart ta`,
        # so it was handing out a green light without reading a single rule. Caught
        # by running it on a machine that has two.
        rules_dir = Path(args.dir) if args.dir else user_rules_dir()
        print(f"  · {rules_dir}\n")
        rep = load_rules(rules_dir)
        for r in rep.rules:
            print(f"  ok   {r.name}  ({', '.join(str(trig) for trig in r.on)})")
        for path, tb in rep.errors:
            print(f"  ERROR {path}", file=sys.stderr)
            print("       " + tb.strip().splitlines()[-1], file=sys.stderr)
        print("\n" + t("cli.rules_loaded", n=len(rep.rules), bad=len(rep.errors)))
        return 0 if rep.ok else 1

    d = _request(cfg, "GET", "/rules").json()
    for r in d["rules"]:
        print(f"  {r['name']}  ({', '.join(r['on'])})")
    for e in d["errors"]:
        print(f"  ERROR {e['file']}", file=sys.stderr)
    return 0


def cmd_organize(cfg: Config, args) -> int:
    r = _request(cfg, "POST", "/organize", timeout=TIMEOUT_LLM).json()
    print(f"{r['placed']} nota(s) reorganizada(s) por {r['model']}")
    if r.get("skipped_pinned"):
        print(t('cli.skipped_pinned', n=r['skipped_pinned']))
    if r.get("groups"):
        print("grupos: " + " → ".join(r["groups"]))
    return 0


def cmd_prose(cfg: Config, args) -> int:
    print(_request(cfg, "POST", "/digest-prose", timeout=TIMEOUT_LLM).json()["text"])
    return 0


def cmd_init(cfg: Config, args) -> int:
    """The first-run interview. Without it, "priority" is a guess."""
    current = _request(cfg, "GET", "/priorities").json()
    if current.get("content") and not args.force:
        print(current["content"])
        print("\n" + t("cli.priorities_exist"))
        return 0
    answers = {}
    print(t("cli.interview_intro") + "\n")
    for key, question in current["questions"]:
        try:
            answers[key] = input(f"{question}\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n" + t("cli.cancelled"))
            return 1
        print()
    r = _request(cfg, "POST", "/priorities", {"answers": answers}).json()
    print(r["content"])
    return 0


def _migrate_layout(cfg: Config) -> list[str]:
    """Move the configuration to `~/.config/ta/`, losing nothing. Idempotent.

    **It copies, never moves**, and never overwrites what is already there.
    Running it twice costs nothing, which is what allows calling it from inside
    `doctor` without asking first.
    """
    from .config import config_dir, config_file
    from .daemon import EXAMPLE_RULES

    done = []
    target = config_dir()
    target.mkdir(parents=True, exist_ok=True)

    if not config_file().exists():
        example = EXAMPLE_RULES.parent / "config.toml"
        if example.exists():
            shutil.copy2(example, config_file())
            done.append(t("doctor.created", path=config_file()))

    rules_dir = target / "rules"
    legacy = EXAMPLE_RULES.resolve().parents[1] / "rules"
    if not rules_dir.exists():
        rules_dir.mkdir(parents=True)
        # The legacy is `<repo>/rules` for whoever already used it: their rules
        # are THEIRS, and losing them over a layout change would be unacceptable.
        # Whoever has no legacy starts empty — the examples are copied
        # deliberately.
        source = legacy if legacy.is_dir() else None
        if source:
            for f in source.glob("*.py"):
                shutil.copy2(f, rules_dir / f.name)
            done.append(t("doctor.rules_copied",
                           n=len(list(rules_dir.glob("*.py"))), path=rules_dir))
        else:
            done.append(t("doctor.created", path=rules_dir))
    return done


def cmd_doctor(cfg: Config, args) -> int:
    """What works on this machine, and what to do about what does not.

    **It does not talk to the daemon**, on purpose: whoever most needs this
    command is the person whose daemon would not start.
    """
    from .capabilities import environment, inspect
    from .config import load_env_file

    # Without this the diagnosis lies: the CLI never receives the `.env` (only
    # systemd does, through EnvironmentFile), so `HA_TOKEN` and `GEMINI_API_KEY`
    # would show as absent on a machine where they are configured and working. A
    # false negative here sends people to fix what is not broken — worse than not
    # diagnosing at all.
    if (env_path := load_env_file()) is not None:
        cfg = Config.from_env()
        print("  · " + t("doctor.read_env", path=env_path) + "\n")

    done = _migrate_layout(cfg)
    for f in done:
        print(f"  · {f}")
    if done:
        print()

    for key, value in environment(cfg):
        print(f"  {key:10} {value}")
    print()

    caps = inspect(cfg)
    largura = max(len(c.label) for c in caps)
    quebrado = False
    for c in caps:
        mark = "ok  " if c.ok else ("FALHA" if c.essential else "—   ")
        print(f"  {mark} {c.label:{largura}}  {c.reason}")
        if not c.ok and c.fix:
            print(f"       {' ' * largura}  → {c.fix}")
        quebrado = quebrado or (c.essential and not c.ok)

    print()
    live = sum(1 for c in caps if c.ok)
    print("  " + t("cli.doctor_summary", live=live, total=len(caps)))
    # Exits non-zero only when the CORE is broken. A missing integration is the
    # normal state of a fresh clone, and must not look like an error.
    return 1 if quebrado else 0


def cmd_lang(cfg: Config, args) -> int:
    """Show or set the language. It does not talk to the daemon: a local decision.

    With no argument, it says the language **and where it came from** — saying
    just "pt" does not help somebody who expected English, because the next
    question is always "why?".

    The language belongs to the INSTALLATION, not the invocation:
    `TA_LANG=en ta note "..."` does not change how the note is read, because the
    daemon does the parsing and it resolved the language at its own boot. That is
    the right behaviour, not a limitation — if it varied per call, two notes
    captured on the same day would read `@03/04` as different dates, and the
    database would store both as if they were the same thing (ADR 0013).

    That is why setting it requires a restart, and the message says so.
    """
    from .config import config_file

    if not args.code:
        print(f"{lang()}  (de: {lang_source()})")
        return 0

    path = config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    # Rewriting the key where it already is preserves comments and order; a
    # round-trip `tomllib` does not exist in the standard library, and bringing a
    # TOML writer in just for this would be a new dependency for one line.
    for i, line in enumerate(lines):
        if line.strip().startswith("lang"):
            lines[i] = f'lang = "{args.code}"'
            break
    else:
        lines.insert(0, f'lang = "{args.code}"')
    path.write_text("\n".join(lines) + "\n")

    reset_cache()
    from .config import _user_config

    _user_config.cache_clear()
    print(t("cli.lang_set", code=args.code, path=path))
    print(t("cli.lang_after_restart"))
    print("  systemctl --user restart ta")
    return 0


def cmd_priorities(cfg: Config, args) -> int:
    if not args.instruction:
        c = _request(cfg, "GET", "/priorities").json()["content"]
        print(c or t("cli.priorities_unset"))
        return 0
    r = _request(
        cfg, "POST", "/priorities", {"instruction": " ".join(args.instruction)},
        timeout=TIMEOUT_LLM,
    ).json()
    print(r["content"])
    return 0


def cmd_event(cfg: Config, args) -> int:
    """Propose an event from a note, and only store it with your yes (ADR 0007)."""
    payload = {"note_id": args.note_id} if args.note_id else {"text": " ".join(args.text or [])}
    r = _request(cfg, "POST", "/detect-event", payload, timeout=TIMEOUT_LLM).json()
    c, targets = r["candidate"], r["targets"]

    if not c["is_event"]:
        print(t("cli.not_an_event"))
        return 0

    print("▶ evento detectado:")
    print(f"    {t('cli.event_title')}: {c['title']}")
    print(f"    {t('cli.event_when')}: {c['start'].replace('T', ' ')} - {c['end'][11:]}")
    print(f"    agenda: Terminal Assistant ({c['account']})")
    print(f"    {t('cli.event_confidence')}: {c['confidence']:.0%}")
    if c["account"] not in targets:
        raise Problem(t("cli.no_dedicated_calendar", account=c["account"]))

    try:
        resp = input("\n" + t("cli.event_prompt")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\n" + t("cli.cancelled"))
        return 1
    # The accepted key comes from the catalogue, like the prompt. It was hardcoded
    # to `"s"`, so the English prompt said `[y] create` and typing `y` refused the
    # event without a word.
    if resp != t("cli.confirm_key"):
        print(t("cli.nothing_created"))
        return 0

    out = _request(cfg, "POST", "/calendar/event", {
        "confirmed": True,
        "source_uid": targets[c["account"]],
        "title": c["title"], "start": c["start"], "end": c["end"],
        "note_id": args.note_id,
    }).json()
    print(f"criado na agenda dedicada (uid {out['uid'][:12]}…), sem convidados.")
    return 0


def cmd_capture_popup(cfg: Config, args) -> int:
    """Quick capture for the global shortcut. With no terminal open.

    It uses zenity if present; without it, it falls back to a notification
    explaining the fix — a keyboard shortcut must never fail silently.
    """
    import subprocess

    zenity = shutil.which("zenity")
    if zenity is None:
        subprocess.run(
            ["notify-send", "Terminal Assistant", t("cli.install_zenity")],
            check=False,
        )
        raise Problem(t("cli.install_zenity"))
    proc = subprocess.run(
        [zenity, "--entry", "--title=Nota", "--text=Nova nota:", "--width=460"],
        capture_output=True, text=True, check=False,
    )
    text = proc.stdout.strip()
    if not text:
        return 0
    note = _request(cfg, "POST", "/notes", {"text": text}).json()
    subprocess.run(["notify-send", "Nota salva", _fmt_note(note)], check=False)
    return 0


# Each group carries its OWN commands. The capability probe only adds the
# unavailable marker — it never decides what appears. Without that separation, a
# probe that fails would make `--help` hide half the commands, which is exactly
# what happened in an environment with no D-Bus.
HELP_GROUPS = (
    ("notes", "Notes and tasks", "note list done rm restore board export today"),
    ("home", "House", "on off luz light entities temp router media"),
    ("calendar", "Calendar", "today event"),
    ("ai", "AI", "init priorities organize prose revise event"),
    ("lighter", "Ringlight", "lighter"),
    (None, "Tooling", "doctor lang rules capture-popup"),
)


def _epilogue() -> str:
    """The command list grouped by subsystem, marking what does not work.

    It exists because `ta --help` announced 23 commands, of which 14 require a
    Home Assistant, a GNOME extension or an AI key the visitor may not have —
    with no marker saying so. The difference between "a tool with optional
    integrations" and "a broken tool" is that marker.

    It reads the SAME registry as `ta doctor` and `/health`, so the three cannot
    disagree about what is alive.

    It degrades to the bare label if the probe fails: a `--help` that raises is
    far worse than a `--help` with no availability marker.
    """
    from .capabilities import inspect

    try:
        by_key = {c.key: c for c in inspect()}
    except Exception:  # noqa: BLE001 — help must never die because of a probe
        by_key = {}

    lines = []
    for key, title, commands in HELP_GROUPS:
        cap = by_key.get(key) if key else None
        marker = f"  —  {cap.summary}" if (cap and not cap.ok) else ""
        lines.append(f"  {title}{marker}\n      {commands}")

    return (
        "commands, by subsystem:\n\n"
        + "\n".join(lines)
        + "\n\n`ta doctor` says what each one is missing, and how to get it."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ta",
        description="Terminal Assistant",
        epilog=_epilogue(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # `metavar` swaps the 23-name brace list in the usage line for a marker; the
    # real list is the epilogue, which knows what is available.
    sub = p.add_subparsers(dest="cmd", required=True, metavar="<command>")

    n = sub.add_parser("note", help="capture a note")
    n.add_argument("text", nargs="+")
    n.set_defaults(func=cmd_note)

    ls = sub.add_parser("list", help="list open notes")
    ls.add_argument("--all", action="store_true", help="include completed ones")
    ls.set_defaults(func=cmd_list)

    d = sub.add_parser("done", help="complete a note")
    d.add_argument("note_id", type=int)
    d.set_defaults(func=cmd_done)

    rv = sub.add_parser("rm", help="delete a note (reversible)")
    rv.add_argument("note_id", nargs="?", type=int)
    rv.add_argument("--list", action="store_true", help="show the trash")
    rv.add_argument(
        "--purge",
        action="store_true",
        help="permanently delete what is in the trash (no undo)",
    )
    rv.add_argument("--yes", action="store_true", help="do not ask (for scripts)")
    rv.set_defaults(func=cmd_rm)

    rs = sub.add_parser("restore", help="take a note out of the trash")
    rs.add_argument("note_id", type=int)
    rs.set_defaults(func=cmd_restore)

    sub.add_parser("revise", help="the LLM re-tags every open note").set_defaults(
        func=cmd_revise
    )

    sub.add_parser("export", help="dump the notes as markdown").set_defaults(func=cmd_export)
    sub.add_parser("board", help="open the board in your browser").set_defaults(func=cmd_board)

    t = sub.add_parser("today", help="the day's events and tasks")
    t.add_argument("--date", help="YYYY-MM-DD (default: today)")
    t.set_defaults(func=cmd_today)

    # `luz` stays, and `light` joins it: it was the only Portuguese command of the
    # 23, and renaming it would cost the muscle memory of whoever already uses it
    # without buying anything the alias does not.
    for name in ("luz", "light"):
        lz = sub.add_parser(name, help="turn a light on, or set its brightness")
        lz.add_argument("entity", help="an alias (bedroom) or an entity_id")
        lz.add_argument("brightness", nargs="?", type=int, help="0-100")
        lz.set_defaults(func=cmd_light)

    of = sub.add_parser("off", help="turn everything off, or one entity")
    of.add_argument("entity", nargs="?")
    of.set_defaults(func=cmd_off)

    on = sub.add_parser("on", help="turn on an entity, a group (luz, tudo) or a room")
    on.add_argument("entity", help="entity_id, apelido, grupo ou name de environment")
    on.add_argument("brightness", nargs="?", type=int, help="0-100, only on light.")
    on.set_defaults(func=cmd_on)

    sub.add_parser("entities", help="list the inventory coming from Home Assistant").set_defaults(
        func=cmd_entities
    )
    sub.add_parser("router", help="external IP and speed").set_defaults(func=cmd_router)
    sub.add_parser("temp", help="temperature, weather and the plug's consumption").set_defaults(
        func=cmd_temp
    )

    lg = sub.add_parser("lighter", help="control the light ring")
    lg.add_argument("action", nargs="?", choices=["on", "off", "toggle"], default="toggle")
    lg.add_argument("--profile", help="aplica um profile por name")
    lg.set_defaults(func=cmd_lighter)

    md = sub.add_parser("media", help="media on an Echo (a Home Assistant media_player)")
    md.add_argument(
        "alvos",
        nargs="*",
        metavar="[entity] [action]",
        help=f"action: {', '.join(MEDIA_ACTIONS)}. With no entity, uses TA_ECHOS",
    )
    md.add_argument("--volume", type=int)
    md.add_argument("--play", nargs="+", help="o que tocar")
    md.add_argument("--announce", nargs="+", help="texto para falar")
    md.set_defaults(func=cmd_media)

    sub.add_parser("organize", help="the LLM groups and orders, and stores it").set_defaults(
        func=cmd_organize
    )
    sub.add_parser("prose", help="the day in prose (optional)").set_defaults(func=cmd_prose)
    sub.add_parser("capture-popup", help="quick capture (for the global shortcut)").set_defaults(
        func=cmd_capture_popup
    )

    ini = sub.add_parser("init", help="the priorities interview")
    ini.add_argument("--force", action="store_true")
    ini.set_defaults(func=cmd_init)

    pr = sub.add_parser("priorities", help="show or adjust priorities by prompt")
    pr.add_argument("instruction", nargs="*")
    pr.set_defaults(func=cmd_priorities)

    ev = sub.add_parser("event", help="propose an event from a note")
    ev.add_argument("--note-id", type=int)
    ev.add_argument("text", nargs="*")
    ev.set_defaults(func=cmd_event)

    rl = sub.add_parser("rules", help="list or validate the rules")
    rl.add_argument(
        "check", nargs="?", const=True, default=False, help="validate without the daemon"
    )
    rl.add_argument("--dir", help="rules directory")
    rl.set_defaults(func=cmd_rules)

    lg = sub.add_parser("lang", help="show or set the language (pt | en)")
    lg.add_argument("code", nargs="?", choices=LANGS, help="the language to set")
    lg.set_defaults(func=cmd_lang)

    sub.add_parser(
        "doctor", help="what works on this machine, and how to fix the rest"
    ).set_defaults(func=cmd_doctor)

    # Argparse's flat list goes: it would repeat the 25 commands the epilogue
    # already shows grouped, without saying which work here. `_choices_actions` is
    # private API, and the `getattr` is what guarantees the worst consequence of
    # it being renamed is a redundant help — never a crash.
    if (entries := getattr(sub, "_choices_actions", None)) is not None:
        entries.clear()
    return p


def main(argv: list[str] | None = None) -> int:
    # The `.env` comes first. Systemd hands it to the daemon, but nobody handed
    # it to the CLI — and that had two consequences: `ta doctor` and `ta --help`
    # reported `HA_TOKEN is not set` on a machine where it was configured and
    # working, and a `TA_PORT` in the file applied to the daemon and not to the
    # CLI, which kept hitting the default port.
    #
    # It does not overwrite what is already in the environment, so
    # `TA_LANG=en ta ...` still wins over the line in the file.
    load_env_file()

    # The CLI is no place for library logs. Without this, probing the
    # capabilities to build `--help` printed `WARNING`s from Lighter and the
    # calendar on top of the help itself. The daemon configures its own logging.
    logging.basicConfig(level=logging.ERROR, format="ta: %(message)s")

    args = build_parser().parse_args(argv)
    try:
        return args.func(Config.from_env(), args)
    except Problem as e:
        print(f"ta: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
