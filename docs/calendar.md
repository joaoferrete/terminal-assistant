# Calendar

**Optional.** Without it, `ta today` still lists your chaseable tasks, and rules
that ask for the current event just get nothing back.

The design choice: this project **never talks to a calendar provider**. GNOME's
Online Accounts already syncs your calendars into Evolution Data Server, handles
OAuth and renews tokens. This reads what is already on disk
([ADR 0004](adr/0004-calendar-through-gnome-online-accounts.md)).

That means: no OAuth app to register, no refresh token to babysit, no credential
stored by this project — and reads that are local and instant. The cost is that
it only works on a Linux desktop with GNOME, which is a trade this project is
happy with.

## Setting it up

### 1. The typelibs

```bash
sudo apt install gir1.2-ecal-2.0 gir1.2-edataserver-1.2
```

These are the introspection bindings that let Python speak to Evolution. They are
not installed by default.

### 2. The virtualenv has to see them

PyGObject lives in the system Python, installed by `apt`, outside your venv. The
venv only sees it if it was created with `--system-site-packages`:

```bash
/usr/bin/python3 -m venv --system-site-packages .venv
```

`make install` does this for you. If you created the venv another way, delete it
and rerun — this is the failure mode
[ADR 0005](adr/0005-the-system-python-because-of-pygobject.md) exists to warn
about, because a `python3 -m venv` typed from reflex produces an environment
where `import gi` fails with no obvious explanation.

```bash
make check-gi
```

That prints `ECal OK` and `EDataServer OK`, or the exact `apt` command for
whatever is missing.

### 3. Connect your accounts

**Settings → Online Accounts.** Add whichever accounts you want. GNOME does the
rest.

Open the Evolution calendar once afterwards and let it sync, otherwise there is
nothing on disk to read yet.

### 4. Optional: the dedicated calendar

If you want detected events to be written anywhere, create a calendar named
exactly `Terminal Assistant` in your provider — one per account you want to write
to.

If it does not exist, nothing is written **anywhere**. That is deliberate: a
guessed event landing in your real work calendar shows you as busy to your
colleagues, and falling back to the main calendar would be the wrong kind of
helpful ([ADR 0007](adr/0007-propose-and-confirm-before-writing-to-the-calendar.md)).

### Did it work?

```bash
ta doctor          # Calendar: ok
ta today
```

## Using it

```bash
ta today                        # events + chaseable tasks + weather
ta today --date 2026-08-15      # some other day
```

And from a rule, where it is **context rather than a trigger**
([ADR 0008](adr/0008-the-microphone-is-the-meeting-trigger.md)):

```python
event = await ctx.calendar.now()        # the event happening right now, or None
events = await ctx.calendar.today()     # today's list
```

The Portuguese names `agora()` and `hoje()` are the same methods and keep working
for good — a rule you already wrote does not break because the project changed
language. New rules should use the English ones.

The distinction matters. The microphone tells you that you are *in a call*; the
calendar tells you *which* call, and only when something asks. A calendar-only
trigger is wrong in both directions — it fires for meetings that did not happen,
and is blind to ad-hoc calls.

`now()` returns an event only between its start and end. So a rule that runs
after the scheduled end gets `None`, and should handle that rather than assuming.

## Personal versus work

If you connect more than one account, notes get routed to the right one.

The signal is the **account domain**, not the calendar name. A well-known
consumer provider means personal; your own domain means work. Only the domain is
ever sent to a model, never a full address.

This one took a wrong turn worth recording: the first attempt compared a `parent`
field, which is an opaque GNOME hash. The comparison was always false, so *every*
event went to the same account regardless of what the model decided. Classic
silent failure — the event appeared, just in the wrong place.

## Performance, and why the first call is slow

The first `ta today` after a restart takes a few seconds and tells you it is
warming up.

Each cloud calendar source pays a fixed connection timeout the first time it is
contacted — and a freshly created calendar pays the whole thing, because
Evolution has no cache for it yet. With several accounts connected, in series,
that adds up to a minute.

So: sources are connected **in parallel** at boot, in the background, and cached.
The cost becomes the slowest single source rather than the sum, and every
subsequent read is milliseconds.

`ta today` waits for the warm-up rather than failing on a timeout, and says it is
waiting. An optimisation that turned a slow command into a *failing* command
would be worse than the original slowness.

