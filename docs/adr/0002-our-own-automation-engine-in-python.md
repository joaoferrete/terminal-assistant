# Our own automation engine, written in Python

Home Assistant has an automation engine, but it cannot see the state of the
user's machine — and every trigger that motivated this project starts there
(microphone in use, time of day, the local calendar). Since the PC's state would
have to be pushed into Home Assistant anyway, the app becomes the only brain: it
evaluates the triggers and treats Home Assistant as a dumb actuator. Rules are
written in Python, with decorators, and live under version control.

## Considered Options

- **Home Assistant as the brain**, receiving the PC's state and calling the app
  back. Rejected: the Rules would leave the repository for Home Assistant's YAML,
  and a purely local action would travel PC → HA → PC.
- **Split by domain** — house in Home Assistant, PC in the app. Rejected: two
  brains means two places to investigate when a light comes on at the wrong time,
  and the condition "and it is past 16:00" does not exist on the
  [Lighter](https://github.com/joaoferrete/Lighter) side.
- **Declarative Rules in YAML.** Rejected on implementation cost: with Python the
  engine is an event dispatcher; with YAML we would have to write an interpreter
  — a parser, a condition evaluator and, inevitably, a template language — before
  the first Rule could run. Home Assistant's own automations are the example of
  where that road ends.

## Consequences

- A syntax error in a Rule file could take the daemon down. Mitigated by loading
  each Rule in isolation, and by a static check command.
- Rules are not editable from a graphical interface, and that trade was
  deliberate: the reason the app is the brain was precisely to have them under
  version control.
