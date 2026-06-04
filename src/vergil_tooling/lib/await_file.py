"""Filesystem channel primitives for the ``.vergil/`` agent handshake.

These are the rock-solid building blocks behind ``vrg-await`` (§6 of the
Vergil 2.1 workflow design):

- :func:`atomic_write` — write via a temp file + ``os.replace`` so a reader
  watching the path never observes a half-written file; the content flips in
  a single atomic step.
- :func:`wait_for_file` — block until a path appears (or its content changes,
  detected by SHA-256).

The waiter recomputes the SHA-256 on every poll rather than gating on mtime.
The channel files are tiny, and a content checksum is the authoritative,
filesystem-independent change signal — mtime resolution and update semantics
vary across filesystems, which is precisely the flakiness the design warns
about. Correctness over a micro-optimization that does not matter at this size.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
import time
from pathlib import Path

_POLL_INTERVAL = 1.0


def compute_sha256(path: Path) -> str:
    """Return the hex SHA-256 digest of the file at ``path``."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def atomic_write(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` atomically via a temp file + ``os.replace``.

    A reader watching ``path`` never observes a partially written file: the
    rename is atomic and the content flips in a single step. Parent directories
    are created as needed, and no temp file is left behind on failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
        raise


def wait_for_file(
    path: Path,
    since: str | None = None,
    *,
    poll_interval: float = _POLL_INTERVAL,
) -> str:
    """Block until ``path`` is a regular file whose content meets the condition.

    With ``since=None``: return as soon as ``path`` exists as a regular file.
    With ``since`` set: return once the file exists *and* its SHA-256 differs
    from ``since`` (the previously observed digest).

    Returns the current SHA-256 digest. Blocks indefinitely, polling every
    ``poll_interval`` seconds.
    """
    while True:
        if path.is_file():
            current = compute_sha256(path)
            if since is None or current != since:
                return current
        time.sleep(poll_interval)
