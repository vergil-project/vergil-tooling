"""The plain-text rule: printable ASCII (U+0020 to U+007E) plus newline only.

Every piece of text the ruling tools handle passes this check. Anything else,
tab and carriage return included, is refused with the character's code point and
its offset, and is never converted: plain ASCII has no invisible or look-alike
characters, so what is on screen is all there is. A line that contains the
marker prefix, in any letter case, is refused too, so text can never carry or
imitate a vergil marker.
"""

from __future__ import annotations

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.marker import MARKER_PREFIX


def check_plain_text(text: str, *, what: str) -> None:
    """Refuse ``text`` unless it is printable ASCII plus newline with no marker prefix.

    The message names ``what``, the offending character's code point (or the
    marker prefix) and its offset in ``text``.
    """
    for offset, char in enumerate(text):
        if char != "\n" and not " " <= char <= "~":
            msg = (
                f"{what} is not plain text: character U+{ord(char):04X} at offset {offset}"
                " (only printable ASCII, U+0020 to U+007E, and newline are allowed)"
            )
            raise RulingError(msg)
    # The text is ASCII here, so lower() keeps every offset in place.
    offset = text.lower().find(MARKER_PREFIX.lower())
    if offset != -1:
        msg = f"{what} contains the vergil marker prefix {MARKER_PREFIX!r} at offset {offset}"
        raise RulingError(msg)
