# An agent that acts, through a catalogue, never a shell

The Owner wants the bot to be *agentic*: to carry a request through several steps,
to act on the household's machines (the Satellites) as well as on the house, to
take the initiative on some triggers, and to create automations from a
conversation. Decided in an interview on 2026-09-26.

"Agentic" here means **more reach through more Tools, not a different kind of
reach**. Everything the agent does is still a Python function somebody wrote,
declaring its Grant and whether it changes state (ADR 0017, D16). The guardrails of
ADR 0019 apply to each new thing unchanged: taint, confirmation, filtering by who
asks, a Receipt with an undo.

- **Satellites act through a catalogue.** Lock the screen, volume and media, open
  an app or a URL, a notification, a screenshot, and scripts the Member lists in
  **their own laptop's** config. The laptop decides what exists, as with files
  (D41). The server can ask only for what the laptop offers.
- **Longer tasks.** A turn may take more steps, bounded by the cost ceilings (D30),
  so "find the bill in my email, put it on the calendar and remind me the day
  before" is one request.
- **Initiative, bounded.** Proactive actions start only from an explicit trigger
  the Owner or the Member set up. They run with that Member's Grant, and every one
  is told in the chat with its Receipt and undo. Invisible autonomy is worse than
  none (the review's rule since V1).
- **Automations by chat are data, not code.** "When I leave, turn everything off"
  becomes a stored rule: a trigger, an optional condition, and a Tool call with its
  arguments. The agent never writes a Python rule file. Rule files are code, and a
  model writing code that the daemon imports is a shell with extra steps.

## Considered Options

- **Commands with confirmation.** The agent proposes a shell command, and it runs
  after a button press. Rejected by the Owner: every command becomes a tap, and
  one mistaken approval, perhaps prompted by an injected email, can break the
  machine.
- **A free shell.** Rejected: it reverses D16 and ADR 0019. Any web page, email or
  group message the agent reads could run commands on the server or a laptop.
- **Everything through the model.** Dropping the pre-router so that "apaga a luz"
  also goes through the agent. Rejected by the Owner: slower and more expensive,
  for no new ability.
- **The agent writes Python Rules.** Rejected: it is code execution by another
  name.

## Consequences

- A new ability is a new Tool, reviewed like code, and the catalogue grows one
  Tool at a time. That is slower than a shell, on purpose.
- A Member's Satellite scripts are that Member's own code, allowed by their own
  config, on their own machine. The agent chooses among them and passes no free
  arguments.
- Chat-made rules need a place to be listed, switched off and deleted, in the chat
  and on the board. A rule nobody can see is the invisible autonomy this ADR
  forbids.
