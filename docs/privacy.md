# Privacy policy

*Terminal Assistant (`ta`) is self-hosted software. There is no company and no
central service behind it. Each installation runs on its own household's server,
and whoever runs that server is responsible for it. This page says what the
software does with your data, so that an installation's OAuth consent screen has
something true to point to.*

## What it accesses, and when

When a member of the household connects a Google account from the chat:

- **Google Calendar** (`calendar.events`, `calendar.calendarlist.readonly`): reads
  that member's events for their daily summary and answers, and creates events in
  a calendar named "Terminal Assistant". It never adds guests.
- **Gmail** (`gmail.readonly`, `gmail.compose`): only when that member asks about
  their email. It searches and reads messages to answer them, and writes **drafts**
  when they ask for one. It never sends an email. The member reviews and sends
  every draft from Gmail themselves.

## What is kept

- One refresh token per connected account, in a file on the household's server
  that only the server's own user can read.
- **No email is stored.** Messages are fetched when a member asks and discarded
  after the answer. Calendar events are not copied either, except for the link
  between a note and the event created from it.

## Who sees it

- Only the member who connected the account, and only in a private chat with the
  bot. Other members of the same household cannot read it, and it is never shown
  in a group.
- To write an answer, the part of an email or event the answer needs is sent to
  the language model the installation is configured with (for example DeepSeek or
  Google Gemini), under that provider's own terms. Nothing is sent to anyone else,
  sold, or used for advertising.

## Stopping it

Remove the app from your Google account at
<https://myaccount.google.com/permissions>. The token stops working at once. The
server's owner can also delete the token file.

## Google API Services User Data Policy

The use of information received from Google APIs follows the
[Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
including the Limited Use requirements.

## Contact

Whoever runs your installation. For the software itself, open an issue at
<https://github.com/joaoferrete/terminal-assistant/issues>.
