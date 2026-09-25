# The LLM is never on the critical path

Dumping a thought cannot wait for the network; organising the board can. Capturing
a Note is always deterministic, local and instant: a light syntax resolves tag,
priority and deadline. The model enters only on an explicit command, and what it
produces — grouping and order — is **stored** as data. Opening the board
afterwards never calls it.

## Consequences

- With no internet, or no API key, the tool still captures and answers. Only the
  luxury of reorganising is unavailable.
- The order the user set by hand survives: because it is stored data rather than
  a computation repeated on every open, a later `organize` does not undo what was
  dragged unless asked.
- Two consecutive looks at the board show the same thing. Nothing reorganises
  itself, and nothing costs money for being looked at.
  ⚠️ **The first sentence no longer holds** — see the amendment below.

## Amendment, 2026-08-11: the clock may reorganise; the model may not

The third consequence above said two consecutive looks at the board show the same
thing. ADR 0010 breaks that on purpose: order now starts with the Horizon, the
time band a deadline falls into, and that band is derived from the clock at
display time. At midnight, a Note that was due tomorrow becomes due today and
moves up — with nobody asking.

The sentence was too broad. What this ADR decides, and what still stands, is that
**the LLM** and **the network** stay off the critical path. The clock is neither:
it is local, instant, free and deterministic given the day. And a board where
today's task does not rise on today is a board that lies — denying that to
preserve the sentence would be preserving the letter and losing the reason.

What survives intact, and is the part that mattered:

- Opening the board **never** calls the model, and never costs money.
- With no network the order is still correct: the horizon is computed on the
  machine.
- **Nothing is overwritten in the database** by the passage of time. `sort_key`
  only changes when you drag or ask for `organize` — which is exactly why the
  band is derived rather than stored.
- Dragging still beats the model, through the same `pinned_by_user`.

## Amendment, 2026-09-25: a conversation has a model in it; capture still does not

V2 adds a bot you talk to, and understanding "turn off the light" versus "I should
cache the fleet endpoint" takes a model. So the model now sits on the path of a
*conversation*. What this ADR protects is kept by a stronger invariant instead of the
old sentence: **every message addressed to the bot is either handled or becomes a
Note.** When the model is unavailable, the message is captured deterministically,
exactly as `ta note` would have done. See [ADR 0017](0017-the-bot-handles-or-captures.md).

Capture from a Satellite still never waits: with the server away, it queues locally
([ADR 0015](0015-a-server-and-its-satellites.md)). And opening the board still never
calls a model.
