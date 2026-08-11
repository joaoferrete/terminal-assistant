# Loopback by default, and a token to leave it

The daemon listened on `0.0.0.0` from day one, with a `# noqa: S104` in the code
saying "LAN on purpose: the board opens on my phone". It was a conscious choice,
and for one machine on a home network it is defensible.

It exposes 29 routes and has **no authentication at all**. Anyone on the same
network, with no credential, can:

- read every personal note (`GET /notes`);
- delete them (`/rm`, `/purge`);
- turn the house's lights on and off, using the owner's Home Assistant token;
- discover the Home Assistant URL and which secrets are configured (`/health`);
- spend the model API key, one call per request.

Publishing that with a step-by-step guide telling strangers to run
`make install-service` transfers that risk to people who did not ask for it, on
networks that are not their living room: a café, a coworking space, a university,
a building's shared wifi.

So the default becomes `127.0.0.1`, and opening to the network requires **two**
deliberate acts: `TA_HOST` and `TA_TOKEN`. Without a token the daemon **refuses to
start** on a non-loopback address, with the fix in the message.

The middleware only exists when there is a token, and **anyone arriving from
loopback passes without a credential**. That is not laxity: whoever is already on
the machine has the `.env`, and requiring a token from them would make `ta note`
carry a secret without buying any security. What the middleware covers is the
network.

## Considered Options

- **Keep `0.0.0.0` and document it in SECURITY.md.** Rejected on the asymmetry of
  who reads what: whoever opens the security document already worries; whoever
  follows the step-by-step is exactly the person who does not know they should.
- **Loopback by default, with no token at all.** Much cheaper — no change to the
  board. Rejected because the phone is half the point of the board, so almost
  everyone will open it, which returns to the previous state with one extra step.
- **A token always, including on loopback.** More uniform, no special case in the
  code. Rejected for getting in the way of legitimate local use: `ta note` would
  have to carry a credential, and "install it and use it" would stop being
  immediate.
- **Real authentication — users, passwords, sessions.** Rejected as
  disproportionate: this is a single-machine tool, and a shared secret solves the
  entire threat model (the local network) without bringing account management.
- **HTTPS.** It solves nothing here and costs a lot: a certificate for a LAN IP is
  self-signed, the phone's browser complains, and the attacker in the threat model
  is on the same network — they need the credential, not to intercept traffic. If
  the daemon ever leaves the LAN that changes, and then the answer is a tunnel, not
  homemade TLS.

## Consequences

- **The board on a phone now takes one extra step.** The address becomes
  `http://<ip>:7777/board?token=…`. The token goes in once, moves to
  `localStorage`, and is **stripped from the address bar immediately** — otherwise
  it lives in the browser history and in every pasted link.
- **The author's existing installation breaks**, and it is the only point in this
  whole piece of work with a human decision in it: the service was running open.
  `ta doctor` detects it, generates the token and prints the two lines for the
  `.env`.
- **`create_app` can now fail.** The check lives there rather than in `main()`, so
  it also applies to anyone assembling the app themselves. That makes the failure
  visible in tests, which is where it should appear.
- **Constant-time token comparison** (`hmac.compare_digest`). Comparing a secret
  with `==` leaks the correct prefix through response timing. It is cheap to do
  right and expensive to discover later.
- **`::ffff:127.0.0.1` counts as local.** An IPv6 socket accepting IPv4 reports the
  peer in that form, and without handling it the local CLI would start needing a
  token depending on the socket family — a failure that would only appear on
  somebody else's machine.
- **`/health` still reports the presence of a secret, never its value.** The
  discipline is old and now has a test.
