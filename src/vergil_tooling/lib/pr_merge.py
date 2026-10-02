"""Shared wait-and-merge engine with fail-fast ordering.

Used by ``vrg-finalize-pr`` (squash by default) and the release
workflow (merge strategy). Doomed outcomes — already merged, draft,
conflicting, behind — are checked *before* waiting, never after
letting a pointless CI run finish:

- MERGED: the caller's premise is wrong. What "already merged" means
  is a caller-level decision (finalize pre-checks and skips to
  cleanup; ``vrg-pr-await`` aborts per #1420), so the engine raises.
- Draft: can go green but ``gh pr merge`` refuses it.
- CONFLICTING: cannot merge no matter what CI says. Re-checked every
  iteration — a conflict can arise mid-loop when another PR merges.
- BEHIND: the current CI run is irrelevant; update-branch cancels it
  and starts a fresh one, so update immediately instead of waiting.

The BEHIND precheck reads ``mergeStateStatus``, which GitHub computes lazily
and serves stale for a window after the base branch advances — so in a merge
train (serial batch finalize, issue #1673) a freshly-behind branch can pass the
precheck and only be caught by the merge endpoint itself, which rejects with
"the head branch is not up to date with the base branch." That authoritative
rejection is fed back into the same update-and-retry path rather than surfaced
as a hard failure (issue #2856).

"Green" means every registered check is terminal **and** every required status
check of the base branch is registered and terminal (issue #3061). CI aggregator
jobs (``test / evidence`` …) only register once their dependencies finish, so
there is a window where every *registered* check is done yet a required one does
not exist; merging there is refused with "the base branch policy prohibits the
merge". The engine resolves the required set once, keeps waiting while any
required check is outstanding, and treats that policy rejection with no failed
checks as *not ready* — poll and re-attempt — rather than fatal, bounded by the
shared ``_POLL_TIMEOUT_SECS`` deadline. A review requirement is surfaced at once.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import github

# Imported directly (not via the module) so the `except` clause holds the
# real class even when tests replace the whole `github` module with a mock.
from vergil_tooling.lib.github import (
    _POLL_INTERVAL_SECS,
    _POLL_TIMEOUT_SECS,
    GitHubAPIError,
    OrphanedCheckError,
    describe_outstanding,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_BRANCH_UPDATES = 5
_UPDATE_SETTLE_SECS = 5
# Poll cadence and ceiling while the PR is "not ready" — required checks not yet
# registered/terminal, or a branch-policy rejection with no failed checks (#3061).
# The ceiling is the shared pending-checks deadline so the waits never drift.
_NOT_READY_POLL_SECS = _POLL_INTERVAL_SECS
_NOT_READY_TIMEOUT_SECS = _POLL_TIMEOUT_SECS
# reviewDecision values that mean branch policy wants a human review — a block
# no amount of waiting on CI clears, so it is surfaced immediately.
_REVIEW_BLOCKS = frozenset({"REVIEW_REQUIRED", "CHANGES_REQUESTED"})


class MergeAbortError(Exception):
    """The PR cannot be merged; the message explains why and what to do."""


# Substring of the merge endpoint's rejection when the repo requires branches to
# be up to date and the head is behind base. This is the authoritative signal the
# lazily-computed mergeStateStatus precheck can miss in a merge train (issue #2856).
_BEHIND_REJECTION_SIGNATURE = "not up to date"


def _is_behind_rejection(exc: GitHubAPIError) -> bool:
    """True when a merge rejection means the head branch is behind base."""
    return _BEHIND_REJECTION_SIGNATURE in (exc.stderr or "").lower()


# Substring of the merge endpoint's rejection when a branch-protection or ruleset
# requirement is unmet — most often a required check that has not registered or
# finished yet, but also e.g. a required review (issue #3061).
_POLICY_REJECTION_SIGNATURE = "base branch policy prohibits the merge"


def _is_policy_rejection(exc: GitHubAPIError) -> bool:
    """True when a merge rejection is the generic base-branch-policy block."""
    return _POLICY_REJECTION_SIGNATURE in (exc.stderr or "").lower()


def wait_and_merge(
    pr: str,
    *,
    strategy: str,
    wait_checks: Callable[[str], None] | None = None,
) -> None:
    """Block until *pr* is green and current, then merge it.

    ``wait_checks`` lets callers substitute their own check-waiting
    primitive (the release workflow passes its verbose-aware wrapper);
    the default is ``github.wait_for_checks``.

    The base branch's required status checks are resolved once up front (a
    lookup failure warns and falls back to registered checks only). Whatever
    waiter runs, the engine re-checks the required set afterwards, so a custom
    waiter that only watches registered checks still cannot merge early.

    Raises ``MergeAbortError`` on any unmergeable condition.
    """
    resolved = github.required_check_names(pr)
    required: frozenset[str] = resolved if resolved is not None else frozenset()
    if wait_checks is not None:
        wait = wait_checks
    else:

        def wait(p: str) -> None:
            github.wait_for_checks(p, required=required)

    updates = 0
    not_ready_deadline: float | None = None

    def _await_not_ready(reason: str) -> None:
        """Pause one poll while the PR is not ready, or abort once the deadline passes.

        The deadline starts at the first not-ready observation and is shared by
        every later one in this call, so the retry loop is always bounded.
        """
        nonlocal not_ready_deadline
        now = time.monotonic()
        if not_ready_deadline is None:
            not_ready_deadline = now + _NOT_READY_TIMEOUT_SECS
        elif now >= not_ready_deadline:
            msg = (
                f"PR {pr} is still not mergeable after {_NOT_READY_TIMEOUT_SECS}s: {reason}. "
                "Check the base branch's protection rules and rulesets, then re-run."
            )
            raise MergeAbortError(msg)
        print(f"Not ready to merge — {reason}; re-checking in {_NOT_READY_POLL_SECS}s...")
        time.sleep(_NOT_READY_POLL_SECS)

    def _policy_block_reason() -> str:
        """Explain a base-branch-policy rejection, or abort if it cannot clear by waiting.

        A failed check or a required review will not clear by polling, so both
        abort now; otherwise the rejection is "not ready" and the reason names
        what is still awaited.
        """
        failed = github.failed_check_names(pr)
        if failed:
            msg = f"Checks failed on PR {pr}: {', '.join(failed)}"
            raise MergeAbortError(msg)
        status = github.merge_status(pr)
        review = status.get("reviewDecision", "")
        if review in _REVIEW_BLOCKS:
            msg = (
                f"PR {pr} is blocked by base branch policy: reviewDecision={review}. "
                "It needs an approving review before it can merge."
            )
            raise MergeAbortError(msg)
        prefix = "the base branch policy prohibits the merge"
        outstanding = github.required_checks_outstanding(pr, required)
        if outstanding:
            return f"{prefix}; awaiting required checks: {describe_outstanding(outstanding)}"
        if resolved is None:
            return (
                f"{prefix} with no failed checks; the required checks could not be "
                "read (see warning above)"
            )
        state = status.get("mergeStateStatus", "")
        return (
            f"{prefix} with no failed checks and no outstanding required checks "
            f"(mergeStateStatus={state or 'unknown'})"
        )

    def _update_or_abort() -> None:
        """Update the behind branch, or abort once the merge-train cap is hit.

        Shared by the mergeStateStatus precheck and the merge endpoint's own
        stale-behind rejection so both are bounded by one branch-update cap.
        """
        nonlocal updates
        updates += 1
        if updates > _MAX_BRANCH_UPDATES:
            msg = (
                f"PR {pr} still behind after {_MAX_BRANCH_UPDATES} branch updates "
                "— the merge train is busy; re-run when it settles."
            )
            raise MergeAbortError(msg)
        print("Branch is behind base — updating and re-checking...")
        try:
            github.update_branch(pr)
        except GitHubAPIError as exc:
            msg = f"update-branch failed for PR {pr}: {exc}"
            raise MergeAbortError(msg) from exc
        time.sleep(_UPDATE_SETTLE_SECS)

    while True:
        if github.pr_state(pr) == "MERGED":
            msg = (
                f"PR {pr} is already merged — nothing to wait for. "
                "If cleanup is what remains, run vrg-finalize-pr without arguments."
            )
            raise MergeAbortError(msg)
        if github.is_draft(pr):
            msg = f"PR {pr} is a draft — mark it ready (gh pr ready {pr}) and re-run."
            raise MergeAbortError(msg)
        if github.mergeable(pr) == "CONFLICTING":
            msg = (
                f"PR {pr} has merge conflicts. Resolve them in the PR's worktree "
                "(merge the target branch in, push), then re-run."
            )
            raise MergeAbortError(msg)
        if github.merge_state_status(pr) == "BEHIND":
            _update_or_abort()
            continue

        print(f"Waiting for checks on {pr}...")
        try:
            wait(pr)
        except OrphanedCheckError as exc:
            msg = (
                f"PR {pr} cannot be merged: GitHub left a check-run non-terminal "
                "after its backing workflow run completed (orphaned check-run). "
                "Close and reopen the PR to re-run the gate, then re-run "
                "vrg-finalize-pr."
            )
            raise MergeAbortError(msg) from exc

        failed = github.failed_check_names(pr)
        if failed:
            msg = f"Checks failed on PR {pr}: {', '.join(failed)}"
            raise MergeAbortError(msg)

        # A waiter that watches only registered checks (the release workflow's
        # streaming one) can return while a required aggregator check has not
        # even registered yet — keep waiting rather than merge into a policy
        # rejection (#3061).
        outstanding = github.required_checks_outstanding(pr, required)
        if outstanding:
            _await_not_ready(f"awaiting required checks: {describe_outstanding(outstanding)}")
            continue

        if github.merge_state_status(pr) == "BEHIND":
            continue  # something merged while we waited -> update at loop top

        print(f"Checks passed. Merging {pr} (--{strategy})...")
        try:
            github.merge(pr, strategy=strategy)
        except GitHubAPIError as exc:
            # The merge endpoint enforces "require branches up to date" itself.
            # A stale-behind rejection here is the authoritative signal the lazy
            # mergeStateStatus precheck missed (issue #2856) — feed it back into
            # the same update-and-retry path.
            if _is_behind_rejection(exc):
                _update_or_abort()
                continue
            # A branch-policy rejection with no failed checks means "not ready
            # yet" (e.g. a required check still registering), not fatal: poll and
            # re-attempt within the deadline (#3061). Any other failure is real.
            if not _is_policy_rejection(exc):
                raise
            _await_not_ready(_policy_block_reason())
            continue
        print("Merged.")
        return
