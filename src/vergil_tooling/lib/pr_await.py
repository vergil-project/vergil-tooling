"""Merge waiter behind ``vrg-pr-await`` (epic vergil-project/.github#1218, follow-up F5).

``vrg-submit-pr`` opens each PR with auto-merge on, and GitHub merges it once
its required checks pass. :func:`wait_for_merge` waits on one such PR until it
merges, until it can no longer merge as it stands, or until the deadline
passes. It never merges a PR and never turns auto-merge off. Its only change is
bringing a PR that a release held up to date, from its own worktree (step 3).

There is no webhook ingress on a laptop, so it polls: one
:func:`vergil_tooling.lib.finalize_watch.snapshot` read per interval, the same
read ``vrg-finalize-pr``'s watch phase judges. Each read is judged in this
order:

1. **Merged:** the wait ends in success, with the merge commit. A PR that
   merged before the wait started counts too.
2. **Closed** without merging: reason ``closed``.
3. **The release lock** (epic vergil-project/.github#1664, spec §5.5), the
   rule ``vrg-finalize-pr``'s watch applies: the wait's *lock* rule
   (:class:`vergil_tooling.lib.release_lock.watch.LockWatch`) is asked
   about the open PR before auto-merge off is judged, so auto-merge off
   never ends the wait. Under a held lock the PR is reported as held (the
   release, its version, its stage and its health, on stderr when it
   changes) and the wait goes on, every
   :data:`~vergil_tooling.lib.release_lock.model.BROKEN_WATCH_POLL_SECS`
   while the release is broken or its lock unreadable, reclaiming a stale
   lock. With no lock held an unarmed PR is re-armed through the one PR
   path; a re-arm the path refuses ends the wait with reason ``refused``,
   naming the path's reason. A re-arm refused only because the base branch
   kept moving keeps the wait going: the PR is re-armed again on the next
   poll, and only a run of such refusals that the lock rule gives up on
   raises (vergil-tooling#5390). A hold that ends on a PR behind ``develop``
   brings it up to date first (vergil-tooling#5414): a pushed rebase keeps
   the wait going, its checks not judged until the next read shows the new
   head; a rebase that conflicts ends the wait with reason ``conflict``.
4. **A required check concluded in failure:** reason ``check_failed``,
   naming the checks, judged by
   :func:`vergil_tooling.lib.finalize_watch.check_failures` as the finalize
   watch judges them: a failed or cancelled check-run of a run that a newer
   run of its workflow superseded on the same head is not a failure (#4692).
5. **A required check was cancelled** on a cancelled run that no newer run
   replaced, for :data:`finalize_watch.CANCEL_GRACE_SECS` on end: reason
   ``check_cancelled``, naming the checks in ``failed_checks``. Until then
   the wait goes on, since a re-run may still start.
6. **Conflict** (``mergeStateStatus`` ``DIRTY``) reported at the same head
   for :data:`finalize_watch.CONFLICT_SETTLE_SECS` on end: reason
   ``conflict``. Until then the wait goes on: GitHub recomputes a PR's
   mergeable status after a push, and may report the old conflict
   meanwhile, so a conflict read just after pr-watch pushes a fix is not
   yet one (vergil-tooling#5036). A new head starts the count again, and
   any other merge state clears it. The rule is
   :class:`finalize_watch.ConflictSettle`, the one ``vrg-finalize-pr``
   applies (vergil-tooling#4998).
7. **Orphaned check:** reason ``orphaned_check``. The PR is ``BLOCKED`` and
   every pending check is stuck non-terminal after its backing workflow run
   completed (``github.orphaned_check_names``). Such a check emits no further
   event, so the PR would never merge. GitHub can write results back late
   during an Actions incident, which looks the same at the moment of checking
   (#3431), so the same pending set must stay orphaned for the grace period
   (``$VRG_ORPHAN_GRACE``, 300s by default) first.
8. **Deadline:** a PR still pending when the deadline passes is reason
   ``timed_out``, or ``held_by_release`` when the lock held it at its last
   read (``vrg-pr-await`` exits
   :data:`~vergil_tooling.lib.finalize_watch.EXIT_HELD_BY_RELEASE` for it,
   and the caller runs the same wait again). The deadline is
   ``$VRG_FINALIZE_DEADLINE`` seconds (default 7200), the epic's batch
   deadline (:func:`finalize_watch.deadline_secs`).

A new review or a new commit never ends the wait: a review after submit is
advisory and never holds the merge (spec §6.2).

Every configuration value is checked before the first read, and every failure
to read the PR raises; none is read as a pending or a clean PR.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Literal

from vergil_tooling.lib import finalize_watch, github
from vergil_tooling.lib.release_lock.model import BROKEN_WATCH_POLL_SECS

_POLL_INTERVAL = 15.0

#: Every reason a PR has not merged when the wait ends.
Reason = Literal[
    "check_failed",
    "check_cancelled",
    "conflict",
    "refused",
    "closed",
    "orphaned_check",
    "timed_out",
    "held_by_release",
]

_HELD_BY_RELEASE: Reason = "held_by_release"

_PENDING_BUCKET = "pending"
_PENDING_CONTEXT_STATES = frozenset({"PENDING", "EXPECTED"})


class PrAwaitError(RuntimeError):
    """The PR's state could not be trusted; the message names the PR."""


