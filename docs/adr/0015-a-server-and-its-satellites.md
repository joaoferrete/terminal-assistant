# A server and its Satellites, not a single machine

`ta` was built as an assistant for one machine: the daemon, the database, the
microphone and the calendar all lived on the same laptop. V2 adds a messaging
Channel and several people, and a Channel has to answer while the laptop is closed.
So the core — database, board, scheduler, Rules, the bot and every model call —
moves to an always-on home server, and the laptop becomes a **Satellite**. A
Satellite reports what only it can sense (the microphone) and performs what only it
can do (the Lighter, desktop notifications).

## Considered Options

- **Keep the core on the laptop, with a thin relay on the server.** Least change.
  Rejected: a voice note sent from the street while the laptop sleeps is either lost
  or waits for hours, which defeats capture.
- **Two independent installations.** Simple to start. Rejected: two sources of truth
  for Notes, and nothing that reconciles them.
- **Everything on the server, dropping the desktop.** Rejected: the meeting Rule,
  the reason the project exists, needs the laptop's microphone and ring light.

## Consequences

- Capture on the laptop now crosses a network. [ADR 0003](0003-the-llm-is-never-on-the-critical-path.md)'s
  promise survives only because the Satellite queues captures locally when the
  server does not answer.
- The meeting Rule runs on the server. The microphone signal travels in, and the
  Lighter action travels back out. Both go over a connection the Satellite opens, so
  the server never has to reach into the laptop.
- The calendar can no longer come from GNOME Online Accounts on the server; see the
  amendment to [ADR 0004](0004-calendar-through-gnome-online-accounts.md).
- Home Assistant moves to the server too, so the house stays controllable with the
  laptop off.
