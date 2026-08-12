"""Settle-predicate waiter for the post-PR loop (§9 of the 2.1 workflow design).

There is no webhook ingress on a laptop, so ``vrg-pr-await`` polls the GitHub
API. It blocks until the PR *settles*: all checks reach a terminal conclusion,
**or** a new commit appears (the head SHA moves), **or** a new review appears,
**or** the PR is wedged by an *orphaned check-run* (every still-pending check is
stuck non-terminal after its backing workflow run already completed, while
``mergeStateStatus`` is ``BLOCKED``). On settle it returns the observed state so
the wrapping skill can reconcile.

The orphaned-check settle is the one that keeps the loop from hanging forever:
an orphaned check-run emits no further event, so waiting for a state transition
that will never arrive would block indefinitely. Detecting it lets ``pr-watch``
surface the condition (close/reopen the PR to re-run the gate) instead of
spinning silently. The detection reuses ``github.orphaned_check_names`` — the
same signal ``vrg-finalize-pr`` uses — rather than re-deriving it.

A merged PR can never settle into anything actionable — continued polling
would just spin as an orphaned watcher — so every poll (including the first)
checks the PR's state and raises :class:`PrMergedError` when it is merged.
A merge observed mid-watch means the audit cycle was bypassed; failing loudly
surfaces the short-circuit instead of silently spinning.

The "is it settled?" decision lives here in deterministic code, not in agent
tokens — the agent only acts on the returned verdict.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from vergil_tooling.lib import github

_POLL_INTERVAL = 15.0

_FAILED_BUCKETS = frozenset({"fail", "cancel"})
_PENDING_BUCKET = "pending"
_MERGED_STATE = "MERGED"
_BLOCKED_STATE = "BLOCKED"
_ORPHANED_CHECK_REASON = "orphaned_check"


class PrMergedError(Exception):
    """The watched PR is already merged, so the watch can never settle."""

    def __init__(self, pr: str) -> None:
        self.pr = pr
        super().__init__(f"PR {pr} is already merged; aborting watch")


@dataclass(frozen=True)
class PrState:
    """A snapshot of the PR's gating state at one poll."""

    head_sha: str
    checks: list[dict[str, str]] = field(default_factory=list)
    reviews: list[dict[str, object]] = field(default_factory=list)
    pr_state: str = "OPEN"
    merge_state_status: str = ""

    @property
    def merged(self) -> bool:
        """True when the PR has been merged."""
        return self.pr_state == _MERGED_STATE

    @property
    def blocked(self) -> bool:
        """True when the PR's ``mergeStateStatus`` is ``BLOCKED``."""
        return self.merge_state_status == _BLOCKED_STATE

    @property
    def has_checks(self) -> bool:
        """True when at least one check has been registered for the head SHA."""
        return len(self.checks) > 0

    @property
    def checks_pending(self) -> bool:
        """True when any check is still running (bucket ``pending``)."""
        return any(c.get("bucket") == _PENDING_BUCKET for c in self.checks)

    @property
    def pending_check_names(self) -> list[str]:
        """Names of checks still in the ``pending`` bucket."""
        return [str(c["name"]) for c in self.checks if c.get("bucket") == _PENDING_BUCKET]

    @property
    def failed_checks(self) -> list[str]:
        """Names of checks whose bucket is ``fail`` or ``cancel``."""
        return [str(c["name"]) for c in self.checks if c.get("bucket") in _FAILED_BUCKETS]

    @property
    def all_checks_passed(self) -> bool:
        """True when checks exist and none are pending or failed."""
        return self.has_checks and not self.checks_pending and not self.failed_checks


def gather_state(pr: str) -> PrState:
    """Poll the GitHub API once for the PR's head SHA, checks, reviews, and state."""
    return PrState(
        head_sha=github.head_sha(pr),
        checks=github.pr_checks(pr),
        reviews=github.pr_reviews(pr),
        pr_state=github.pr_state(pr),
        merge_state_status=github.merge_state_status(pr),
    )


def settle_reason(
    state: PrState,
    *,
    since_sha: str | None,
    since_reviews: int | None,
) -> str | None:
    """Return why the PR has settled, or ``None`` if it has not.

    Priority order: a new commit invalidates everything downstream, so it wins
    over a new review, which wins over checks merely reaching terminal state.
    """
    if since_sha is not None and state.head_sha != since_sha:
        return "new_commit"
    if since_reviews is not None and len(state.reviews) > since_reviews:
        return "new_review"
    if state.has_checks and not state.checks_pending:
        return "checks_terminal"
    return None


def is_orphaned_block(state: PrState, pr: str) -> bool:
    """True when *pr* is wedged by an orphaned check-run and would hang the watch.

    The condition is: ``mergeStateStatus`` is ``BLOCKED``, at least one check is
    still pending, and **every** pending check is an orphan — stuck non-terminal
    after its backing workflow run already completed. Only then is continued
    polling futile: an orphaned check emits no further event, so waiting for a
    transition that will never arrive blocks forever.

    The orphan set comes from ``github.orphaned_check_names`` (the same detector
    ``vrg-finalize-pr`` uses). It excludes app-posted statuses that have no
    backing Actions run, so a pending app status genuinely still running is
    *not* an orphan — the ``all(...)`` guard then keeps the watch waiting rather
    than declaring a false orphaned settle. The GitHub API call is made only
    once the cheap local predicates (blocked + pending) already hold.
    """
    if not state.blocked:
        return False
    pending = state.pending_check_names
    if not pending:
        return False
    orphans = set(github.orphaned_check_names(pr))
    return all(name in orphans for name in pending)


def wait_for_settle(
    pr: str,
    *,
    since_sha: str | None,
    since_reviews: int | None,
    poll_interval: float = _POLL_INTERVAL,
) -> tuple[PrState, str]:
    """Block until the PR settles; return the settled state and the reason.

    Raises :class:`PrMergedError` as soon as any poll (including the first)
    observes the PR merged — a merged PR aborts the watch even when a settle
    reason exists, since no settle verdict on a merged PR is actionable.

    Beyond the pure :func:`settle_reason` verdicts, a poll also settles with
    reason ``"orphaned_check"`` when :func:`is_orphaned_block` holds — the PR is
    ``BLOCKED`` and every pending check is an orphaned check-run. That settle
    exists so the watch reports the wedge instead of hanging forever on an event
    that will never fire.
    """
    while True:
        state = gather_state(pr)
        if state.merged:
            raise PrMergedError(pr)
        reason = settle_reason(state, since_sha=since_sha, since_reviews=since_reviews)
        if reason is not None:
            return state, reason
        if is_orphaned_block(state, pr):
            return state, _ORPHANED_CHECK_REASON
        time.sleep(poll_interval)


def to_output(state: PrState, reason: str) -> dict[str, object]:
    """Build the JSON-serializable result emitted by ``vrg-pr-await``.

    On an ``"orphaned_check"`` settle, ``orphaned_checks`` names the wedged
    check-runs so ``pr-watch`` can report exactly which gates are stuck. Those
    are the still-pending checks: :func:`is_orphaned_block` only returns the
    reason when every pending check is an orphan, so the pending set *is* the
    orphan set. For every other reason the list is empty.
    """
    orphaned_checks = state.pending_check_names if reason == _ORPHANED_CHECK_REASON else []
    return {
        "reason": reason,
        "head_sha": state.head_sha,
        "review_count": len(state.reviews),
        "checks": state.checks,
        "failed_checks": state.failed_checks,
        "all_checks_passed": state.all_checks_passed,
        "merge_state_status": state.merge_state_status,
        "orphaned_checks": orphaned_checks,
    }
