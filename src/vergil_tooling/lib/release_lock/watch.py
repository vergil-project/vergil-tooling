"""The merge watch under the release lock (epic vergil-project/.github#1664, spec §5.5, §5.7).

The rule is layer 2 of the spec's recovery in layers (§5.7): the watch re-arms its
own PR when it finds it unarmed with no lock held.

:class:`LockWatch` is the lock rule both merge watches run on every read of
an open PR, before they would judge auto-merge off: ``vrg-finalize-pr``'s
(:func:`vergil_tooling.lib.finalize_watch.watch`) and ``vrg-pr-await``'s
(:func:`vergil_tooling.lib.pr_await.wait_for_merge`). It reads the lock
(:func:`.refs.read_lock`, no trusted-author check) and answers with a
:class:`~vergil_tooling.lib.finalize_watch.LockView`:

- **No lock held.** An armed PR is ``free``. An unarmed one is offered to
  the one PR path (:func:`vergil_tooling.lib.pr_path.arm`), the watch's only
  way to turn auto-merge on: ``armed`` or ``merged`` is ``free``, naming the
  re-arm; ``held`` (a release took the lock meanwhile) is ``held``, naming
  the path's detail; ``refused`` is ``refused``, naming the path's reason,
  and the watch ends the PR there.
- **Held, by a live release** (``running``, with a fresh heartbeat):
  ``held``, reported as ``held by release <version> (stage <stage>,
  <health>)``.
- **Held, by a broken release or one whose heartbeat is stale:** judged by
  the staleness rule (:func:`.stale.judge`). A lock that is not stale (its
  tracking issue is open, so it is the human's) is ``broken``, reported with
  why it is not reclaimed. A stale one is reclaimed
  (:func:`.reclaim.reclaim_if_stale`, as the identity running the watch):
  a reclaim that wins is ``free``, naming it, since the lock is gone and its
  re-arm sweep has run; one another reclaimer or the holder beat is
  ``free`` too, and the next read sees what is there now.
- **Held but unreadable:** ``unreadable``, reported as ``held by an
  unreadable release lock (<error>)``, and never reclaimed: only
  ``vrg-release-lock break`` clears it.

The watch never takes or breaks a lock. Every step that cannot be
completed (the lock read, a tracking issue read, the re-arm or the reclaim)
raises :class:`LockWatchError`, naming the PR and what failed; none is read
as a free lock.
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import finalize_watch, identity_mode, pr_path
from vergil_tooling.lib.finalize_watch import FREE, LockView
from vergil_tooling.lib.release_lock import reclaim, refs, stale
from vergil_tooling.lib.release_lock.model import ReleaseLockError

if TYPE_CHECKING:
    from vergil_tooling.lib.finalize_watch import Snapshot
    from vergil_tooling.lib.release_lock.model import LockRecord

__all__ = ["LockWatch", "LockWatchError"]


class LockWatchError(finalize_watch.FinalizeWatchError):
    """A lock read, re-arm or reclaim step of the merge watch failed; the message names the PR."""


def _health(rec: LockRecord, verdict: stale.Staleness, *, now: datetime) -> str:
    """``(stage <stage>, <health>)``, with the heartbeat's age when it is what makes it not live."""
    if rec.health == "running" and not verdict.live:
        age = stale.heartbeat_age_secs(rec, now=now)
        return f"(stage {rec.stage}, running, heartbeat {age}s old)"
    return f"(stage {rec.stage}, {rec.health})"


class LockWatch:
    """The release lock rule of the merge watch (see the module docstring).

    *actor* names who reclaims a stale lock, in the lock history and on the
    tracking issue; by default the identity mode and host running the
    watch, as ``vrg-release-lock break`` names its human.
    """

    def __init__(self, *, actor: str | None = None) -> None:
        self._actor = actor

    def _actor_name(self) -> str:
        if self._actor is None:
            self._actor = f"{identity_mode.current_mode().value} on {socket.gethostname()}"
        return self._actor

    def __call__(self, snap: Snapshot, /) -> LockView:
        repo, _number = finalize_watch.pr_repo_and_number(snap)
        try:
            state = refs.read_lock(repo)
        except ReleaseLockError as exc:
            msg = f"{snap.url}: reading the release lock failed: {exc}"
            raise LockWatchError(msg) from exc
        if not state.held:
            return FREE if snap.auto_merge_on else self._rearm(snap)
        rec = state.record
        if rec is None:
            return LockView("unreadable", f"held by an unreadable release lock ({state.error})")
        now = datetime.now(UTC)
        try:
            verdict = stale.judge(repo, state, now=now)
        except ReleaseLockError as exc:
            msg = f"{snap.url}: judging the release lock failed: {exc}"
            raise LockWatchError(msg) from exc
        held = f"held by release {rec.version} {_health(rec, verdict, now=now)}"
        if verdict.live:
            return LockView("held", held)
        if not verdict.stale:
            return LockView("broken", f"{held}; not reclaimed: {verdict.reason}")
        return self._reclaim(snap, repo, rec.version, verdict.reason)

    def _reclaim(self, snap: Snapshot, repo: str, version: str, reason: str) -> LockView:
        try:
            result = reclaim.reclaim_if_stale(repo, actor=self._actor_name())
        except ReleaseLockError as exc:
            msg = f"{snap.url}: reclaiming the stale release lock failed: {exc}"
            raise LockWatchError(msg) from exc
        if result is None:
            return LockView(
                "free",
                f"the stale release lock of release {version} ({reason}) was not reclaimed"
                " here: another reclaim or its holder got there first; reading it again",
            )
        armed = len(result.rearm.armed)
        return LockView(
            "free",
            f"reclaimed the stale release lock of release {version}, as stale: {result.reason};"
            f" the re-arm sweep armed {armed} PR(s)",
        )

    @staticmethod
    def _rearm(snap: Snapshot) -> LockView:
        try:
            result = pr_path.arm(snap.url)
        except pr_path.PRPathError as exc:
            msg = f"{snap.url}: re-arming it through the one PR path failed: {exc}"
            raise LockWatchError(msg) from exc
        if result.outcome == "refused":
            return LockView("refused", result.detail)
        if result.outcome == "held":
            return LockView("held", f"held by the release lock ({result.detail})")
        return LockView(
            "free",
            "auto-merge was off with no release lock held: re-armed through the one PR path"
            f" ({result.outcome})",
        )
