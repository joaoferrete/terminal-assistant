# Security

Terminal Assistant runs on your own machine and holds two things worth
protecting: **everything you have ever written down**, and **a credential that
controls your house**. This page explains what it exposes, what it deliberately
does not, and how to tell us when we got it wrong.

For how to actually set any of this, see [`.env.example`](.env.example) — this
page is the reasoning, not the reference.

## What the daemon is

A single local process listening on **`127.0.0.1:7777`**. The CLI talks to it over
HTTP, and the board is a web page it serves. There is no cloud component, no
telemetry, and no account.

Your notes live in a SQLite file at `~/.local/share/ta/ta.db` (or
`$XDG_DATA_HOME/ta/ta.db`), owned by you, with no encryption at rest. Anyone who
can read your home directory can read your notes — the same as any other local
file. Full-disk encryption is the right tool for that, and it belongs to your
operating system, not to us.

## The default is loopback, on purpose

The HTTP API has **no authentication of its own**. Bound to loopback that is
fine: only processes on your machine can reach it.

Bound to a network address it is not fine at all. Anyone on the same Wi-Fi could
read every note, delete them, switch your lights, and spend your model API key —
no credential required. So the daemon **refuses to start** on a non-loopback
address unless `TA_TOKEN` is set, and tells you how to fix it.

When a token is set, requests from other machines must present it, as
`Authorization: Bearer <token>` or `?token=<token>`. **Requests from loopback are
still allowed without one** — you already own the machine and the `.env` file, so
requiring a secret there would add friction without adding safety.

If you open the board on your phone, the token travels once in the URL, is stored
in `localStorage`, and is stripped from the address bar immediately so it does not
end up in your browser history or in any link you paste.

### If you open it to your network, know this

- Traffic is **plain HTTP**. Someone who can already watch your local network can
  read your notes in transit. We do not ship TLS because a self-signed
  certificate for a LAN IP trains you to click through browser warnings, which is
  worse. If you need this outside a network you trust, put it behind a VPN or an
  SSH tunnel rather than exposing the port.
- **Never port-forward `7777` to the internet.** A shared secret over plain HTTP
  is not built for that, and nothing in this project is hardened for it.

## Secrets

Secrets come from the environment only, never from a versioned file. The systemd
unit reads them from a `.env` that is in `.gitignore`.

`GET /health` reports **whether** a secret is configured, never its value, and
there is a test that keeps it that way. Log output follows the same rule.

**The Home Assistant token is the most dangerous thing here.** It is a long-lived
credential that can control every device Home Assistant knows about — not only
the ones this project uses. Scope it as narrowly as your setup allows, and rotate
it if it ever leaves your machine.

## What is intentionally not protected

Being explicit so you can disagree with us knowingly:

- **Rules are arbitrary Python**, loaded from your rules directory and executed by
  the daemon with your privileges. That is the whole design
  ([ADR 0002](docs/adr/0002-motor-de-automacao-proprio-em-python.md)) — it is
  code you wrote, not a sandbox. Do not run a rule file you did not read.
- **The local user is trusted.** Any process running as you can reach the daemon.
- **Note text is sent to a model provider** when AI features are enabled — the
  second pass over a capture, `organize`, `prose`, and event detection. This is
  optional and off without an API key, and no feature that matters depends on it
  ([ADR 0003](docs/adr/0003-llm-fora-do-caminho-critico.md)). Only the **domain**
  of a calendar account is ever sent, never a full address.

## Reporting a vulnerability

Please use **GitHub's private vulnerability reporting** — the *Security* tab of
this repository, "Report a vulnerability". It reaches the maintainer without the
report being public first.

Please do not open a public issue for anything exploitable.

This is a personal project maintained in spare time, so no response time is
promised. What is promised: you will get a real answer, credit if you want it,
and an honest note in the fix explaining what went wrong. If a report turns out
to be a design decision rather than a bug, it gets written down here rather than
argued away.

## Supported versions

The `main` branch is the only supported version. There are no releases yet and no
backports.
