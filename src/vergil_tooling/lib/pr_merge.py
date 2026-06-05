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
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import github

# Imported directly (not via the module) so the `except` clause holds the
# real class even when tests replace the whole `github` module with a mock.
from vergil_tooling.lib.github import GitHubAPIError

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_BRANCH_UPDATES = 5
_UPDATE_SETTLE_SECS = 5


class MergeAbort(Exception):
    """The PR cannot be merged; the message explains why and what to do."""


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

    Raises ``MergeAbort`` on any unmergeable condition.
    """
    wait = wait_checks if wait_checks is not None else github.wait_for_checks
    updates = 0
    while True:
        if github.pr_state(pr) == "MERGED":
            msg = (
                f"PR {pr} is already merged — nothing to wait for. "
                "If cleanup is what remains, run vrg-finalize-pr without arguments."
            )
            raise MergeAbort(msg)
        if github.is_draft(pr):
            msg = f"PR {pr} is a draft — mark it ready (gh pr ready {pr}) and re-run."
            raise MergeAbort(msg)
        if github.mergeable(pr) == "CONFLICTING":
            msg = (
                f"PR {pr} has merge conflicts. Resolve them in the PR's worktree "
                "(merge the target branch in, push), then re-run."
            )
            raise MergeAbort(msg)
        if github.merge_state_status(pr) == "BEHIND":
            updates += 1
            if updates > _MAX_BRANCH_UPDATES:
                msg = (
                    f"PR {pr} still behind after {_MAX_BRANCH_UPDATES} branch updates "
                    "— the merge train is busy; re-run when it settles."
                )
                raise MergeAbort(msg)
            print("Branch is behind base — updating and re-checking...")
            try:
                github.update_branch(pr)
            except GitHubAPIError as exc:
                msg = f"update-branch failed for PR {pr}: {exc}"
                raise MergeAbort(msg) from exc
            time.sleep(_UPDATE_SETTLE_SECS)
            continue

        print(f"Waiting for checks on {pr}...")
        wait(pr)

        failed = github.failed_check_names(pr)
        if failed:
            msg = f"Checks failed on PR {pr}: {', '.join(failed)}"
            raise MergeAbort(msg)

        if github.merge_state_status(pr) == "BEHIND":
            continue  # something merged while we waited -> update at loop top
        break

    print(f"Checks passed. Merging {pr} (--{strategy})...")
    github.merge(pr, strategy=strategy)
    print("Merged.")
