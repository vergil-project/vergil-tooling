"""The checkout lock: one process at a time syncs or cleans up a main clone.

Several ``vrg-finalize-pr`` runs (and a ``vrg-release``) may run at once on
one machine. Their local checkout work (the sync of ``develop``, the cleanup,
the provision and the validate) is serialized by an exclusive
``fcntl.flock`` on ``<main clone>/build/.vergil-state/checkout.lock``, one
lock per clone (epic vergil-project/.github#1218, spec §6.4).

- The OS releases the lock when its holder exits, so a crashed run never
  wedges a later one.
- A run that has to wait says so once, naming the holder's PID and purpose
  when the lock file records them. Only the holder writes the file, after it
  has the lock, so a waiter never overwrites the holder's record.
- The lock is not re-entrant: a nested acquire by the same process is a bug
  and raises :class:`CheckoutLockError`.
- The wait has no timeout of its own. Every section that holds the lock is
  bounded by its own timeouts.

The lock is taken only where a command starts a sync or cleanup section of
its own (``vrg-finalize-pr --cleanup-only``, ``vrg-release``'s update-deps
fast-forward), never by a helper such a section calls: a finalize stage that
syncs or cleans up says in its docstring that its caller holds the lock.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
from typing import TYPE_CHECKING

from vergil_tooling.lib.merge_review.state import ensure_state_root

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

#: The lock file's name, under ``build/.vergil-state/``.
LOCK_NAME = "checkout.lock"

#: The lock file's permission bits when this module creates it.
_LOCK_MODE = 0o600

# The purpose of the lock this process holds, or None. A second acquire while
# it is set is a nesting bug: flock would block on this process's own lock.
_held_by_this_process: str | None = None


class CheckoutLockError(Exception):
    """The checkout lock cannot be taken: nested, or its file is unsafe."""


@contextlib.contextmanager
def checkout_lock(main_root: Path, *, purpose: str) -> Generator[None]:
    """Hold the checkout lock of the clone at *main_root* for the ``with`` body.

    Checks (creating where absent) ``build/.vergil-state`` with the
    state-directory checks of ``lib/merge_review/state.py``, then opens
    ``checkout.lock`` without truncating it. When another process holds the
    lock, prints ``Waiting for the checkout lock (held by pid <pid>:
    <purpose>)…`` once and blocks until it is free. Once it holds the lock it
    records ``<pid> <purpose>`` in the file, and clears the record before it
    lets go.

    Raises :class:`CheckoutLockError` when this process already holds the
    lock, when *purpose* is empty or spans lines, or when the lock file is a
    symlink or not a regular file. ``StateDirError`` and ``BuildDirError``
    pass through from the state-directory checks.
    """
    global _held_by_this_process  # noqa: PLW0603
    if _held_by_this_process is not None:
        msg = (
            "checkout lock already held by this process "
            f"(held for {_held_by_this_process!r}, asked for {purpose!r})"
        )
        raise CheckoutLockError(msg)
    if not purpose.strip() or "\n" in purpose or "\r" in purpose:
        msg = f"checkout lock purpose must be one non-empty line, not {purpose!r}"
        raise CheckoutLockError(msg)

    path = ensure_state_root(main_root) / LOCK_NAME
    fd = _open_lock_file(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Waiting for the checkout lock ({_describe_holder(fd)})…", flush=True)
            fcntl.flock(fd, fcntl.LOCK_EX)
        _held_by_this_process = purpose
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()} {purpose}\n".encode(), 0)
            yield
        finally:
            _held_by_this_process = None
            # Clear the record before letting go, so a stale holder line never
            # names a process that has finished. Unlocking happens on close.
            os.ftruncate(fd, 0)
    finally:
        os.close(fd)


def _open_lock_file(path: Path) -> int:
    """Open *path* read-write, creating it, refusing a symlink or a non-file."""
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path, flags, _LOCK_MODE)
    except OSError as exc:
        if path.is_symlink():
            msg = f"{path} is a symlink, not a lock file"
            raise CheckoutLockError(msg) from exc
        if path.is_dir():
            msg = f"{path} is not a regular file"
            raise CheckoutLockError(msg) from exc
        raise
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        msg = f"{path} is not a regular file"
        raise CheckoutLockError(msg)
    return fd


def _describe_holder(fd: int) -> str:
    """Name the holder from the lock file's record, or say it has none.

    The record is ``<pid> <purpose>``. It is empty between a new holder's
    acquire and its write, and after a holder lets go.
    """
    record = os.pread(fd, 4096, 0).decode(errors="replace").strip()
    pid, _, holder_purpose = record.partition(" ")
    if pid.isdigit() and holder_purpose:
        return f"held by pid {pid}: {holder_purpose}"
    return "holder not recorded"
