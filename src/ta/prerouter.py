"""The deterministic front of the bot (D9): what needs no model.

"Apaga a luz" should not wait for a model, cost a call, or stop working when the
provider is down. So a small grammar recognises switching the house and hands it
straight to the home Tools, in a clean (untainted) turn.

The words are input syntax, like `@sexta` and `!alta`: typed by people, so they
are kept in both languages at once, and never renamed.

A match is only a *candidate*. The bot runs it only if the target resolves to
something the asker may switch — "apaga aquela nota" starts like "apaga a luz",
and must reach the agent instead of failing to find a lamp called "aquela nota".
"""

from __future__ import annotations

import re

_ON = r"acende|acenda|acender|liga|ligue|ligar|turn on|switch on"
_OFF = r"apaga|apague|apagar|desliga|desligue|desligar|turn off|switch off"
_ARTICLE = r"(?:(?:a|o|as|os|the)\s+)?"
# Longest first: in an alternation `light` would win over `lights` and leave the
# "s" glued to the target ("s in the bedroom").
_LIGHT = r"(?:(?:luzes|luz|lampadas|lâmpadas|lampada|lâmpada|lights|light|lamps|lamp)\s*)?"
_OF = r"(?:(?:da|do|das|dos|de|in the|in|of the)\s+)?"

_PATTERN = re.compile(
    rf"^\s*(?:por favor\s+|please\s+)?(?P<verb>{_ON}|{_OFF})\s+{_ARTICLE}{_LIGHT}{_OF}"
    r"(?P<target>.*?)\s*(?:,?\s*(?:por favor|please))?[.!]*\s*$",
    re.IGNORECASE,
)


def home(text: str) -> tuple[str, str] | None:
    """("home_on"|"home_off", target) for a switching sentence, else None.

    An empty target means the lights (`luz`), as in a bare "apaga a luz".
    """
    m = _PATTERN.match(text)
    if m is None:
        return None
    tool = "home_on" if re.fullmatch(_ON, m["verb"], re.IGNORECASE) else "home_off"
    target = m["target"].strip()
    return tool, target or "luz"
