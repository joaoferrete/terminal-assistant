# The calendar through GNOME Online Accounts, not our own OAuth

The app needs to read and write two Google calendars, one personal and one for
work. The accounts are connected in Settings → Online Accounts, and the app talks
over DBus to Evolution Data Server, which GNOME already keeps in sync.

Google's Calendar scope is sensitive, and an OAuth client of our own in
"Testing" publishing status issues refresh tokens that expire every seven days —
re-authenticating two accounts forever is not viable for a daemon. GNOME's OAuth
client is already verified, and Online Accounts renews the token itself.

## Considered Options

- **Our own OAuth against the Google Calendar API.** A clean, well-documented
  REST API, but it carries the seven-day expiry described above; escaping it
  requires submitting the app for Google verification. There is also the risk of
  the work account's Workspace admin blocking an unverified third-party app.
- **Read-only, over an ICS feed**, leaving event creation to a link the user
  clicks. Rejected: it gives up an explicit requirement.

## Consequences

- The Evolution Data Server API is more work than REST, and it is the reason the
  project's language ended up pinned (see ADR 0005).
- Reading the calendar becomes local and instant, because Evolution syncs in the
  background. That is what makes it viable to use the calendar as a Rule
  condition without paying network latency.
- The app depends on the user having connected the accounts in GNOME. Without
  that there is no calendar, and the failure has to be stated clearly rather than
  looking like a bug.
