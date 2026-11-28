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
- **Every open PR, once no lock is held** (including after a hold that a
  reclaim ended, and in a fresh run that never saw the hold): before it is
  re-armed, it is brought up to date once
  (:func:`vergil_tooling.lib.pr_path.update_held_pr`, vergil-tooling#5414).
  A PR behind ``develop`` is rebased onto ``origin/develop`` and pushed:
  ``updated``, which the watch reports as pending while CI re-runs; the next
  read re-arms it only if auto-merge is off. A conflicting rebase is
  ``conflicted``, which the watch ends ``failed: conflict``. A PR that is up
  to date is left alone; one that this clone cannot update reports its
  detail once per watch run (the detail says so), and is then left as the
  bullet above has it. The watch keeps no record across reads or runs, so a
  PR freed between two runs is updated by the first read of the next run.
- **Held, by a live release** (``running``, with a fresh heartbeat):
  ``held``, reported as ``held by release <version> (stage <stage>,
  <health>)``.
- **Held, by a broken release or one whose heartbeat is stale:** judged by
  the staleness rule (:func:`.stale.judge`). A lock that is not stale (its
  tracking issue is open, so it is the human's) is ``broken``, reported with
  why it is not reclaimed. A stale one is reclaimed
  (:func:`.reclaim.reclaim_if_stale`, as the identity running the watch):
  a reclaim that wins ends the hold: it is ``free``, naming it, and the watch
  makes no update or re-arm call of its own in that read. The update runs on
  the next read, which finds the lock free (see above). One another reclaimer
  or the holder beat is ``free`` too, and the next read sees what is there now.
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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import finalize_watch, identity_mode, pr_path
from vergil_tooling.lib.finalize_watch import FREE, LockView
from vergil_tooling.lib.release_lock import reclaim, refs, stale
from vergil_tooling.lib.release_lock.model import ReleaseLockError, heartbeat_age_secs

if TYPE_CHECKING:
    from vergil_tooling.lib.finalize_watch import Snapshot
    from vergil_tooling.lib.release_lock.model import LockRecord
    from vergil_tooling.lib.release_lock.refs import LockState
    from vergil_tooling.lib.release_lock.stale import Staleness

__all__ = ["HeldLock", "LockWatch", "LockWatchError"]


class LockWatchError(finalize_watch.FinalizeWatchError):
    """A lock read, re-arm or reclaim step of the merge watch failed; the message names the PR."""


@dataclass(frozen=True)
class HeldLock:
    """A held lock as judged: its view, and the state and staleness verdict it rests on.

    *staleness* is ``None`` for an unreadable lock, which is not judged.
    """

    view: LockView
    state: LockState
    staleness: Staleness | None


def _health(rec: LockRecord, verdict: stale.Staleness, *, now: datetime) -> str:
    """``(stage <stage>, <health>)``, with the heartbeat's age when it is what makes it not live."""
    if rec.health == "running" and not verdict.live:
        age = heartbeat_age_secs(rec, now=now)
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
        # The PRs this watch pushed a rebase of, with that update, until GitHub shows the new head.
        self._updated: dict[str, pr_path.HeldUpdate] = {}
        # The PRs whose not-updated detail this run has reported, so a poll does not repeat it.
        self._reported: set[str] = set()

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
            return self._free(snap)
        return self.judge_held(repo, state, where=snap.url, now=datetime.now(UTC)).view

    def judge_held(self, repo: str, state: LockState, *, where: str, now: datetime) -> HeldLock:
        """Judge a lock *state* that is held: its view, and the verdict it rests on.

        Shared by the merge watch and the submit guard. *where* names the
        caller's subject (a PR's URL, or the repository) in any error. A held
        lock that is stale is reclaimed here, as the watch's rule says; any
        failed step raises :class:`LockWatchError`.
        """
        rec = state.record
        if rec is None:
            view = LockView("unreadable", f"held by an unreadable release lock ({state.error})")
            return HeldLock(view=view, state=state, staleness=None)
        try:
            verdict = stale.judge(repo, state, now=now)
        except ReleaseLockError as exc:
            msg = f"{where}: judging the release lock failed: {exc}"
            raise LockWatchError(msg) from exc
        held = f"held by release {rec.version} {_health(rec, verdict, now=now)}"
        if verdict.live:
            view = LockView("held", held)
        elif not verdict.stale:
            view = LockView("broken", f"{held}; not reclaimed: {verdict.reason}")
        else:
            view = self._reclaim(where, repo, rec.version, verdict.reason)
        return HeldLock(view=view, state=state, staleness=verdict)

    def _reclaim(self, where: str, repo: str, version: str, reason: str) -> LockView:
        try:
            result = reclaim.reclaim_if_stale(repo, actor=self._actor_name())
        except ReleaseLockError as exc:
            msg = f"{where}: reclaiming the stale release lock failed: {exc}"
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

    def _free(self, snap: Snapshot) -> LockView:
        """The view of open PR *snap* while no lock is held (see the module docstring).

        A PR this watch pushed a rebase of is ``updated`` (pending) until GitHub
        shows the pushed head; only then is it judged and re-armed again. Any
        other PR is brought up to date once, before anything else, whether or
        not this run saw it held. Then an armed PR is ``free``, and an unarmed
        one is re-armed (:meth:`_rearm`). An update's detail is reported with the view.
        """
        url = snap.url
        pushed = self._updated.get(url)
        if pushed is not None:
            if snap.head == pushed.old_head:
                return LockView("updated", pushed.detail)
            del self._updated[url]
        update = self._update_held(snap)
        detail = self._first_report(url, update)
        if update.outcome == "updated":
            self._updated[url] = update
            return LockView("updated", detail)
        if update.outcome == "conflicted":
            return LockView("conflicted", detail)
        if snap.auto_merge_on:
            return LockView("free", detail) if detail else FREE
        if detail:
            print(f"{url}: {detail}")
        return self._rearm(snap)

    def _first_report(self, url: str, update: pr_path.HeldUpdate) -> str:
        """*update*'s detail, left out when this run already reported *url*'s not-updated detail."""
        if update.outcome != "not_updated":
            return update.detail
        if url in self._reported:
            return ""
        self._reported.add(url)
        return update.detail

    @staticmethod
    def _update_held(snap: Snapshot) -> pr_path.HeldUpdate:
        """Bring *snap*'s PR up to date through the one PR path; a step that fails raises."""
        try:
            return pr_path.update_held_pr(snap.url)
        except pr_path.PRPathError as exc:
            msg = f"{snap.url}: bringing the held PR up to date failed: {exc}"
            raise LockWatchError(msg) from exc

    @staticmethod
    def _rearm(snap: Snapshot) -> LockView:
        try:
            result = pr_path.arm(snap.url)
        except pr_path.PRPathError as exc:
            msg = (
                f"{snap.url}: re-arming it through the one PR path failed: {exc}; "
                f"the PR, read just now, is {pr_path.describe_state(snap.url)}"
            )
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
