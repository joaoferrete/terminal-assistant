# One language at a time, not both at once

The tool needed to speak English as well as Portuguese: the repository was going
public, and the interface, the messages and the capture parser were all Portuguese
only.

The obvious route would be accepting both vocabularies simultaneously — `@sexta`
and `@friday`, `!alta` and `!high`, all always valid. It looks more generous, and
it works for almost everything.

Almost.

> **`@03/04` is 3 April in Portuguese and 4 March in the American convention.**

With both languages active that token is genuinely ambiguous, and whichever side
is chosen is **silently wrong** for half the users. No error, no missing mark,
nothing removed from the text: it returns a **valid and wrong** date, and the
person misses the deadline without ever knowing why.

This project treats that failure mode as a category, not an isolated case — the
`@25/12` that was silently discarded, the `home.light()` that answered 200 and did
nothing, the priority that vanished during review. All of them cost
disproportionate time precisely because they did not error.

And the LLM's second pass cannot be relied on to fix it: the AI is optional by
explicit decision (ADR 0003). The regex is the floor.

So there is **one active language at a time**, resolved by `TA_LANG`, and the
numeric date order is a consequence of it.

## Precedence

    TA_LANG  →  ~/.config/ta/config.toml  →  the system locale  →  'en'

The locale comes before the default on purpose: on a `pt_BR.UTF-8` machine nothing
changes for someone already using the tool, with no configuration at all. That is
also what removes the need to ask on first run — and the question **could not**
have lived in `ta init`, which is the Priorities interview and requires an LLM.

## Considered Options

- **Both vocabularies together, with `DD/MM` always winning.** Nothing breaks for
  existing users and the code is simpler. Rejected for knowingly accepting that an
  American user will mark the wrong date without noticing.
- **Both together, refusing the ambiguous numeric date** (when both numbers are
  ≤ 12, the mark returns to the text, as `@casa` already does). Elegant, honest,
  and it never errs silently. Rejected because it would break `@03/04` for existing
  users — and half of all short dates fall in that range.
- **Detecting each note's language from its text.** Smarter on paper. Rejected
  because detection on short text ("meeting 03/04") is unreliable, and it fails
  silently: the same defect, with more code to maintain it.
- **ISO only, and be done.** `@2026-04-03` is never ambiguous. Rejected because the
  short syntax is half the value of fast capture.

## Consequences

- **The language belongs to the installation, not to the invocation.**
  `TA_LANG=en ta note "..."` does not change how a note is read: the CLI is a thin
  client and the daemon does the parsing, having resolved the language at boot.
  That is the correct behaviour, not a limitation — if it varied per call, two
  notes captured on the same day would read `@03/04` as different dates and the
  database would store them as if they were the same thing. `ta lang <pt|en>`
  writes it and says it needs a restart.
- **Relative words work in both languages always.** `@today` on a Portuguese
  machine is unambiguous, and refusing it would be pedantry. What cannot work in
  both is the **numeric date**, and only that.
- **`!alta` and `!high` are both accepted**, because priority is a closed enum
  with no possible collision. The language of input is not the language of storage
  (amendment to ADR 0006).
- **The date forms are structurally different, not translated.** Portuguese says
  "17 de outubro" and "na sexta"; English says "October 17", "17 October",
  "October 17th" and "next Friday". Each language has its own set of regexes,
  compiled once.
- **AM/PM only exists in English, and forced a new rule.** Before, `8pm` matched
  **nothing** — the lookahead failed on the `p` and the mark vanished silently. To
  accept `8pm`, the minutes suffix had to become optional, and that made the day of
  the month be re-read as an hour: "dentist on October 17" gained a reminder at
  17:00. The guard — a bare number is never a time — is now explicit.
- **The retrospective verb list has to exist per language.** "hoje aprendi X" and
  "today I learned X" are records, not tasks, and the regex gets the form right and
  the intention wrong without that curation.
- **The message catalogue is injected into the board's HTML**, not fetched by a
  route: the language does not change during the life of the page, and a second
  request would only add latency and a moment of drawing raw keys. The JS has no
  parallel table — the same discipline as `HORIZON_LABEL`.
- **A test guarantees both languages have exactly the same keys.** Without it, a
  key present only in `pt` becomes wrong text on the machine of somebody using
  `en`, and only there.
- **The language's `lru_cache` became a source of coupling between tests.** A test
  that pinned `TA_LANG=en` left the cache that way for the following ones, and
  thirteen Portuguese parser tests started failing with nothing related to them
  having changed. An autouse `conftest` clears the caches on every pass.
- **The prompt bodies stay in Portuguese.** What changes with `TA_LANG` is the
  language the model **answers** in, through a system instruction in one place.
  Rewriting the prompts would change the model's behaviour with no way to compare
  before and after without burning calls — and that is not what the decision needs.
