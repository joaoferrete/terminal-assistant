# AI

**Everything on this page is optional.** If you never set an API key, the tool
works: capture, the board, the trigger engine, reminders, `ta today`, export,
the whole house integration. None of it calls a model.

That is a design rule, not an accident
([ADR 0003](adr/0003-llm-fora-do-caminho-critico.md)):

- **Capture never waits for a model.** The parser is a regex, offline, and the
  note is saved before anything else happens.
- **Opening the board never calls a model**, and never costs money.
- **Everything a model produces is written to the database.** Nothing is
  recomputed on the fly, so two consecutive looks at the board show the same
  thing.
- **Your hand always wins.** A note you dragged is not reordered; a priority you
  typed is not overwritten.

The clock is the one exception to "nothing reorders itself" — the due-date band
is computed at display time, so the board turns the day over at midnight. The
clock is local, instant, free and deterministic, which is why it gets to do that
and a model does not ([ADR 0010](adr/0010-o-relogio-ordena-o-quadro.md)).

## Turning it on

```bash
# get a key at https://aistudio.google.com/apikey
echo "GEMINI_API_KEY=your-key" >> .env
systemctl --user restart ta

ta doctor          # AI (Gemini): ok
```

## What it adds

| Feature | Command | Calls per use |
|---|---|---|
| Second pass over a capture | *(automatic)* | 1 per note captured |
| Re-tag everything open | `ta revise` | 1 per open note |
| Group and refine order | `ta organize` | 1 |
| The day in prose | `ta prose` | 1 |
| Detect an event from text | `ta event` | 1 |
| The priorities interview | `ta init` | 1 |

Default model is `gemini-flash-latest`, a moving alias — it never goes stale, at
the cost of being able to change behaviour on its own. Pin it with
`TA_GEMINI_MODEL` if you would rather have the reverse trade.

### The second pass

The one that runs without being asked. After a capture is saved and the response
has already gone back to you, a review runs in the background and can:

- fix a deadline the regex got structurally right and semantically wrong;
- classify intent — task, appointment, or just a note;
- add **structural** tags (area, type) *alongside* the themes you typed, never
  instead of them;
- set a priority, if you did not;
- propose a calendar event.

**It never overwrites what you typed.** There is a flag per field recording who
decided it. What you set by hand is locked forever; what the model set is
revisable — which is what lets `ta revise` correct the model's *own* earlier
guesses without touching yours.

A note captured with no network is queued, not lost. Later captures drain the
backlog, and `ta revise` pushes everything open back in.

Turn just this off, keeping the key for the on-demand commands:

```
TA_AUTO_REVIEW=0
```

### `ta organize`

Groups notes by theme and refines the order **inside** each due-date band. It
cannot move things across bands — the clock owns the deadline, and asking a model
for urgency was asking it to compete with a rule that always beats it.

What it actually decides: what unblocks what, and what matters to you given your
priorities. The result is **written**, so the board does not shuffle when you
look at it again.

Notes you dragged keep their relative position.

### `ta init` and priorities

A four-question interview producing a small Markdown profile: what you do, what
cannot slip, what you tend to postpone, what your days look like.

It matters more than it sounds. The profile is what lets *"review the Kafka
consumer"* be routed to your work calendar without the note saying so — the
profile is what knows Kafka is your job.

```bash
ta priorities                                  # show it
ta priorities "put study above chores"         # rewrite by prompt
```

The rewrites are versioned, so the history is auditable.

### Events on the calendar

When a note is clearly an appointment, an event can be created in a **dedicated
calendar** named `Terminal Assistant` — one per account, a separate and
disposable layer.

Two guards are permanent, and they exist to protect **other people**
([ADR 0007](adr/0007-propor-e-confirmar-antes-de-escrever-na-agenda.md)):

1. **Never with guests.** A guest means an email to a real person, and a wrong
   invitation cannot be undone by deleting the event.
2. **Only in the dedicated calendar.** If that calendar does not exist, nothing
   is written anywhere — rather than falling back to your main one.

Personal versus work routing uses the **domain** of each connected account. Only
the domain goes to the model, never a full address.

## What is sent, and what is not

Sent when a feature runs: the note text, your priorities profile, and the
**domain** of your calendar accounts.

Never sent: your Home Assistant token, your API keys, full email addresses, or
anything at all when the features are off.

This is a third-party API. Whatever you type into a note that gets reviewed goes
to Google. If that is not acceptable for some of your notes, `TA_AUTO_REVIEW=0`
makes every model call something you ask for explicitly.

## When it fails

It fails **loudly and locally**. No key, no SDK, no network, or a response that
does not match the requested schema — each one produces a message saying what to
do, and nothing else in the tool is affected.

```bash
ta doctor          # says whether the key is configured
```

An AI call that fails never takes a note with it. The note was saved before the
call started; that is the whole point of the ordering.
