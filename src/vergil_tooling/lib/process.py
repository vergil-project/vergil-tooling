"""Replace the current process without losing buffered output."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Sequence


def exec_replace(file: str, args: Sequence[str]) -> NoReturn:
    """Flush stdout and stderr, then ``os.execvp`` *file* with *args*.

    ``execvp`` replaces the process image without flushing Python's stream
    buffers. When stdout is a pipe or a file, Python block-buffers it, so any
    output printed just before the exec (a run banner, say) is silently lost
    unless it is flushed first (issue #3396). Returns only when ``os.execvp``
    is stubbed, as in tests.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvp(file, list(args))  # noqa: S606, S607
