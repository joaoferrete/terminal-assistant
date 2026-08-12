# The clock orders the board, and priority orders within the band

Display order used to be priority first, deadline as tiebreak. The effect was a
board that did not answer the only question anyone asks it in the morning: a
`!high` with three weeks of slack sat above a `!medium` due today, because the
declared axis beat the real one.

Enter the **Horizon**: the band of time a deadline falls into, counted from today
— `overdue`, `today`, `week` (7 days, a rolling window) or `later`. It is the
first criterion, ahead of priority. Priority did not lose value, it changed
scope: it orders **within** the band.

Two consequences of this are decisions, not implementation details:

**The Horizon is derived at display time, never stored.** It is a function of the
clock — the same Note with deadline `2026-08-20` is `later` today and `today` on
the 20th — and nothing in `sort_key` can represent that.

**And it is derived on the server.** `GET /notes` returns Notes already in display
order, each with its band. The board does no date arithmetic and no reordering:
the three views, `ta list` and `ta export` all receive the same order without each
reimplementing it. That is what deleted the third copy of the priority rank table
— there was one in `store.py`, one in `cli.py` and one in `board.html`.

## Considered Options

- **A continuous urgency score**, combining days-until-deadline with priority into
  a single number. It orders well and explains nothing: there is no divider to
  draw, the user cannot predict where a Note will land, and a test over it pins a
  number instead of a sentence. Rejected for being unauditable, not for ordering
  badly.
- **Letting priority promote a Note one band**, so a `!high` due tomorrow could tie
  with today's. Rejected for a visual reason: the board draws dividers per band,
  and that rule would put a post-it under a divider it does not belong to. An order
  the screen itself contradicts is worse than a coarser order.
- **`undated` as a band of its own, at the end.** More honest about what has no
  date, and rejected because it sinks: a `!high` task with no deadline would sit
  below a `!low` due in September. What has no date marked is not, for that reason,
  less important than the distant future — so `undated` lives in `later`, and the
  divider's label admits it: **"later and undated"**.
- **A nightly job baking the band into `sort_key`**, to keep order as stored data.
  Rejected twice over: it would fight `pinned_by_user`, rewriting at night exactly
  what the user dragged during the day, and it would destroy the guarantee ADR 0003
  sells — nothing is overwritten in the database because of the passage of time.
- **Using `remind_at` as the deadline when there is no `due`.** Tempting: a
  reminder at 22:00 today is clearly "today". Rejected because it changes what
  `overdue` means — a Reminder is not late, it has **already fired** — and that
  would drag `fired_at` into the ordering just to stop consumed reminders from
  living in `overdue` forever.

## Consequences

- **The board reorders itself at midnight.** That is the point, and it contradicts
  a consequence written in ADR 0003 — see the amendment there.
- **`organize` became a refiner within the band.** What the model writes to
  `sort_key` is the last tiebreak in the key, so the order of the groups it
  returns only shows between Notes that already tied on band, priority and
  deadline. The prompt was rewritten to say so: the model groups by theme and
  orders by what unblocks what — the clock owns the deadline. The thematic
  grouping survives in the `group` field, which the board does not draw yet.
- **The default view shows little of this.** It uses the stored position; the order
  only appears in the fallback grid, for a Note never dragged. Correct per ADR 0003
  — the hand wins — but whoever arranged the board by hand has to look at the
  **list** view to see the change.
- **A `!high` with no deadline falls below every `!low` due this week.** That is
  the other side of the coin of putting `undated` with `later`, and it is inherent
  to "the deadline dominates".
- **`ta export` reorders from one day to the next**, because it orders against
  `date.today()`. Acceptable in a dump; if the export is ever versioned, grouping
  under band headings solves it.