@dataclass(frozen=True)
class AwaitResult:
    """How the wait ended.

    *reason* is ``None`` exactly when the PR merged; *merge_commit* is then
    its merge commit. *detail* says in words why the PR cannot merge, or,
    for ``held_by_release``, which release still holds it.
    """

    pr: str
    reason: Reason | None
    detail: str
    head_sha: str
    merge_state_status: str
    merge_commit: str = ""
    failed_checks: tuple[str, ...] = field(default_factory=tuple)
    orphaned_checks: tuple[str, ...] = field(default_factory=tuple)

    @property
    def merged(self) -> bool:
        """True when the PR merged."""
        return self.reason is None

    @property
    def held(self) -> bool:
        """True when the deadline passed while a release held the PR."""
        return self.reason == _HELD_BY_RELEASE

    @property
    def outcome(self) -> str:
        """``merged``, ``held_by_release`` or ``cannot_merge``."""
        if self.merged:
            return "merged"
        return _HELD_BY_RELEASE if self.held else "cannot_merge"

    def to_output(self) -> dict[str, object]:
        """The JSON object ``vrg-pr-await`` prints."""
        return {
            "pr": self.pr,
            "outcome": self.outcome,
            "reason": self.reason,
            "detail": self.detail,
            "head_sha": self.head_sha,
            "merge_commit": self.merge_commit or None,
            "merge_state_status": self.merge_state_status,
            "failed_checks": list(self.failed_checks),
            "orphaned_checks": list(self.orphaned_checks),
        }


def _cannot_merge(
    snap: finalize_watch.Snapshot,
    reason: Reason,
    detail: str,
    *,
    failed: tuple[str, ...] = (),
    orphaned: tuple[str, ...] = (),
) -> AwaitResult:
    return AwaitResult(
        pr=snap.url,
        reason=reason,
        detail=detail,
        head_sha=snap.head,
        merge_state_status=snap.merge_state,
        failed_checks=failed,
        orphaned_checks=orphaned,
    )


def judge(
    snap: finalize_watch.Snapshot,
    required: frozenset[str] | None,
    cancel: finalize_watch.CancelGrace | None = None,
    now: float = 0.0,
    conflict: finalize_watch.ConflictSettle | None = None,
) -> AwaitResult | None:
    """*snap*'s terminal result, steps 1-6 of the module docstring, or ``None``.

    Step 3, the release lock, is not judged here: :func:`wait_for_merge`
    asks its lock rule about an open PR before calling this, and a single
    read (:func:`standing_block`) does not judge it, so auto-merge off is
    never a result. *required* ``None`` counts every check as required. *cancel* tracks the
    cancelled checks' grace period at *now*; with none, as for one read, a
    cancelled check is reported at once. *conflict* tracks how long a
    conflict has been reported at one head, at *now*; with none, as for one
    read, a conflict is reported at once.
    """
    if snap.state == "MERGED":
        if not snap.merge_commit:
            msg = f"{snap.url}: GitHub reports the PR merged but names no merge commit"
            raise PrAwaitError(msg)
        return AwaitResult(
            pr=snap.url,
            reason=None,
            detail="",
            head_sha=snap.head,
            merge_state_status=snap.merge_state,
            merge_commit=snap.merge_commit,
        )
    if snap.state == "CLOSED":
        return _cannot_merge(snap, "closed", "the PR was closed without merging")
    checks = finalize_watch.check_failures(snap, required)
    if checks.failed:
        detail = f"required check failed: {', '.join(checks.failed)}"
        return _cannot_merge(snap, "check_failed", detail, failed=checks.failed)
    held = cancel is not None and not cancel.expired(checks.cancelled, now)
    if checks.cancelled and not held:
        detail = f"required check cancelled: {', '.join(checks.cancelled)}; no newer run "
        if cancel is None:
            detail += "of its workflow has started (a re-run may still start)"
        else:
            detail += f"started within {github.format_wait(cancel.grace)}"
        return _cannot_merge(snap, "check_cancelled", detail, failed=checks.cancelled)
    conflicted = snap.merge_state == "DIRTY"
    if conflict is None:
        if conflicted:
            return _cannot_merge(snap, "conflict", "the PR has a merge conflict with its base")
    elif conflict.settled(conflicted=conflicted, head=snap.head, now=now):
        detail = (
            "the PR has a merge conflict with its base (reported at its head for "
            f"{github.format_wait(conflict.settle)})"
        )
        return _cannot_merge(snap, "conflict", detail)
    return None


