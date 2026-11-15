"""Escape untrusted text for interpolation into generated Markdown.

The roadmap and activity-log generators interpolate issue, epic and milestone
titles into Markdown pages that the docs sites render. A title is free text
that anyone who can open an issue controls, so it must render as literal text:
never as inline HTML, a link, emphasis, a table cell break or a heading.

This module is an import-graph leaf: it uses only the standard library.
"""

from __future__ import annotations

import re

# Markdown punctuation that can start or end inline structure. The backslash
# comes first so the escapes added for the others are not themselves escaped.
_MARKDOWN_SPECIALS = ("\\", "`", "*", "_", "[", "]", "|", "~")

_NEWLINES = re.compile(r"\r\n|\r|\n")


def escape_inline(text: str) -> str:
    """*text* made safe to interpolate into one line of Markdown.

    Newlines become spaces, so the text stays on its line (and in its table
    cell). ``&``, ``<`` and ``>`` are HTML-escaped, so no tag or entity
    survives. The Markdown specials ``\\ ` * _ [ ] | ~`` are backslash-escaped,
    and so is a leading ``#``, so no link, emphasis, code span, strikethrough,
    cell break or heading can form.
    """
    text = _NEWLINES.sub(" ", text)
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in _MARKDOWN_SPECIALS:
        text = text.replace(char, "\\" + char)
    if text.startswith("#"):
        text = "\\" + text
    return text
