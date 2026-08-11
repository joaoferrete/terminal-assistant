# The microphone in use is the meeting trigger; the calendar is context

"I am in a meeting" is detected from the microphone being in use, observed through
PipeWire. The local calendar comes in afterwards as enrichment and as a condition:
it says *which* appointment it is, and enables Rules that distinguish between
kinds of meeting.

The reason for not using the window title is environmental: the session is
Wayland, and no process outside the compositor can read the focused window's
title. The microphone, besides being observable, is a universal signal — it works
for Meet, Zoom, Slack and Discord without knowing the difference between them.

## Considered Options

- **The calendar alone.** Rejected: it is wrong in both directions, firing for
  meetings that did not happen and staying blind to ad-hoc calls.
- **The window title, through a GNOME Shell extension.** Technically possible —
  the [Lighter](https://github.com/joaoferrete/Lighter) extension does exactly
  that, because it runs inside the Shell — but it would only cover meetings in a
  browser, and would add one more surface to maintain.

## Consequences

- There is a predictable false positive: recording audio, or using the microphone
  for anything else, fires the automation. That is acceptable because the action
  is reversible and cheap.
- The ability to read a window title remains available through Lighter, should a
  more precise trigger ever be needed.
