# Working on this repository as an agent

This file is for coding agents — Claude Code, Copilot, Cursor, whatever comes next.
It is not a second copy of [`CONTRIBUTING.md`](CONTRIBUTING.md): everything about
*why* the project is shaped this way lives there, and every rule below links to its
owner rather than restating it.

What is here is the short list of things an agent gets wrong on this codebase
specifically — collected from the times one did.

## Before you finish, run these three

```bash
make lint && make test
python scripts/scan_language.py
python scripts/scan_secrets.py
```

CI runs all of them plus the history scan. If you are opening a PR, also check the
title, because that is gated too:

```bash
python scripts/pr_title.py "fix(board): o checkbox de concluídas voltou a aparecer"
```

## The seven that actually bite

### 1. Never write a user-visible string in English. Or in Portuguese.

It goes in the catalogue, `src/ta/i18n.py`, with both languages on the same line.
A string written directly in either language is not translated — it is
*untranslatable*, and it will read as broken to half the users.

This is the single most repeated mistake in this project's history. Forty-odd
strings were fixed in one pass and three more were found the next day, in `ta list`,
`ta doctor` and a CSS `content:` property. The full rule, with the table of what
stays Portuguese forever, is in
[CONTRIBUTING § Language](CONTRIBUTING.md#language).

### 2. Portuguese in code is a `scripts/scan_language.py` failure, not a style opinion

Comments, docstrings and identifiers are English. Stored tags (`trabalho`,
`anotacao`), group names (`luz`, `tudo`) and typed marks (`!alta`, `@sexta`) are
Portuguese **forever** — somebody's database holds them.

Do not verify this by looking for accented characters. It does not work:
`canceladas ficam de fora: revisar prazo de coisa encerrada` has none and survived
three manual sweeps. Run the script.

### 3. A rename can break code that is not in this repository

`ctx.calendar.agora()` is called by rules in `~/.config/ta/rules/`, on people's
machines, outside any checkout. So `agora`/`hoje` still exist and point at the same
objects as `now`/`today`, with a test pinning the identity.

Before renaming anything public, ask where else it is typed: a rule file, a
`config.toml` key, a `localStorage` key, a CLI subcommand, a stored DB value. If the
answer is "outside the repo", keep the old name working.

### 4. `ruff` is not optional after a mechanical edit

Two real `F821`s came from find-and-replace across a file — one caught by ruff, one
that the whole 333-test suite missed because the line needed a live Home Assistant.
When you rename with a script, lint is the only thing that sees the leftovers.

And rename **identifiers before function names**, never the reverse: doing it the
other way produced `test_apelido_do_file_uivo_e_usado`, which is a word in no
language and which no test checks.

### 5. "Returns 200 and contains the string" is not interface verification

If you touched the board, open it and look. Five bugs in this project were visible
only on screen: a clipped counter, an invisible swatch, a warning hidden behind a
post-it, dark-on-dark text, and a delete button that had escaped its card. A sixth —
the "done" checkbox disappearing from every view — was found by reading, and would
have been found in one second by looking.

`make demo` boots an isolated daemon on fake data, so there is no excuse.

### 6. Don't touch the author's running installation

The daemon is a live `systemd --user` service with real notes in
`~/.local/share/ta/ta.db`, real rules in `~/.config/ta/rules/`, and real credentials
in `.env`. The working tree is an editable install, so the CLI picks up your changes
immediately and the daemon does not until a restart.

Restarting the service, editing `~/.config/ta/`, and running `pkill` are the user's
calls, not yours. When killing a demo daemon, target it **by port**, not by a
pattern that also matches `ta.daemon`.

### 7. Write the commit message the repository already writes

Subject line `type(scope): subject`; body as **problem → decision → what you found
on the way**. That last section is not filler — it is where half the value of this
`git log` lives, and it is the part an agent is most tempted to skip because nothing
enforces it.

The types, the scopes and the reasoning are in
[CONTRIBUTING § Opening a pull request](CONTRIBUTING.md#opening-a-pull-request).

## Two habits worth copying

**Comment the *why*, and the failure that led to the line.** A comment restating the
code is noise here; a comment recording the bug is the most valuable thing in the
file. Somebody — possibly you, in the next session — will want to simplify the line.

**When a guard is wrong, fix the guard.** The allowlist in `scan_language.py` is a
list of decisions with reasons attached, not a mute button, and a test enforces that
each entry has a real one. If a check fires on correct code, that is a bug in the
check.

## What to do when you are unsure

Say so, and say what you would need to decide. This project would rather answer one
question than review a confident guess — and an assumption stated out loud is worth
more than a decision buried in a diff.
