# Terminal Assistant

A personal command-line assistant for a single machine, bringing smart-home
control and daily note capture under the same tool. What unites the two halves is
a shared trigger engine.

Terms are written in English. For the home half that is not a style choice: Home
Assistant's vocabulary is English, and it is the source of truth for what is in
the house — using a different word for the same thing would mean translating at
every boundary.

## Language

### Home

**Device**:
A physical appliance — a lamp, a plug. It exists in the world, has a brand and a
protocol.
_Avoid_: gadget, thing, unit

**Entity**:
The smallest controllable or observable unit exposed by Home Assistant. A single
Device can expose several Entities, and a command always addresses an Entity —
never a Device.
_Avoid_: item, resource, object

### Automation

**Trigger**:
The condition that starts an automation. It can come from time, from a signal
produced by the machine itself, or from an Entity changing state.
_Avoid_: event, hook, fire

**Rule**:
A Trigger, its conditions and its actions, taken as one unit. It is the shape an
automation takes once written down.
_Avoid_: automation, recipe, scene

### Notes

**Note**:
The unit of capture: a piece of text the user wrote down. It is the only entity
in the notes half — the roles it takes on come from the presence of attributes,
never from a type chosen at capture time.
_Avoid_: item, card, entry

**Task**:
The role a Note takes on when it gains a deadline. It is chaseable and can be
completed. Not a separate entity.
_Avoid_: to-do, item

**Horizon**:
The band of time a Task's deadline falls into, counted from today: `overdue`,
`today`, `week` (the next 7 days, a rolling window) or `later`. It is **derived
from the clock and never stored** — the same Note changes band at midnight
without anything being written. A Note with no deadline has Horizon `later`,
alongside the distant future. It is the first criterion of display order: the
Horizon says *when*, and Priorities only orders within the band.
_Avoid_: deadline, urgency, bucket

**Reminder**:
The role a Note takes on when it gains a moment to fire. It is consumed when it
fires. Not a separate entity.
_Avoid_: alarm, alert

**Status**:
A Note's state in the work queue: `todo`, `doing`, `hold`, `done` or `cancelled`.
It is an axis of its own, independent of the roles — a Note can be a Task and be
on `hold`. `done` and `cancelled` are both terminal: the Note left the queue, by
different doors.
_Avoid_: state, column, stage

**Post-it**:
The visual representation of a Note on the board. It is drawing, never data —
nothing in the model *is* a Post-it.
_Avoid_: using it as a synonym for Note

**Priorities**:
The description, maintained by the user, of what matters to them: their work,
what is relevant, what comes first. It is what gives the word "priority" meaning
when a Note is ordered — within a Horizon, never above it.
_Avoid_: profile, preferences, context

**Digest**:
The consolidated view of one day, bringing together calendar events and chaseable
Notes.
_Avoid_: summary, briefing, agenda

### Runtime

**Capability**:
A subsystem that may be alive or dead on this particular machine — notes,
calendar, microphone, home, ringlight, AI. A Capability knows whether it is
available, **why not** when it is not, and **how to fix it**. One registry
answers for all of them, and `ta doctor`, `/health`, the grouped `--help` and the
runtime error messages all read from it, so they cannot disagree.
_Avoid_: feature, module, integration (when the availability is what matters)

**Language**:
The active language of the tool, resolved once per process from `TA_LANG`, the
config file, or the system locale. It governs three surfaces together — the
capture parser, the interface text, and the language the model writes prose in.
Exactly one is active at a time, because accepting two would make a numeric date
ambiguous.
It is **not** the language of the documentation, which is a separate and fixed
decision.
_Avoid_: locale, i18n, translation

### Disambiguation

**Profile** is ambiguous in this project and should not be used on its own. The
[Lighter](https://github.com/joaoferrete/Lighter) extension calls a set of
ring-light appearance values a _profile_. The description of what matters to the
user is **Priorities**. When Lighter's meaning is needed, write _Lighter
profile_.
