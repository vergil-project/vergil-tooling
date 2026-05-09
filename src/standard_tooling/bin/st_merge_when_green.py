"""Poll a PR's checks, then merge it when they all pass.

Intentionally dumb: surfaces any check failure to the caller with a non-zero
exit code. The caller is responsible for deciding what to do with a failure.

Designed for release-workflow PRs where the agent is both author and
reviewer and there is no human to gate the merge. For normal PRs, leave
them to manual merge.
"""

from __future__ import annotations

import argparse
import sys

from standard_tooling.lib import github
from standard_tooling.lib.release import is_release_branch

_STRATEGIES = ("merge", "squash", "rebase")


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
            f"Error: st-merge-when-green is only for release-workflow PRs. "
            f"Branch '{branch}' does not start with release/*.",
            file=sys.stderr,
        )
        return 1

    print(f"Waiting for checks to pass on {args.pr}...")
    github.wait_for_checks(args.pr)
    print(f"Checks passed. Merging with --{args.strategy}...")
    github.merge(args.pr, strategy=args.strategy)
    print("Merged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
