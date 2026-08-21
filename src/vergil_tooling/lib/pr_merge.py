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
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import github

# Imported directly (not via the module) so the `except` clause holds the
# real class even when tests replace the whole `github` module with a mock.
from vergil_tooling.lib.github import GitHubAPIError, OrphanedCheckError

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_BRANCH_UPDATES = 5
_UPDATE_SETTLE_SECS = 5


class MergeAbortError(Exception):
    """The PR cannot be merged; the message explains why and what to do."""


# Substring of the merge endpoint's rejection when the repo requires branches to
# be up to date and the head is behind base. This is the authoritative signal the
# lazily-computed mergeStateStatus precheck can miss in a merge train (issue #2856).
_BEHIND_REJECTION_SIGNATURE = "not up to date"


def _is_behind_rejection(exc: GitHubAPIError) -> bool:
    """True when a merge rejection means the head branch is behind base."""
    return _BEHIND_REJECTION_SIGNATURE in (exc.stderr or "").lower()


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

    Raises ``MergeAbortError`` on any unmergeable condition.
    """
    wait = wait_checks if wait_checks is not None else github.wait_for_checks
    updates = 0

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

        if github.merge_state_status(pr) == "BEHIND":
            continue  # something merged while we waited -> update at loop top

        print(f"Checks passed. Merging {pr} (--{strategy})...")
        try:
            github.merge(pr, strategy=strategy)
        except GitHubAPIError as exc:
            # The merge endpoint enforces "require branches up to date" itself.
            # A stale-behind rejection here is the authoritative signal the lazy
            # mergeStateStatus precheck missed (issue #2856) — feed it back into
            # the same update-and-retry path. Any other failure is real; re-raise.
            if not _is_behind_rejection(exc):
                raise
            _update_or_abort()
            continue
        print("Merged.")
        return
