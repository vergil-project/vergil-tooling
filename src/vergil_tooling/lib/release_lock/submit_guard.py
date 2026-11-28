"""The release lock's guard on ``vrg-submit-pr`` (vergil-tooling#5427, human ruling F17).

While a release holds a repository's lock, a push or PR lands on a head that
cannot merge until the lock is released (CI runs on it for nothing, and a push
that lands after the release tags its version fails ``version / version-bump``).
So ``vrg-submit-pr`` runs :func:`clear` once, before any mode pushes or opens
anything, and not for ``--dry-run``:

- **No lock held**, or a held lock the watch's rule reclaims as stale (the
  reclaim is the watch's, :meth:`.watch.LockWatch.judge_held`): :func:`clear`
  returns ``False`` and the submit goes on unchanged.
- **Held, and the caller did not ask to wait**: :class:`LockedByReleaseError`,
  naming what the lock records and how to wait. The tool exits 7 and pushes
  nothing.
- **Held, and the caller asked to wait** (``--wait-for-release``): :func:`clear`
  polls the lock, reporting each change, until it clears (it returns ``True``,
  so the caller knows it waited and may rebase) or the deadline
  (``finalize_watch.deadline_secs``) passes, which is
  :class:`LockWaitTimedOutError`: the tool exits 6 and pushes nothing. A broken
  or unreadable lock is polled at the merge watch's pace, a live one at
  :data:`.model.SUBMIT_WAIT_POLL_SECS`.
- **A failed read or judgement**: :class:`LockGuardError`, naming the error. A
  lock that cannot be read is never taken as free, and the tool exits 1.

An unreadable lock is held, with its read error named. The held state is read
from the lock's structured record (:class:`.model.LockRecord`), never parsed
from text.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import finalize_watch
from vergil_tooling.lib.release_lock import refs, status, watch
from vergil_tooling.lib.release_lock.model import (
    BROKEN_WATCH_POLL_SECS,
    SUBMIT_WAIT_POLL_SECS,
    ReleaseLockError,
    heartbeat_age_secs,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "LockGuardError",
    "LockWaitTimedOutError",
    "LockedByReleaseError",
    "clear",
]


class LockGuardError(Exception):
    """The lock could not be read or judged, or the wait's deadline is malformed.

    Its message is complete and names what failed; nothing was pushed.
    """


class LockedByReleaseError(Exception):
    """The lock is held by a release and the caller did not ask to wait.

    Its message names the lock's record and how to wait; nothing was pushed.
    """


class LockWaitTimedOutError(Exception):
    """The lock was still held when the wait's deadline passed; nothing was pushed."""


def clear(
    repo: str,
    *,
    wait: bool,
    report: Callable[[str], object] = print,
    sleep: Callable[[float], object] | None = None,
    clock: Callable[[], float] | None = None,
    actor: str | None = None,
) -> bool:
    """Clear *repo*'s release lock for a submit: ``True`` when it waited for it, ``False`` when it was clear.

    *wait* is the ``--wait-for-release`` flag. *report* receives the wait's
    progress lines. *sleep* and *clock* (``time.sleep`` and ``time.monotonic``
    by default) are injectable so a test does not sleep. *actor* names who
    reclaims a stale lock, as :class:`.watch.LockWatch` does.

    Raises :class:`LockedByReleaseError` when the lock is held and *wait* is
    false, :class:`LockWaitTimedOutError` when a wait reaches the deadline, and
    :class:`LockGuardError` for any read, judgement or deadline failure.
    """
    lock = watch.LockWatch(actor=actor)
    reading = _read(repo, lock)
    if reading is None or reading.view.state == "free":
        if reading is not None:
            report(reading.view.detail)
        return False
    if not wait:
        raise LockedByReleaseError(_refusal(repo, reading))
    return _wait(repo, lock, reading, report=report, sleep=sleep, clock=clock)


def _read(repo: str, lock: watch.LockWatch) -> watch.HeldLock | None:
    """The lock of *repo* judged when held, ``None`` when it is free.

    A reclaimed stale lock comes back with a ``free`` view, which the caller
    reads as free too.
    """
    try:
        state = refs.read_lock(repo)
    except ReleaseLockError as exc:
        msg = (
            f"vrg-submit-pr: cannot read the release lock on {repo}, so it cannot be told "
            f"whether a release holds it: {exc}; nothing was pushed and no PR was opened."
        )
        raise LockGuardError(msg) from exc
    if not state.held:
        return None
    try:
        return lock.judge_held(repo, state, where=repo, now=datetime.now(UTC))
    except watch.LockWatchError as exc:
        msg = f"vrg-submit-pr: {exc}; nothing was pushed and no PR was opened."
        raise LockGuardError(msg) from exc


def _refusal(repo: str, reading: watch.HeldLock) -> str:
    """The message for a held lock the caller did not ask to wait for."""
    lines = [
        f"vrg-submit-pr: the repository {repo} is locked by another process, most likely a "
        "release, and may stay locked for a long time."
    ]
    state, verdict = reading.state, reading.staleness
    rec = state.record
    if rec is None:
        lines.append(f"release lock: held, unreadable: {state.error}")
    else:
        lines.append("release lock: held")
        judged = verdict.tracking_issue if verdict is not None else ""
        age = heartbeat_age_secs(rec, now=datetime.now(UTC))
        lines += status.record_lines(rec, judged_issue=judged, heartbeat_age_secs=age)
        if verdict is not None:
            lines.append(f"stale: {'yes' if verdict.stale else 'no'} ({verdict.reason})")
    lines += [
        "Nothing was pushed and no PR was opened.",
        "To wait for the lock to clear instead, re-run with --wait-for-release: it waits "
        "within the deadline ($VRG_FINALIZE_DEADLINE) and then gives up with exit 6.",
    ]
    return "\n".join(lines)


def _wait(
    repo: str,
    lock: watch.LockWatch,
    first: watch.HeldLock,
    *,
    report: Callable[[str], object],
    sleep: Callable[[float], object] | None,
    clock: Callable[[], float] | None,
) -> bool:
    """Poll *repo*'s held lock until it clears; ``True``, or :class:`LockWaitTimedOutError`."""
    try:
        limit = finalize_watch.deadline_secs()
    except ValueError as exc:
        msg = f"vrg-submit-pr: {exc}; nothing was pushed and no PR was opened."
        raise LockGuardError(msg) from exc
    pause = time.sleep if sleep is None else sleep
    elapsed = time.monotonic if clock is None else clock
    start = elapsed()
    reading: watch.HeldLock | None = first
    shown = ""
    while reading is not None and reading.view.state != "free":
        detail = reading.view.detail
        if detail != shown:
            report(f"waiting for the release lock on {repo}: {detail}")
            shown = detail
        left = limit - (elapsed() - start)
        if left <= 0:
            msg = (
                f"vrg-submit-pr: the release lock on {repo} is still held after waiting "
                f"{limit}s, the deadline ($VRG_FINALIZE_DEADLINE): {detail}. Nothing was "
                "pushed and no PR was opened. Re-run with --wait-for-release to wait again."
            )
            raise LockWaitTimedOutError(msg)
        pace = BROKEN_WATCH_POLL_SECS if reading.view.slow else SUBMIT_WAIT_POLL_SECS
        pause(min(pace, left))
        reading = _read(repo, lock)
    report(f"the release lock on {repo} has cleared")
    if reading is not None:
        report(reading.view.detail)
    return True
