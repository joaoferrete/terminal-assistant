# The bot handles a message or captures it — it never drops one

A message to the bot can be a thought to capture, a command for the house, or a
question. Deciding which takes a model, and [ADR 0003](0003-the-llm-is-never-on-the-critical-path.md)
promised that capture never depends on one. The two are reconciled by one
invariant: **every message addressed to the bot is either handled or becomes a
Note.**

In order: a deterministic pre-router resolves slash commands and the home grammar
the CLI already has, with no model. Everything else goes to an LLM agent that can
only call **Tools** — Python functions declared with `@tool`, each naming the Grant
it needs and whether it is destructive. If the model is down, slow or returns
garbage, the message is captured as a Note and the reply says so.

There is no shell Tool. A destructive Tool asks for confirmation with a button.

In an allowlisted group the bot reads every message, and actionable ones go straight
into a household List. This is an explicit exception to "the model proposes, the
user confirms" ([ADR 0007](0007-propose-and-confirm-before-writing-to-the-calendar.md)),
and it holds only because adding a List item is cheap and reversible. Every action
leaves a **Receipt** tied to the message that caused it: undo, and "what did you do
here?", both read it. Proactive capture can never reach a destructive Tool.

## Considered Options

- **Capture by default; talk only behind a prefix.** Keeps ADR 0003 literally.
  Rejected: "turn off the light" without the prefix becomes a Note, and talking
  means remembering syntax.
- **The model decides everything.** Least friction. Rejected: when the provider is
  down the bot goes silent and the brain dump is lost.
- **A shell-command allowlist, or free shell with confirmation.** Rejected:
  arguments supplied by the model become command injection, and a confirmation read
  on a phone is eventually approved without being read.
