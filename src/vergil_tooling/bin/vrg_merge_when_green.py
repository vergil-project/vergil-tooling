"""Poll a PR's checks, then merge it when they all pass.

Wraps ``gh pr checks --watch --fail-fast`` with an outer loop that detects
when the PR branch is behind its base or has merge conflicts. When behind,
auto-updates the branch (fast-forward merge from base) and re-polls CI.
Fails fast on merge conflicts.

Designed for release-workflow PRs where the agent is both author and
reviewer and there is no human to gate the merge. For normal PRs, leave
them to manual merge.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import github
from vergil_tooling.lib.release import is_release_branch

_STRATEGIES = ("merge", "squash", "rebase")
_MAX_BRANCH_UPDATES = 5


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Wait for a PR's checks to pass, then merge it.",
    )
    parser.add_argument("pr", help="PR URL or number")
    parser.add_argument(
        "--strategy",
        choices=_STRATEGIES,
        default="merge",
        help="Merge strategy (default: merge)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    branch = github.read_output(
        "pr",
        "view",
        args.pr,
        "--json",
        "headRefName",
        "--jq",
        ".headRefName",
    )
    if not is_release_branch(branch):
        print(
            f"Error: vrg-merge-when-green is only for release-workflow PRs. "
            f"Branch '{branch}' does not start with release/*.",
            file=sys.stderr,
        )
        return 1

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
    print(f"Checks passed. Merging with --{args.strategy}...")
    github.merge(args.pr, strategy=args.strategy)
    print("Merged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
