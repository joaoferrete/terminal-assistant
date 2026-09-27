# Members: private by default, and every action runs as its author

The bot is shared by the people who live in the house. Each person is a **Member**;
the one who administers the installation is the **Owner**. A Note is private to the
Member who wrote it. Household things, such as the shopping List, are born shared.
What a Member may do — which Entities, Lists and Tools — is a **Grant**: a named
set of permissions assigned in the config, closed by default.

The rule that makes this safe with a bot that reads a whole group and acts on what
it reads: **every action runs with the Grant of the message's author, never with
the bot's.** If a sentence in the group is misread as "turn off the bedroom light",
it reaches only as far as its author could.

Identity is the Channel's numeric user id. The config invites people by username
for convenience; the username is bound to the id on first contact and never
consulted again, because a released username can be claimed by a stranger.

## Considered Options

- **Everything visible to everyone, author marked.** Simpler. Rejected: work and
  personal Notes would be on display to the people one lives with.
- **Fully separate spaces.** Rejected: the shopping List would become one list per
  person.
- **Everyone commands everything.** Rejected: the house has personal Entities, and
  proactive capture makes a misread sentence an action.

## Consequences

- Every read path must filter by visibility. The test that covers them does so
  together, as the `deleted_at` test does, because a single forgotten path leaks.
- A reply in a group never contains private data, even when the private data would
  answer the question.
- "Role" already means a Note's role (Task, Reminder), which is why the set of
  permissions is called a Grant.
