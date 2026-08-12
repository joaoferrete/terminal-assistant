# The user's configuration and rules leave the repository

Entity aliases lived in `src/ta/config.py`, as source code:

```python
ENTITY_ALIASES = {
    "quarto": "light.bedroom_lamp",
    "ventilador": "switch.fan_socket_1",
}
```

And Rules lived in `<repo>/rules/`, resolved from the package's own file. The
example rule opens by saying, in as many words, "this file is versioned on
purpose".

While the repository belonged to one person, both were right. It is their house,
versioned next to the code that commands it, and editing and committing is the
workflow.

Opened up, those same two sentences become defects. To configure their own house,
whoever clones has to **edit files inside the installed package** — a change lost
on any reinstall — and **inside the git checkout**, which conflicts on every
`git pull`. And the example rule would load on their boot, aiming at a lamp that
does not exist in their house.

So:

- Aliases and groups move to `~/.config/ta/config.toml`, respecting
  `XDG_CONFIG_HOME`.
- The user's Rules move to `~/.config/ta/rules/`.
- The repository's `rules/` becomes `examples/rules/`: versioned documentation,
  **not loaded**.

The pattern is not new to the project — `db.default_db_path()` already put the
database in `~/.local/share/ta/`. Configuration simply had not followed.

## Considered Options

- **Environment variables in the `.env` that already exists.**
  `TA_ALIASES="bedroom=light.x"` would cost no new file and would reuse systemd's
  `EnvironmentFile`. Rejected because it does not cover Rules — Python code does
  not fit in an environment variable — and because a string with its own syntax
  ages badly the moment somebody wants a group.
- **Leave it as it is, documenting "fork and edit".** Coherent with "personal
  project, open for reading", and rejected along with that positioning: it
  guarantees a merge conflict on every update.
- **Only the aliases move, Rules stay in the repository.** Less work, and it
  leaves exactly the worst piece standing — the example rule loading against the
  wrong house.
- **A configuration directory inside the repository** (`.ta/`, gitignored). Keeps
  everything together and is easy to find. Rejected because it ties configuration
  to the checkout: moving or re-cloning the repository would lose the house, and a
  second checkout would have a second house.
- **Loading `examples/rules/` too, for convenience.** Rejected: a Rule aiming at a
  non-existent entity gives no useful error — Home Assistant answers and nothing
  happens. An example has to be copied deliberately.

## Consequences

- **Nothing breaks in an existing installation.** The loader prefers
  `~/.config/ta/rules/` and **falls back** to `<repo>/rules` when the new
  destination does not exist, logging why. `ta doctor` **copies**, never moves.
- **An installation with no `config.toml` is a usable state, not a broken one.**
  It is the state of everybody who clones. `ta on light.whatever` is still exact,
  and `ta on bedroom` still matches by substring against Home Assistant's own
  inventory — resolution by room never depended on an alias.
- **Groups stay built in; aliases do not.** `luz`, `tomada` and `tudo` hold for
  any house and are not personal. The user's file adds to them, and a repeated
  name replaces one.
- **A broken `config.toml` does not take the daemon down** — it becomes a
  `WARNING` and empty aliases. Failing to boot over a convenience file would be
  disproportionate; failing **silently** would be worse, because the symptom is
  `ta on bedroom` quietly not working any more.
- **A test guarantees no concrete `entity_id` comes back into `config.py`.** The
  failure mode it causes only appears on somebody else's machine, which is too
  late. It caught an `entity_id` left in a comment.
- **`RULES_DIR` became `user_rules_dir()`**, rather than `rules_dir()`, because
  `create_app` has a parameter by that name and the shadowing would silently turn
  the call into something else.
