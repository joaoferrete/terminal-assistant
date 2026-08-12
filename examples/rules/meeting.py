"""The rule that motivated the project.

"I joined a Meet call and it is past 16:00, so turn the light up to full — and it
would be PERFECT if I could combine that with the ringlight coming on."

It is the case no off-the-shelf engine solves: the trigger is born on the
computer (the microphone), the condition is about the clock, and the actions land
half in the house and half on the PC. That is why the app is the brain and Home
Assistant is a dumb actuator (ADR 0002).

The original request tied the hour to *entering* the call. Today the hour does not
decide *whether* the rule runs — it decides **what it does**, because what `16:00`
really means is "it is dark now":

- The **light** goes to full at any hour: on a call it always helps.
- The **ringlight** only comes in after dark. During the day natural light does
  the job, and a lit edge is production nobody asked for.
- At the **end**, the light goes back to a living level only if it is still early.
  After that it stays where it is, because lowering it would return the room to
  darkness.

This file is versioned as documentation and is NOT loaded — it targets one
specific house's inventory. Copy it to `~/.config/ta/rules/`, adjust the entity
ids, and `ta rules check` validates it without booting the daemon.
"""

from ta.engine import after, mic_active, mic_inactive, rule

# An id from a real house inventory. Replace it with one of yours.
LIGHT = "light.bedroom_lamp"
RINGLIGHT_PROFILE = "Meet"

DARK_AFTER = "16:00"
LIVING_LEVEL = 40


def _is_dark(ctx) -> bool:
    """Whether `DARK_AFTER` has already passed.

    One place decides what "16:00" means, and both uses read from here: turning
    the ringlight on at entry, and holding the light at exit. It is the same
    primitive a `when=` would use, so the three cannot disagree.

    The hour is read at the moment of firing, not at the start of the call — a
    meeting crossing 16:00 counts as dark on the way out. What decides is the
    window, not the calendar.
    """
    return after(DARK_AFTER)(ctx)


async def _current_title(ctx) -> str:
    """The title of the appointment in progress, or empty.

    The calendar comes in as context, not as a trigger (ADR 0008): it says WHICH
    meeting it is, and allows deciding differently per kind of appointment.
    """
    event = await ctx.calendar.now() if ctx.calendar else None
    return (event or {}).get("summary", "")


def _no_production(title: str) -> bool:
    """A 1:1 needs no production.

    An example of a condition that only exists because the app has the calendar
    on top of the microphone signal.
    """
    return bool(title) and "1:1" in title


@rule(on=mic_active(), name="meeting")
async def meeting(ctx):
    """A call with the mic on: light always, ringlight only after dark."""
    title = await _current_title(ctx)
    if _no_production(title):
        return

    await ctx.home.light(LIGHT, 100)

    dark = _is_dark(ctx)
    if dark:
        await ctx.lighter.apply_profile(RINGLIGHT_PROFILE)

    if title:
        # The notification says what actually happened. Announcing a ringlight in
        # the morning would be a cheap lie, and that is how people stop trusting
        # the alert.
        what = "light and ringlight on" if dark else "light on"
        await ctx.notify.send("Meeting", f"{title} — {what}", urgency="low")


@rule(on=mic_inactive(), name="meeting_end")
async def meeting_end(ctx):
    """Left the call: the ringlight always goes off, the light only drops if early.

    Turning it off entirely would be hostile — the person is still in the room.

    The `enable(False)` is unconditional on purpose, even though in the morning
    the ringlight was never turned on: switching off something already off costs
    nothing, and the alternative — repeating the entry condition here — would
    leave the edge lit after a meeting that started at 15:50 and ended at 16:10.

    The 1:1 is checked here too, not only at entry: if nothing was turned on,
    nothing should be undone. That check depends on the appointment still being in
    progress (`calendar.now()` only returns an event between `start` and `end`),
    so somebody leaving **after** the scheduled time falls through to the common
    path and has the light adjusted. Erring towards adjusting is the cheap side.
    """
    if _no_production(await _current_title(ctx)):
        return

    await ctx.lighter.enable(False)

    if not _is_dark(ctx):
        await ctx.home.light(LIGHT, LIVING_LEVEL)
