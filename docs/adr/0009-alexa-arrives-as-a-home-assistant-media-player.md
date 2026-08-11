# Alexa arrives as a Home Assistant `media_player`

Commanding Echo devices — speaking an announcement, playing music, adjusting
volume — goes through the community `alexa_media_player` integration installed
**in Home Assistant**, which exposes each Echo as a `media_player.*` entity. The
app gains no Alexa-specific code at all: it keeps addressing `entity_id`s and
calling Home Assistant services, exactly as it does with a lamp.

The gain is containment. `alexa_media_player` authenticates with a
reverse-engineered session cookie and is the least reliable piece in the whole
project — it breaks when Amazon changes something on their side. Keeping it behind
Home Assistant puts that fragility in one place, and the day it breaks is not the
day this repository has to change.

## Considered Options

- **A virtual device plus an Alexa Routine** — Home Assistant exposes a fake
  switch and a Routine triggers on it. It uses only official APIs on both ends and
  is far more reliable. Rejected for not meeting the requirement: a Routine has
  fixed text and fixed actions, so there is no way to ask for a dynamic volume or
  "play this song", and each Routine would need its own fake switch. It remains
  the right option if the requirement ever narrows to triggering predefined
  scenes.
- **Voice Monkey**, a third-party webhook service. Rejected: it adds a cloud
  dependency without solving the media half.
- **Talking to Amazon directly from the app.** Rejected for the same reason
  recorded in ADR 0001: Alexa's official API is aimed at the device manufacturer,
  not the end user.

## Consequences

- **The desktop notification is never replaced, only complemented.** A Reminder
  that only spoke on the Echo would go silent along with the integration the day
  it breaks. The announcement is additional; the reliable path stays mandatory.
- This integration will need periodic maintenance, unlike the rest of the project.
  When `ta play` stops working, the first place to look is the Amazon
  authentication in Home Assistant, not the code here.
