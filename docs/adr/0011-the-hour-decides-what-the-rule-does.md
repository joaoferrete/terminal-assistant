# The hour does not decide whether the rule runs; it decides what it does

The original request was "after 16:00, when I join a call, turn the light up and
the ringlight on". The rule was born that way, with the hour in the `when=` of the
entry trigger — before 16:00, nothing happened.

That was wrong in both directions. A call at 10:00 turned nothing on, even though
a light at full helps on a call at any hour. And the light dropped back to a
living level on leaving an 18:00 meeting, returning the room to darkness precisely
when the sun had already gone.

The mistake was treating `16:00` as a switch for the rule. It never was: what
`16:00` actually means is **"it is dark now"**. And getting dark does not change
*whether* you are in a meeting — it changes what is useful to do about it.

So the hour left `when=` and became an internal condition, governing three things:

| Effect | Before 16:00 | After |
|---|---|---|
| Light to 100%, on entry | yes | yes |
| Ringlight | no | yes |
| Light back to living level, on exit | yes | no |

The light is unconditional because it helps on a call at any hour. The ringlight
only comes in after dark: during the day natural light does the job, and a lit
ring is production nobody asked for. And the light only returns to a living level
while it is still early, because after that lowering it means returning the room
to darkness.

## Considered Options

- **Keep `when=after("16:00")` and accept the gap.** That is what existed.
  Rejected because the frequent case — a morning call — is not an exception, it is
  most days.
- **Two entry rules, one for each side of 16:00.** It would be explicit in
  `ta rules check`. Rejected because both would share the 1:1 check, the calendar
  title and the light — the real difference is one line, and two near-identical
  rules diverge at the first maintenance.
- **Store the light's previous level on entry and restore it on exit.** More
  correct on paper: it would return exactly what was there, instead of a fixed
  `40`. Rejected because it introduces state between two firings that can be hours
  apart, separated by a daemon restart, or by somebody having changed the light
  from their phone in between. The stored state would go stale silently, and the
  light would return to a value nobody recognises.
- **Read sunset from Home Assistant's `sun.sun` instead of fixing `16:00`.** That
  is the true signal. Rejected for now because it would tie a decision to Home
  Assistant that today is taken without it — the rule works with Home Assistant
  down, just without the light part — and because a readable constant at the top of
  the file is easier to adjust than one more dependency. It stays as the obvious
  improvement.
- **Turning the ringlight off on exit only if it would have been turned on**,
  repeating the entry condition. Rejected on a concrete case: a meeting that starts
  at 15:50 and ends at 16:10 did not turn the ring on (too early) but *would* turn
  it off by the exit condition — or, inverting the condition, would turn it on and
  never off. An unconditional `enable(False)` has no such gap, and turning off
  something already off costs nothing.

## Consequences

- **The hour is read at the moment of firing**, not at the start of the call. A
  meeting that crosses 16:00 counts as dark on the way out. That is what you want:
  what decides is the window in which the action happens, not the calendar.
- **One helper decides what `16:00` means** (`_is_dark`), read by both uses. It is
  the same `after()` primitive a `when=` would use, so the three places cannot
  disagree.
- **The notification text now depends on the hour.** Announcing "light and
  ringlight" in the morning would be a cheap lie, and that is how people stop
  trusting the notice. It has a test, because it is the kind of detail nobody
  reviews.
- **The 1:1 check now exists on both sides.** Only the entry used to consult the
  calendar; if nothing was turned on, nothing should be undone. This depends on the
  appointment still being in progress — `calendar.now()` only returns an event
  between `start` and `end` — so somebody leaving **after** the scheduled end falls
  through to the common path and has the light adjusted. Erring towards adjusting
  is the cheap side.
- **The rule's name changed** from the Portuguese "late meeting" to just "meeting",
  because it is no longer about the afternoon. The documentation cited the old name
  in two files and nobody noticed — there is now a test comparing the docs against
  the rules that actually load.
- **`16:00` is still a guess.** It is roughly when it gets dark in the author's
  winter, and it holds neither for their summer nor for another latitude. That is
  why it is a named constant at the top of the file.
