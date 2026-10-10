"""Block until a PR's required checks pass and the branch is up to date.

Wraps the shared bounded check waiter (``github.wait_for_checks``, deadline
``$VRG_CHECKS_TIMEOUT`` or 1800s, stale-check-run aware per #3170) with an outer
loop that detects when the PR branch is behind its base. When the branch is
behind, auto-updates it (fast-forward merge from base) and re-polls CI.

Waiting only establishes that checks are terminal, not that they passed, so
success is determined authoritatively from the check conclusions
(``failed_check_names``) and only then from ``mergeStateStatus``. The caller
sees a zero exit only when every check passed and the PR is mergeable
(``CLEAN``).
"""

from __future__ import annotations

import argparse
import sys
import time

from vergil_tooling.lib import github

_MAX_BRANCH_UPDATES = 5
_MAX_UNKNOWN_RETRIES = 3
_UNKNOWN_RETRY_DELAY = 5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Block until a PR's required checks pass.",
    )
    parser.add_argument("pr", help="PR URL or number")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    updates = 0
    while True:
        if github.mergeable(args.pr) == "CONFLICTING":
            print(
                "Error: PR has merge conflicts. Rebase or merge the base branch before continuing.",
                file=sys.stderr,
            )
            return 1
        print(f"Waiting for checks to pass on {args.pr}...")
        github.wait_for_checks(args.pr)
        if github.merge_state_status(args.pr) != "BEHIND":
            break
        updates += 1
        if updates > _MAX_BRANCH_UPDATES:
            print(
                "Branch still behind after multiple updates — giving up.",
                file=sys.stderr,
            )
            return 1
        print("Branch is behind base — updating and re-checking...")
        github.update_branch(args.pr)
    failed = github.failed_check_names(args.pr)
    if failed:
        print(
            "Checks failed: " + ", ".join(sorted(failed)) + ".",
            file=sys.stderr,
        )
        return 1
    for attempt in range(_MAX_UNKNOWN_RETRIES + 1):
        status = github.merge_status(args.pr)
        state = status["mergeStateStatus"]
        if state != "UNKNOWN":
            break
        if attempt < _MAX_UNKNOWN_RETRIES:
            print(
                f"Mergeable state is UNKNOWN (transient) — retrying in {_UNKNOWN_RETRY_DELAY}s...",
            )
            time.sleep(_UNKNOWN_RETRY_DELAY)
    if state == "CLEAN":
        print("All checks passed.")
        return 0
    print(
        f"All checks passed, but PR is not mergeable ({state}).",
        file=sys.stderr,
    )
    if state == "BLOCKED":
        review = status["reviewDecision"]
        if review in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            print(f"  Review status: {review}", file=sys.stderr)
        print("  Check branch protection settings.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