## When it does not work

See [troubleshooting.md](troubleshooting.md#the-calendar) — it covers `import gi`
failing, the D-Bus error in headless sessions, an empty `ta today`, recurring
events showing the wrong date, and duplicate events.

## On a server: Google Calendar

A server has no GNOME session, so there the calendar is Google's API, with an
OAuth client of your own ([ADR 0004](adr/0004-calendar-through-gnome-online-accounts.md),
amendment). Each Member connects their own accounts, from the chat.

Setting up the OAuth client takes about ten minutes in the
[Google Cloud Console](https://console.cloud.google.com/), once per household.
Google moves its menus around. If a name below does not match, the console's
search bar finds each page by the name in bold.

**A. The project and the APIs**

1. At the top of the console, pick your project, or create one (**New project**;
   the name is only for you).
2. Open **APIs & Services → Library**. Search for **Google Calendar API**, open
   it, and press **Enable**. For email too, do the same for the **Gmail API**.

**B. The consent screen** (**Google Auth Platform**; older consoles call it
**OAuth consent screen**)

3. If it offers **Get started**, press it and fill in:
   - **App information**: any app name (people see it on the consent screen, e.g.
     "Casa"), and your email as the support email.
   - **Audience**: **External**. *Internal* exists only for Workspace
     organisations, and it would keep your personal Gmail out.
   - **Contact information**: your email.
   - Accept the policy, and press **Create**.
4. **Data Access → Add or remove scopes.** Paste these into *Manually add scopes*,
   press **Add to table**, then **Update**, then **Save**:
   ```
   https://www.googleapis.com/auth/calendar.events
   https://www.googleapis.com/auth/calendar.calendarlist.readonly
   https://www.googleapis.com/auth/gmail.readonly
   ```
   Leave out the last one if you do not want email.
5. **Audience → Publish app**, and confirm. The status must say **In production**.
   In *Testing*, Google expires the connection every seven days and the calendar
   silently disconnects once a week. Google does not review a household app. The
   consent screen shows "Google hasn't verified this app", and you go on through
   **Advanced → Go to (app name)**.

**C. The client**

6. **Clients → Create client.** Pick **Desktop app** as the type (not *Web
   application*: the paste-back flow needs Desktop's `localhost` redirect), give
   it any name, and press **Create**.
7. Copy the **Client ID** and the **Client secret** at once, or press **Download
   JSON**. Newer consoles show the secret **only now**. If you lose it, add a new
   secret to the same client.
8. Put both in `.env`, never in a chat:
   ```
   GOOGLE_CLIENT_ID=1234-abc.apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=GOCSPX-...
   ```
   On a server, edit its `.env`, or use the config page's secrets, which are
   write-only. Then restart the daemon.

**D. In Google Calendar**

9. Create a calendar named exactly **Terminal Assistant**: **Other calendars → +
   → Create new calendar**. The bot creates its events there. Creating a
   calendar would need the scope that can also delete calendars, so the bot does
   not ask for it.

**E. In the chat**

10. In the chat, send `/conectar_agenda`. Open the link, allow access, and when the
    browser shows a page error at the end — that is expected, nothing listens on
    `localhost` — copy the address bar and paste it back into the chat.

The bot asks for two scopes only: reading and creating events, and listing your
calendars. It never asks for the scope that can delete a calendar. Events it
creates go to a calendar named `Terminal Assistant` in that account, and never
have guests. Tokens are stored one file per account, readable by the daemon's
user only.

A **work account** may refuse: a Google Workspace admin can block unverified
third-party apps. If the consent screen says so, that account stays out, and the
personal one still works.

### Email, with the same client

The same OAuth client also lets the bot **read** your Gmail when you ask ("tem
e-mail do banco essa semana?"). It never reads on its own, never sends, deletes or
marks anything as read, and keeps nothing but the connection.

1. Enable the **Gmail API** (step 2 above), and add the `gmail.readonly` scope
   (step 4). If the calendar was already connected, nothing else changes.
2. In the chat, send `/conectar_email`, and do the same paste-back as for the
   calendar.

`gmail.readonly` is a *restricted* scope. An unverified app gets a stronger
warning than for the calendar, and a work account is even more likely to refuse
it. Try your personal account first. Mail works only in a private chat. Each
person reads only their own mailbox, and what is in an email can never make the
bot act without you confirming.
