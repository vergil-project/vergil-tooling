"""The lock owner: the one writer of a held release lock.

Spec §5.2 and §5.6 of epic vergil-project/.github#1664. A release holds its
lock through a :class:`LockOwner`, which every write of the lock (stage,
health, tracking issue, worktree, heartbeat) goes through:

- :meth:`LockOwner.take`, :meth:`LockOwner.adopt` and
  :meth:`LockOwner.takeover` each start a background heartbeat writer before
  they return, so the heartbeat stays fresh through every blocking wait and
  no caller can forget it.
- One lock serialises every write, the heartbeat's included, so no update is
  lost to another.
- Each write is a compare-and-swap against the sha of the owner's last
  write. An own write is told by the sha the owner built, never by the
  record's fields: an adopting owner keeps the holder, version and start
  time, so field matching would read another process's write as its own. A
  mismatch against a sha the owner built (a write that landed although its
  push reported a failure) is retried against it. Any other mismatch, an
  absent lock included, is a loss: the owner raises :class:`LockLost`, at
  once or on the next write when the heartbeat found it, and the heartbeat
  stops. The owner never re-takes a lost lock.
- On a loss, the owner reads the lock history: a ``break``, ``reclaim`` or
  ``takeover`` entry whose old lock carries this owner's token names the
  cause. With none, the lock was removed or rewritten outside the tooling,
  and :class:`LockLost` says so (:attr:`LockLost.outside_change`), for the
  release to record on its tracking issue as a failure to analyse.
"""

from __future__ import annotations

