"""Plain-text tables for terminal reports.

``vrg-repo-status`` and ``vrg-finalize-pr`` print their PR tables through
:func:`render` and cut long titles with :func:`truncate`, so the fleet's
commands lay a PR table out the same way (issue
vergil-project/vergil-tooling#4708).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

#: The width a TITLE cell is cut to, keeping each PR row on one line.
TITLE_WIDTH = 72


def render(columns: Sequence[str], rows: Sequence[Sequence[str]], *, indent: str = "") -> str:
    """*columns* and *rows* as left-aligned columns two spaces apart.

    Every row must have one cell per column. Trailing spaces are stripped
    from each line, and each line starts with *indent*.
    """
    cells = [tuple(columns), *(tuple(row) for row in rows)]
    for row in cells:
        if len(row) != len(columns):
            msg = f"row has {len(row)} cells for {len(columns)} columns: {row!r}"
            raise ValueError(msg)
    widths = [max(len(row[i]) for row in cells) for i in range(len(columns))]
    return "\n".join(
        (indent + "  ".join(row[i].ljust(widths[i]) for i in range(len(columns)))).rstrip()
        for row in cells
    )


def truncate(text: str, width: int = TITLE_WIDTH) -> str:
    """*text* cut to *width* characters, its last one an ellipsis when cut."""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"
