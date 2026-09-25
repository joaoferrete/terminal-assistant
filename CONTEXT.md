# Terminal Assistant

The assistant of a household: smart-home control and note capture under one tool,
united by a shared trigger engine. It runs on one Server, reached through Satellites
and a Channel, by the Members who live there.

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

**List**:
A named collection of Notes with a scope: household (every Member sees it) or
personal. It is the List, not the Note, that decides who sees its items, and Lists
sit outside the Horizon ordering.
_Avoid_: checklist, collection, category

**Post-it**:
The visual representation of a Note on the board. It is drawing, never data —
nothing in the model *is* a Post-it.
_Avoid_: using it as a synonym for Note

**Priorities**:
The description, maintained by each Member, of what matters to them: their work,
what is relevant, what comes first. It is what gives the word "priority" meaning
when a Note is ordered — within a Horizon, never above it.
_Avoid_: profile, preferences, context

**Digest**:
The consolidated view of one day for one Member, bringing together calendar events
and chaseable Notes.
_Avoid_: summary, briefing, agenda

### People

**Member**:
A person of the household whom the Channel recognises, identified by the Channel's
numeric user id, never by a username. A Note belongs to the Member who wrote it.
_Avoid_: user, account, resident

**Owner**:
The Member who administers the installation, invites the others and holds every
Grant.
_Avoid_: admin (a Grant can make other Members admins), root

**Grant**:
A named set of permissions — which Entities, Lists and Tools — assigned to Members.
Every action runs with the Grant of the Member whose message caused it.
_Avoid_: role (that is a Note's role), permission set, profile

**Persona**:
How the bot addresses one Member: the name it uses and its tone. It belongs to the
Member and applies in every conversation.
_Avoid_: profile, personality, preferences

### Channel

**Channel**:
A messaging medium through which Members talk to the bot, such as Telegram. A
private chat and an allowlisted group are both conversations on a Channel.
_Avoid_: bot (the bot is what answers on it), integration, messenger

**Conversation Memory**:
What was said in one conversation, kept so the bot can search it later — and only
from within that same conversation.
_Avoid_: history, context, memory (on its own)

**Receipt**:
The record of what the bot did because of one message: which message, what changed,
and how to undo it.
_Avoid_: log, action (an action belongs to a Rule), audit

### Runtime

**Server**:
The always-on machine that holds the source of truth and runs the core: database,
board, scheduler, Rules, the bot and every model call.
_Avoid_: host, daemon (the daemon is a process on it)

**Satellite**:
A Member's own machine that reports what only it can sense (the microphone) and does
what only it can do (the Lighter, desktop notifications). It queues captures while
the Server is away.
_Avoid_: agent (that is the LLM agent), client (that is the CLI), node

**Tool**:
Something the LLM agent may invoke: a Python function that names the Grant it needs
and whether it is destructive.
_Avoid_: command, skill, action; and do not confuse with Capability

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

**Role** means a Note's role (Task, Reminder), and only that. What a Member is
allowed to do is a **Grant**.

**Agent** means the LLM agent that answers on the Channel. The Member's machine is a
**Satellite**.

**Tool** and **Capability** are different axes: a Tool is something the agent can
invoke; a Capability is a subsystem that is alive or dead on a machine. A Tool can
depend on a Capability (the home Tools need the home Capability).