def pending_check_names(snap: finalize_watch.Snapshot) -> list[str]:
    """Names of *snap*'s checks that have not concluded, in rollup order."""
    names: list[str] = []
    for node in snap.rollup:
        if node.get("__typename") == "StatusContext":
            pending = str(node.get("state") or "").upper() in _PENDING_CONTEXT_STATES
            name = str(node.get("context") or "")
        else:
            bucket = github.check_run_bucket(
                str(node.get("status") or ""), str(node.get("conclusion") or "")
            )
            pending = bucket == _PENDING_BUCKET
            name = str(node.get("name") or "")
        if pending and name not in names:
            names.append(name)
    return names


def orphaned_block(snap: finalize_watch.Snapshot) -> list[str]:
    """The pending checks when *snap*'s PR is wedged by orphaned checks, else ``[]``.

    The PR is wedged when it is ``BLOCKED``, at least one check is pending,
    and **every** pending check is an orphan. ``github.orphaned_check_names``
    leaves out app-posted statuses with no backing Actions run, so a pending
    app status genuinely still running keeps the wait going. The GitHub call
    is made only once the cheap local tests hold.
    """
    if snap.merge_state != "BLOCKED":
        return []
    pending = pending_check_names(snap)
    if not pending:
        return []
    orphans = set(github.orphaned_check_names(snap.url))
    return pending if all(name in orphans for name in pending) else []


def standing_block(pr: str) -> AwaitResult | None:
    """Why *pr* cannot merge as it stands, from one read, or ``None``.

    The one-shot form of :func:`wait_for_merge`, for a status view such as
    ``vrg-repo-status`` (vergil-tooling#4678). It judges one snapshot in the
    module docstring's order: ``None`` when the PR merged or can still merge
    (its checks pending or green), else the result naming the reason. Two
    things differ from the wait:

    - The base branch's required checks are read only when a check has
      failed, so a pending or green PR costs no extra call.
    - An orphaned or a cancelled check is reported from this one read, with
      no grace period: a single read cannot wait one out, so the detail says
      that GitHub may only be delayed, or that a re-run may still start. A
      conflict is reported from this one read too, with no settle time.

    Every failure to read the PR raises, as in :func:`wait_for_merge`.
    """
    snap = finalize_watch.snapshot(pr)
    result = judge(snap, None)
    if result is not None and result.reason in {"check_failed", "check_cancelled"}:
        result = judge(snap, github.required_check_names(snap.url))
    if result is not None:
        return None if result.merged else result
    orphaned = orphaned_block(snap)
    if orphaned:
        detail = (
            f"no result for {', '.join(orphaned)} although their workflow runs "
            "completed (orphaned check-runs, or GitHub is delayed)"
        )
        return _cannot_merge(snap, "orphaned_check", detail, orphaned=tuple(orphaned))
    return None


