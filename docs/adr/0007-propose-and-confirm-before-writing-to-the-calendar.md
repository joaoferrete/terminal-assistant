# Propose and confirm before writing to the calendar

When the LLM concludes that a Note describes an event, the app shows the title,
date, time and destination calendar, and creates nothing without a yes. Even once
confirmed, the event is written to a **dedicated calendar** — its own colourable,
disposable layer — and the app **never adds guests**, so it never notifies
anybody.

This is deliberately less fluid than it could be, and someone reading the code
will want to "fix" it by making creation automatic. The failure mode justifies the
friction: a wrong date guess written straight into a work calendar becomes a ghost
appointment that colleagues can see, and that you only find out about when
somebody asks what that meeting is.

## Amendment, 2026-08-10: there are two dedicated calendars, and the LLM routes

There is a `Terminal Assistant` calendar in **each** account — personal and work.
A detected event is routed to whichever matches the note's context, and that
choice is the model's.

This looks like it contradicts the rejected "automatic on personal, confirmation
on work" option, but it does not: there, routing decided between a dedicated layer
and a **real** calendar, so a routing error broke the protection. Here both
destinations are dedicated, disposable layers, so getting the routing wrong puts
the event in the wrong layer — never into an appointment colleagues can see. The
guard is intact, and the gain is keeping work context next to work.

Confirmation remains mandatory, and the ban on guests remains absolute.

## Consequences

- If anything slips through, deleting the dedicated layer cleans it all up
  without touching real appointments.
- Never adding guests is a project invariant, not a setting — it should never get
  a flag that turns it off.
- The structured output schema for event detection gains a target-account field,
  and it is validated against the calendars that actually exist — never accepted
  as free text.

## Amendment, 2026-08-10: confirmation waived, by the user's decision

The user asked that a note like "nutritionist on 17 September at 8:30" become an
appointment **on its own**, and explicitly chose to let the model create the event
without confirmation, after the risk was laid out: a ghost event in a work
calendar shows you as busy to colleagues.

**Confirmation goes. The other two guards stay**, because they protect other
people rather than the user:

1. **Never guests.** A guest sends email to a real person, and a wrong invitation
   is not undone by deleting the event.
2. **Only in the dedicated `Terminal Assistant` calendar**, one per account. That
   is what keeps all the damage deletable in one go, and it is why the automatic
   creation path **writes nowhere at all** when that calendar does not exist,
   rather than falling back to the main one.

Two new containments, which the manual flow did not need:

- **A confidence floor of 0.7.** Below it the review does nothing. The model is
  instructed to return low confidence when unsure, and a wrong correction is worse
  than none.
- **Every autonomous act notifies.** If the app touched a deadline or wrote to the
  calendar, it appears on screen immediately. Invisible autonomy is worse than no
  autonomy: without the notice, the user loses the chance to undo it while they
  still remember the context.

`TA_AUTO_REVIEW=0` turns the whole second pass off, and capture goes back to being
just the regex. The `/calendar/event` route **still requires `confirmed: true`**:
the manual path was not loosened; what changed is that there is now an automatic
one beside it.
