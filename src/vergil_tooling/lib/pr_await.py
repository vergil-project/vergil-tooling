"""Merge waiter behind ``vrg-pr-await`` (epic vergil-project/.github#1218, follow-up F5).

``vrg-submit-pr`` opens each PR with auto-merge on, and GitHub merges it once
its required checks pass. :func:`wait_for_merge` waits on one such PR until it
merges, until it can no longer merge as it stands, or until the deadline
passes. It never merges a PR, never turns auto-merge on or off, and changes
nothing locally.

There is no webhook ingress on a laptop, so it polls: one
:func:`vergil_tooling.lib.finalize_watch.snapshot` read per interval, the same
read ``vrg-finalize-pr``'s watch phase judges. Each read is judged in this
order:

1. **Merged:** the wait ends in success, with the merge commit. A PR that
   merged before the wait started counts too.
2. **Closed** without merging: reason ``closed``.
3. **Auto-merge off:** reason ``auto_merge_off``. Auto-merge stays pinned to
   the head submit pushed, so a push that turns it off ends the wait here.
4. **A required check concluded in failure:** reason ``check_failed``,
   naming the checks, judged by
   :func:`vergil_tooling.lib.finalize_watch.check_failures` as the finalize
   watch judges them: a failed or cancelled check-run of a run that a newer
   run of its workflow superseded on the same head is not a failure (#4692).
5. **A required check was cancelled** on a cancelled run that no newer run
   replaced, for :data:`finalize_watch.CANCEL_GRACE_SECS` on end: reason
   ``check_cancelled``, naming the checks in ``failed_checks``. Until then
   the wait goes on, since a re-run may still start.
6. **Conflict** (``mergeStateStatus`` ``DIRTY``): reason ``conflict``.
7. **Orphaned check:** reason ``orphaned_check``. The PR is ``BLOCKED`` and
   every pending check is stuck non-terminal after its backing workflow run
   completed (``github.orphaned_check_names``). Such a check emits no further
   event, so the PR would never merge. GitHub can write results back late
   during an Actions incident, which looks the same at the moment of checking
   (#3431), so the same pending set must stay orphaned for the grace period
   (``$VRG_ORPHAN_GRACE``, 300s by default) first.
8. **Deadline:** a PR still pending when the deadline passes is reason
   ``timed_out``. The deadline is ``$VRG_FINALIZE_DEADLINE`` seconds (default
   7200), the epic's batch deadline (:func:`finalize_watch.deadline_secs`).

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

_POLL_INTERVAL = 15.0

#: Every reason a PR cannot merge as it stands.
Reason = Literal[
    "check_failed",
    "check_cancelled",
    "conflict",
    "auto_merge_off",
    "closed",
    "orphaned_check",
    "timed_out",
]

_PENDING_BUCKET = "pending"
_PENDING_CONTEXT_STATES = frozenset({"PENDING", "EXPECTED"})


class PrAwaitError(RuntimeError):
    """The PR's state could not be trusted; the message names the PR."""


@dataclass(frozen=True)
class AwaitResult:
    """How the wait ended.

    *reason* is ``None`` exactly when the PR merged; *merge_commit* is then
    its merge commit. *detail* says in words why the PR cannot merge.
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

    def to_output(self) -> dict[str, object]:
        """The JSON object ``vrg-pr-await`` prints."""
        return {
            "pr": self.pr,
            "outcome": "merged" if self.merged else "cannot_merge",
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
) -> AwaitResult | None:
    """*snap*'s terminal result, steps 1-6 of the module docstring, or ``None``.

    *required* ``None`` counts every check as required. *cancel* tracks the
    cancelled checks' grace period at *now*; with none, as for one read, a
    cancelled check is reported at once.
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
    if not snap.auto_merge_on:
        return _cannot_merge(snap, "auto_merge_off", "auto-merge is off for the PR")
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
    if snap.merge_state == "DIRTY":
        return _cannot_merge(snap, "conflict", "the PR has a merge conflict with its base")
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
      that GitHub may only be delayed, or that a re-run may still start.

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
    poll_interval: float = _POLL_INTERVAL,
    orphan_grace: float | None = None,
    cancel_grace: float = finalize_watch.CANCEL_GRACE_SECS,
) -> AwaitResult:
    """Block until *pr* merges or cannot merge; see the module docstring.

    The deadline, the grace period and *poll_interval* are checked before the
    PR is read: a malformed value raises :class:`ValueError`.
    """
    deadline = finalize_watch.deadline_secs()
    grace = github.orphan_grace_secs() if orphan_grace is None else orphan_grace
    if poll_interval <= 0:
        msg = f"poll_interval must be positive, got {poll_interval!r}"
        raise ValueError(msg)
    snap = finalize_watch.snapshot(pr)
    required = github.required_check_names(snap.url) if snap.state == "OPEN" else None
    end = time.monotonic() + deadline
    suspected: list[str] = []
    suspected_since = 0.0
    cancel = finalize_watch.CancelGrace(cancel_grace)
    while True:
        result = judge(snap, required, cancel, time.monotonic())
        if result is not None:
            return result
        now = time.monotonic()
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
            detail = (
                f"still pending at the {deadline}s deadline ({finalize_watch.DEADLINE_ENV}); "
                "its auto-merge stays on"
            )
            return _cannot_merge(snap, "timed_out", detail)
        time.sleep(min(poll_interval, remaining))
        snap = finalize_watch.snapshot(snap.url)
