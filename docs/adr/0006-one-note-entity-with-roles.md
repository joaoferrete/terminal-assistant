# A single Note entity, with roles given by attributes

The notes module exists to be a place where you write without ceremony, so
capture never asks what kind of thing you are writing. There is one entity,
`Note`, and roles emerge from the presence of attributes: with a deadline it is a
Task, with a firing moment it is a Reminder, with neither it is just text that
stays. The parser and — on command — the LLM promote the Note afterwards, never
while you are writing.

## Considered Options

- **Task, Reminder and Note as separate entities**, each with its own lifecycle.
  It is the more correct model — "completed" makes no sense on a loose idea, and
  a Reminder's firing is not an optional field hanging off something else.
  Rejected because it forces choosing a type at capture, or letting the model
  choose and get it wrong, and both fight the reason the module exists.
- **One entity with no vocabulary of roles**, where everything is called a note.
  Rejected: the Digest would have no hierarchy, because nothing would distinguish
  what is chaseable from what is just text.

## Amendment, 2026-08-10: Status is an axis apart from the roles

The kanban view asked for five states — to do, doing, on hold, done, cancelled —
and `done_at IS NOT NULL` could only answer yes or no. `cancelled` is the case
that proves the gap: a cancelled Note left the queue **without having been done**,
and the binary model had no way to say that.

The Note gained a `status` column with that enum. This does **not** replace the
attribute-derived roles: `due` still makes it a Task and `remind_at` a Reminder,
and a Note can perfectly well be a Task and be on `hold`. They are two orthogonal
axes, and the decision not to have separate entities per type is untouched.

`done_at` was not replaced either: it is still the **instant** the Note entered
`done`. State and timestamp are different things, and cancelling never writes
`done_at`.

## Consequences

- One schema, one read surface.
- The interface and the CLI still speak precisely — "2 tasks are due today", not
  "2 notes" — because the vocabulary of roles exists in the glossary even without
  existing as a type in the database.
- Strange combinations are representable (a completed Note with no deadline, for
  instance). It is up to the interface not to offer them, since the model does not
  prevent them.

## Amendment, 2026-08-11: the stored value is canonical; language is input and display

Priority was the **only Portuguese enum** in the schema (`alta|media|baixa`),
living next to a `status` that had always been English
(`todo|doing|hold|done|cancelled`). They coexisted fine as long as nothing asked
the model for anything in English.

Taking `TA_LANG` to the LLM turns that into a real bug. Told to answer in
English, the model returns `"high"`; the review's validation rejects the value for
not being in the enum; and the priority disappears **silently** — no error, no
log. The note comes back from review with no priority and nobody understands why.

The rule that resolves it, and that now applies to every structured field:

> **The stored value is canonical and singular. Language is a matter of input and
> display.**

In practice:

- The database stores `high|medium|low`, aligned with `status`. Whoever opens the
  SQLite file reads one language.
- `!alta`, `!media` and `!baixa` remain accepted at capture **forever**. They were
  the syntax for the whole life of the project, they are in the muscle memory of
  the people using it, and breaking them would buy nothing. `!high` works too.
- The screen and the CLI show it in the user's language.
- What the model returns in a **structured** field is canonical, regardless of the
  language it was told to write prose in.

Migration 6 rewrote the existing values. It is the project's first migration that
changes **data** rather than adding structure, and because of that `connect()` now
copies the database before applying any pending migration: a migration is atomic,
but atomic is not reversible, and the file contains notes a person wrote.

This is not a precedent for translating the rest by taste. It holds because there
was a concrete, silent failure, and an input path that preserves what people were
already typing.