import dataclasses
import sys
import threading
from typing import TYPE_CHECKING, Any, Final, cast

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release_lock import history, refs
from vergil_tooling.lib.release_lock.model import (
    HEARTBEAT_INTERVAL_SECS,
    LockParseError,
    LockRecord,
    RefReadError,
    RefWriteError,
    ReleaseLockError,
    from_json,
    new_token,
    utc_now,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.release_lock.refs import LockState

#: The record fields a holder may change with :meth:`LockOwner.update`.
UPDATABLE: Final = frozenset({"stage", "health", "broken_stage", "tracking_issue", "worktree"})
#: The history actions that end a holder's lock, and how a loss names each.
_CAUSES: Final = {"break": "broken", "reclaim": "reclaimed", "takeover": "taken over"}
#: How many times one write is retried against the owner's own earlier writes.
_WRITE_ATTEMPTS: Final = 5
_COMMAND: Final = "release-lock"


class LockLost(ReleaseError):  # noqa: N818 - the name is the plan's interface
    """The holder's lock was broken, reclaimed, taken over or changed outside the tooling.

    *cause* is the history entry that names the loss, or ``None``.
    *outside_change* is true when the history was read and records no break,
    reclaim or takeover of this lock: the lock was removed or rewritten
    outside the tooling.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        cause: history.HistoryEntry | None,
        outside_change: bool,
    ) -> None:
        super().__init__(phase=phase, command=_COMMAND, message=message)
        self.cause = cause
        self.outside_change = outside_change


def _find_cause(repo: str, tokens: frozenset[str]) -> tuple[history.HistoryEntry | None, str]:
    """The latest history entry that ended a lock carrying one of *tokens*.

    Returns the entry (or ``None``) and, when the history could not be read,
    the reason (else ``""``). An entry whose old lock does not parse names no
    token, so it is never this owner's.
    """
    try:
        entries = history.read_history(repo)
    except RefReadError as exc:
        return None, str(exc)
    for entry in reversed(entries):
        if entry.action not in _CAUSES:
            continue
        try:
            old = from_json(entry.old)
        except LockParseError:
            continue
        if old.token in tokens:
            return entry, ""
    return None, ""


def _loss(repo: str, *, now: str | None, tokens: frozenset[str], phase: str) -> LockLost:
    """The :class:`LockLost` for a lock found at *now* (``None``: absent)."""
    where = "absent" if now is None else f"at {now}"
    cause, unreadable = _find_cause(repo, tokens)
    if cause is not None:
        if cause.action == "takeover":
            # A supersede moved the lock to the release replacing this one: nothing to resume.
            advice = "this release was superseded and needs nothing further."
        else:
            advice = (
                "finish it with `vrg-release --resume` or replace it with"
                " `vrg-release --supersede`."
            )
        message = (
            f"the release lock on {repo} was {_CAUSES[cause.action]} by {cause.actor}"
            f" at {cause.at}: {cause.reason or 'no reason given'}. This release has"
            f" stopped and does not re-take the lock: {advice}"
        )
        return LockLost(message, phase=phase, cause=cause, outside_change=False)
    if unreadable:
        message = (
            f"the release lock on {repo} was lost (the lock is now {where}), and its cause"
            f" could not be read from {refs.HISTORY_REF}: {unreadable}. This release has stopped."
        )
        return LockLost(message, phase=phase, cause=None, outside_change=False)
    message = (
        f"the release lock on {repo} was removed or rewritten outside the tooling: the lock"
        f" is now {where}, and {refs.HISTORY_REF} records no break, reclaim or takeover of"
        " it. This release has stopped; record it on the tracking issue as a failure to"
        " analyse."
    )
    return LockLost(message, phase=phase, cause=None, outside_change=True)


def _fresh(rec: LockRecord) -> LockRecord:
    """*rec* with a new token and the heartbeat at now: a new owner's first record."""
    return dataclasses.replace(rec, token=new_token(), heartbeat_at=utc_now())


def _adoptable(repo: str, state: LockState) -> tuple[str, LockRecord]:
    """*state*'s sha and record; :class:`ValueError` for an absent or unreadable lock."""
    if state.sha is None:
        msg = f"cannot adopt the release lock on {repo}: it is absent"
        raise ValueError(msg)
    if state.record is None:
        msg = f"cannot adopt the release lock on {repo}: it is unreadable ({state.error})"
        raise ValueError(msg)
    return state.sha, state.record


class LockOwner:
    """The holder's one writer of the lock, with its background heartbeat.

    Made only by :meth:`take`, :meth:`adopt` and :meth:`takeover`.
    """

    def __init__(self, repo: str, record: LockRecord, sha: str) -> None:
        self._repo = repo
        self._record = record
        self._sha = sha
        self._built: set[str] = {sha}
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lost: LockLost | None = None
        self._crash: Exception | None = None

    # --- making an owner ----------------------------------------------------------

    @classmethod
    def take(cls, repo: str, rec: LockRecord) -> LockOwner | None:
        """Take *repo*'s lock with a create-only push, with a new token; ``None`` if it is held."""
        rec = _fresh(rec)
        with refs.scratch(repo, RefWriteError) as where:
            sha = refs.build_lock(where, rec)
            if not refs.write_ref(where, refs.LOCK_REF, new=sha, expected=None):
                return None
        owner = cls(repo, rec, sha)
        owner.start_heartbeat()
        return owner

    @classmethod
    def adopt(cls, repo: str, state: LockState) -> LockOwner:
        """Adopt the lock read as *state*, keeping its record but writing a new token.

        An earlier owner of the same release then meets a sha it did not
        build at its next write, and stops. Raises :class:`LockLost` if the
        lock has changed since *state* was read, and :class:`ValueError` for
        an absent or unreadable lock.
        """
        sha, old = _adoptable(repo, state)
        return cls._replace(repo, sha, _fresh(old), frozenset({old.token}))

    @classmethod
    def takeover(cls, repo: str, state: LockState, rec: LockRecord) -> LockOwner:
        """Take over the lock read as *state* with *rec* (and a new token), by compare-and-swap.

        Supersede's takeover. Raises :class:`LockLost` if the lock has
        changed since *state* was read, and :class:`ValueError` for an absent
        lock (take it instead). An unreadable lock may be taken over.
        """
        if state.sha is None:
            msg = f"cannot take over the release lock on {repo}: it is absent"
            raise ValueError(msg)
        tokens = frozenset({state.record.token}) if state.record is not None else frozenset()
        return cls._replace(repo, state.sha, _fresh(rec), tokens)

    @classmethod
    def _replace(
        cls, repo: str, expected: str, rec: LockRecord, tokens: frozenset[str]
    ) -> LockOwner:
        with refs.scratch(repo, RefWriteError) as where:
            sha = refs.build_lock(where, rec)
            if not refs.write_ref(where, refs.LOCK_REF, new=sha, expected=expected):
                now = _reread(where)
                raise _loss(repo, now=now, tokens=tokens, phase=rec.stage)
        owner = cls(repo, rec, sha)
        owner.start_heartbeat()
        return owner

    # --- reading ------------------------------------------------------------------

    @property
    def sha(self) -> str:
        """The sha of the owner's last write."""
        with self._write_lock:
            return self._sha

    @property
    def token(self) -> str:
        """The holder's pass for ``pr_path.arm``: this owner's token."""
        return self._record.token

    @property
    def record(self) -> LockRecord:
        """The record of the owner's last write."""
        with self._write_lock:
            return self._record

    @property
    def heartbeat_running(self) -> bool:
        """Whether the background heartbeat writer is alive."""
        return self._thread is not None and self._thread.is_alive()

    # --- writing ------------------------------------------------------------------

    def update(self, **fields: str) -> None:
        """Write *fields* (and a fresh heartbeat) into the lock.

        Only the :data:`UPDATABLE` fields may change. Raises
        :class:`LockLost` once the lock is lost, :class:`RuntimeError` once
        the heartbeat stopped on an unexpected error, :class:`ValueError` for
        an invalid record, and :class:`RefWriteError` when the write failed.
        """
        unknown = sorted(fields.keys() - UPDATABLE)
        if unknown:
            msg = (
                f"the lock owner cannot update {', '.join(unknown)};"
                f" only {', '.join(sorted(UPDATABLE))}"
            )
            raise ValueError(msg)
        with self._write_lock:
            self._check()
            changed = cast("dict[str, Any]", fields)
            self._cas(dataclasses.replace(self._record, **changed, heartbeat_at=utc_now()))

    def release(self) -> None:
        """Stop the heartbeat, then delete the lock by compare-and-swap on the last write."""
        self.stop_heartbeat()
        with self._write_lock:
            self._check()
            self._cas(None)

    def _check(self) -> None:
        if self._lost is not None:
            raise self._lost
        if self._crash is not None:
            msg = (
                f"the heartbeat of the release lock on {self._repo} stopped on an"
                f" unexpected error: {self._crash!r}"
            )
            raise RuntimeError(msg) from self._crash

    def _cas(self, rec: LockRecord | None) -> None:
        """Write *rec* (``None`` deletes the lock) against the last write; hold the write lock."""
        expected = self._sha
        for _attempt in range(_WRITE_ATTEMPTS):
            with refs.scratch(self._repo, RefWriteError) as where:
                new = None
                if rec is not None:
                    new = refs.build_lock(where, rec)
                    self._built.add(new)  # before the push: it may land and still report a failure
                if refs.write_ref(where, refs.LOCK_REF, new=new, expected=expected):
                    if rec is not None and new is not None:
                        self._sha, self._record = new, rec
                    return
                now = _reread(where)
            if now is None or now not in self._built:
                self._lost = _loss(
                    self._repo, now=now, tokens=frozenset({self.token}), phase=self._record.stage
                )
                self._stop.set()
                raise self._lost
            expected = now  # an own write that landed: retry against it
        msg = (
            f"writing the release lock on {self._repo} met its own earlier writes"
            f" {_WRITE_ATTEMPTS} times"
        )
        raise RefWriteError(msg)

    # --- the heartbeat ------------------------------------------------------------

    def start_heartbeat(self) -> None:
        """Start the background heartbeat writer; called only by take, adopt and takeover."""
        if self.heartbeat_running:
            msg = f"the heartbeat of the release lock on {self._repo} is already running"
            raise RuntimeError(msg)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._beat, name=f"release-lock heartbeat {self._repo}", daemon=True
        )
        self._thread.start()

    def stop_heartbeat(self) -> None:
        """Stop the heartbeat writer and wait for it to end."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _beat(self) -> None:
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECS):
            with self._write_lock:
                if self._stop.is_set():  # stopped, or lost by an update, while it waited
                    return
                try:
                    self._cas(dataclasses.replace(self._record, heartbeat_at=utc_now()))
                except LockLost:
                    return
                except ReleaseLockError as exc:
                    print(
                        f"warning: the release lock heartbeat on {self._repo} failed,"
                        f" retrying in {HEARTBEAT_INTERVAL_SECS}s: {exc}",
                        file=sys.stderr,
                    )
                except Exception as exc:  # noqa: BLE001 - kept and raised by the next update
                    self._crash = exc
                    return


def _reread(where: Path) -> str | None:
    """The lock's sha on origin after a rejected write; :class:`RefWriteError` if unreadable."""
    try:
        return refs.remote_sha(where, refs.LOCK_REF)
    except RefReadError as exc:
        msg = f"re-reading {refs.LOCK_REF} after a rejected write failed: {exc}"
        raise RefWriteError(msg) from exc
