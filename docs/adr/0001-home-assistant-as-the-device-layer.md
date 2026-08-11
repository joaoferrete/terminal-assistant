# Home Assistant as the device layer

The current hardware is WiFi Tuya (bulbs and a plug), but the stated intention is
to move to Zigbee at some undefined point. We put Home Assistant as the only
layer between the app and the appliances: the app speaks REST/WebSocket to Home
Assistant and never knows a device's protocol. Swapping Tuya for Zigbee (or
Matter) becomes a pairing job in Home Assistant, not a code change.

## Considered Options

- **`tinytuya` directly, behind an adapter interface.** Rejected: it does not
  remove the need for infrastructure later. Zigbee would require `zigbee2mqtt` or
  ZHA anyway, arriving at the same destination by a longer road — and with a
  hand-written adapter to maintain.
- **`tinytuya` directly, with no abstraction.** Rejected: it knowingly signs up
  for rewriting the home module when the hardware changes.

Worth recording why Google's cloud is not among the options: the new Home APIs
are SDK-only for Android and iOS; Smart Device Management covers Nest devices
only; and the Assistant SDK is discontinued. There is no official Google path to
controlling a third-party bulb from a Linux CLI. Alexa is worse — its smart home
API is aimed at the device *manufacturer*, not the end user.

## Consequences

- The app depends on an external service being up. A stopped Home Assistant means
  the home module is unavailable, and the app has to degrade clearly rather than
  hang.
- Home Assistant owns the inventory and the naming of appliances. The app keeps
  no list of its own.
- We get a web dashboard, a phone app and state history for free.
