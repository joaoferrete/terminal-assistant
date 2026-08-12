# Aliases and keyboard shortcuts

Two ways to reach `ta` without typing much: shell aliases for when you are
already in a terminal, and GNOME global shortcuts for when you are not.

## First: `ta` has to be on your `PATH`

This is the step people skip, and the symptom is genuinely confusing — every
command "does not work", and nothing explains why, because the executable simply
is not found.

```bash
export PATH="$HOME/terminal-assistant/.venv/bin:$PATH"
```

Put it in your `~/.zshrc` or `~/.bashrc`, **before** any aliases, and check with
`which ta`. If that prints nothing, nothing below will work.

It happened here. It looked like a broken Home Assistant integration for an
entire debugging session.

## Shell aliases

Paste at the end of your shell config, then reload it. Adjust the names to
whatever language you think in — that is the point of aliases.

```bash
# --- terminal-assistant ---
# House
alias lon="ta on"                  # lon light | lon bedroom | lon bedroom 40
alias loff="ta off"                # loff | loff light | loff fan
alias ring="ta lighter toggle"
alias house="ta temp"              # weather, and whether the fan is drawing power

# Notes
alias n="ta note"                  # n "call the dentist @friday #health !high"
alias nn="ta list"                 # what is open
alias board="ta board"             # opens the board in your browser
alias org="ta organize"            # asks the model and stores the order

# The day
alias today="ta today"             # events + chaseable tasks

# Maintenance
alias revise="ta revise"           # re-tags everything open
alias trash="ta rm --list"         # what has been deleted
```

`ta on` with no number turns things on at full. The term can be a group (`luz`,
`luzes`, `tomada`, `tudo`), a room (`bedroom`), an alias you defined, or a full
`entity_id` — the resolution order is in
[home-assistant.md](home-assistant.md#groups-and-rooms).

The capture syntax is in [notes.md](notes.md#the-marks).

## Global shortcuts (GNOME / Wayland)

On Wayland a global shortcut is registered with GNOME and runs a command. Three
traps, all of them encountered for real:

1. **The `custom-keybindings` list is rewritten whole, never appended to.** If
   you set only your new shortcuts, your existing ones silently disappear. The
   commands below preserve whatever is already at `custom0` and `custom1` —
   check yours first and adjust.
2. **Shell aliases do not work here.** GNOME does not open an interactive shell,
   so `lon` does not exist for it. Use the absolute path to the executable.
3. **Check for conflicts before choosing a combination.** `<Control><Alt>l` is
   taken by the [Lighter][lighter] extension, for instance. The examples use
   `<Super><Alt>`, which is mostly free.

### Registering them

```bash
SD=org.gnome.settings-daemon.plugins.media-keys
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
TA="$HOME/terminal-assistant/.venv/bin/ta"   # absolute path, not an alias

# 1. See what you already have, so you do not erase it.
gsettings get $SD custom-keybindings

# 2. The complete list: yours plus the new ones.
gsettings set $SD custom-keybindings "[
  '$P/custom0/', '$P/custom1/',
  '$P/custom2/', '$P/custom3/', '$P/custom4/', '$P/custom5/'
]"

# 3. Three keys per shortcut: name, command, binding.
set_kb() {  # set_kb <customN> <name> <command> <binding>
  local k="$SD.custom-keybinding:$P/$1/"
  gsettings set "$k" name    "$2"
  gsettings set "$k" command "$3"
  gsettings set "$k" binding "$4"
}

set_kb custom2 'TA: lights on'  "$TA on luz"        '<Super><Alt>l'
set_kb custom3 'TA: lights off' "$TA off"           '<Super><Alt>o'
set_kb custom4 'TA: capture'    "$TA capture-popup" '<Super><Alt>n'
set_kb custom5 'TA: ringlight'  "$TA lighter toggle" '<Super><Alt>r'
```

### Checking

```bash
gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings
```

It should list every path — yours and the new ones. If it lists only the new
ones, the list was overwritten and your originals need putting back.

To inspect one:

```bash
SD=org.gnome.settings-daemon.plugins.media-keys
P=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings
for k in name command binding; do
  gsettings get "$SD.custom-keybinding:$P/custom2/" $k
done
```

### Quick capture

`ta capture-popup` is the one worth binding. It opens a small input with no
terminal, saves, and shows a notification. It uses `zenity` if present; without
it, you get a notification explaining that rather than silent failure — a
keyboard shortcut that does nothing is the worst possible outcome.

```bash
sudo apt install zenity
```

### About the graphical settings

Settings → Keyboard manages these same shortcuts and uses the `custom0`,
`custom1`, … convention — which is what the commands above follow, so the two
paths coexist. A shortcut you add through the interface afterwards becomes the
next `customN` and the list updates itself.

## Is the daemon up?

Every alias speaks HTTP to the daemon. If it is not running, commands fail with a
readable message rather than hanging:

```bash
systemctl --user status ta
systemctl --user restart ta
journalctl --user -u ta -f      # watch automations firing
```

[lighter]: https://github.com/joaoferrete/Lighter