def wait_for_merge(
    pr: str,
    *,
    lock: finalize_watch.LockRule,
    poll_interval: float = _POLL_INTERVAL,
    broken_poll: float = BROKEN_WATCH_POLL_SECS,
    orphan_grace: float | None = None,
    cancel_grace: float = finalize_watch.CANCEL_GRACE_SECS,
    conflict_settle: float = finalize_watch.CONFLICT_SETTLE_SECS,
) -> AwaitResult:
    """Block until *pr* merges or cannot merge; see the module docstring.

    *lock* is asked about the PR on every read while it is open (step 3);
    the wait polls every *broken_poll* seconds while its view is slow
    (:attr:`finalize_watch.LockView.slow`), every *poll_interval* otherwise.
    *conflict_settle* is how long a conflict must be reported at one head
    before the wait ends with reason ``conflict``. The deadline, the grace
    period, *poll_interval* and *broken_poll* are checked before the PR is
    read: a malformed value raises :class:`ValueError`. A lock step that
    cannot be completed raises :class:`finalize_watch.FinalizeWatchError`.
    """
    deadline = finalize_watch.deadline_secs()
    grace = github.orphan_grace_secs() if orphan_grace is None else orphan_grace
    for name, value in (("poll_interval", poll_interval), ("broken_poll", broken_poll)):
        if value <= 0:
            msg = f"{name} must be positive, got {value!r}"
            raise ValueError(msg)
    snap = finalize_watch.snapshot(pr)
    required = github.required_check_names(snap.url) if snap.state == "OPEN" else None
    end = time.monotonic() + deadline
    suspected: list[str] = []
    suspected_since = 0.0
    cancel = finalize_watch.CancelGrace(cancel_grace)
    conflict = finalize_watch.ConflictSettle(conflict_settle)
    announced: str | None = None
    view = finalize_watch.FREE
    while True:
        if snap.state == "OPEN":
            # Merged and closed come first (steps 1 and 2), so only an open PR asks the lock.
            seen, view = view, lock(snap)
            if view != seen and view.detail:
                print(f"vrg-pr-await: {snap.url}: {view.detail}", file=sys.stderr)
            if view.state == "refused":
                detail = f"the one PR path refused to re-arm it: {view.detail}"
                return _cannot_merge(snap, "refused", detail)
            if view.state == "conflicted":
                return _cannot_merge(snap, "conflict", view.detail)
        # A pushed rebase's checks are the old head's: the next read judges the new head.
        pending_update = snap.state == "OPEN" and view.state == "updated"
        result = None
        if not pending_update:
            result = judge(snap, required, cancel, time.monotonic(), conflict)
        if result is not None:
            return result
        now = time.monotonic()
        if conflict.head != announced:
            announced = conflict.head
            if announced is not None:
                print(
                    f"GitHub reports a merge conflict at {announced[:12]}, which may be "
                    "the status from before a push; re-checking for up to "
                    f"{github.format_wait(conflict.settle)} before reporting it.",
                    file=sys.stderr,
                )
        orphaned = orphaned_block(snap)
        if not orphaned:
            suspected = []
        else:
            if orphaned != suspected:
                suspected, suspected_since = orphaned, now
                print(
                    f"{len(orphaned)} check-run(s) have a completed run but no result yet "
                    f"({', '.join(orphaned)}) — GitHub may be delayed; re-checking for up "
                    f"to {github.format_wait(grace)} before reporting them orphaned.",
                    file=sys.stderr,
                )
            if now - suspected_since >= grace:
                detail = (
                    f"no result for {', '.join(orphaned)} after "
                    f"{github.format_wait(now - suspected_since)} although their "
                    "workflow runs completed (orphaned check-runs)"
                )
                return _cannot_merge(snap, "orphaned_check", detail, orphaned=tuple(orphaned))
        remaining = end - now
        if remaining <= 0:
            at = f"at the {deadline}s deadline ({finalize_watch.DEADLINE_ENV})"
            if view.held:
                detail = f"still {view.detail} {at}; run the wait again"
                return _cannot_merge(snap, _HELD_BY_RELEASE, detail)
            if view.state == "retrying":
                # Its auto-merge is off (the re-arm was refused), so it does not stay on.
                detail = f"still {view.detail} {at}; run the wait again"
                return _cannot_merge(snap, "timed_out", detail)
            return _cannot_merge(snap, "timed_out", f"still pending {at}; its auto-merge stays on")
        time.sleep(min(broken_poll if view.slow else poll_interval, remaining))
        snap = finalize_watch.snapshot(snap.url)
