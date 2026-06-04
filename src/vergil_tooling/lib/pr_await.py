"""Settle-predicate waiter for the post-PR loop (§9 of the 2.1 workflow design).

There is no webhook ingress on a laptop, so ``vrg-pr-await`` polls the GitHub
API. It blocks until the PR *settles*: all checks reach a terminal conclusion,
**or** a new commit appears (the head SHA moves), **or** a new review appears.
On settle it returns the observed state so the wrapping skill can reconcile.

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


@dataclass(frozen=True)
class PrState:
    """A snapshot of the PR's gating state at one poll."""

    head_sha: str
    checks: list[dict[str, str]] = field(default_factory=list)
    reviews: list[dict[str, object]] = field(default_factory=list)

    @property
    def has_checks(self) -> bool:
        """True when at least one check has been registered for the head SHA."""
        return len(self.checks) > 0

    @property
    def checks_pending(self) -> bool:
        """True when any check is still running (bucket ``pending``)."""
        return any(c.get("bucket") == _PENDING_BUCKET for c in self.checks)

    @property
    def failed_checks(self) -> list[str]:
        """Names of checks whose bucket is ``fail`` or ``cancel``."""
        return [str(c["name"]) for c in self.checks if c.get("bucket") in _FAILED_BUCKETS]

    @property
    def all_checks_passed(self) -> bool:
        """True when checks exist and none are pending or failed."""
        return self.has_checks and not self.checks_pending and not self.failed_checks


def gather_state(pr: str) -> PrState:
    """Poll the GitHub API once for the PR's head SHA, checks, and reviews."""
    return PrState(
        head_sha=github.head_sha(pr),
        checks=github.pr_checks(pr),
        reviews=github.pr_reviews(pr),
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


def wait_for_settle(
    pr: str,
    *,
    since_sha: str | None,
    since_reviews: int | None,
    poll_interval: float = _POLL_INTERVAL,
) -> tuple[PrState, str]:
    """Block until the PR settles; return the settled state and the reason."""
    while True:
        state = gather_state(pr)
        reason = settle_reason(state, since_sha=since_sha, since_reviews=since_reviews)
        if reason is not None:
            return state, reason
        time.sleep(poll_interval)


def to_output(state: PrState, reason: str) -> dict[str, object]:
    """Build the JSON-serializable result emitted by ``vrg-pr-await``."""
    return {
        "reason": reason,
        "head_sha": state.head_sha,
        "review_count": len(state.reviews),
        "checks": state.checks,
        "failed_checks": state.failed_checks,
        "all_checks_passed": state.all_checks_passed,
    }
