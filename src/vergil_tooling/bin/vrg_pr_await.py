"""Block until a pull request settles, then print its state as JSON.

``vrg-pr-await <PR> [--since-sha <sha>] [--since-reviews <n>]`` is the post-PR
counterpart to ``vrg-await`` (§9 of the Vergil 2.1 workflow design). It polls
the GitHub API and returns when the PR settles: all checks reach a terminal
conclusion, **or** the head SHA moves (a new commit), **or** a new review
appears. The baselines let the caller thread "what I last saw" so a settle on
a fresh commit/review is detected even when checks are already terminal.

On settle it prints a JSON object (``reason``, ``head_sha``, ``review_count``,
``checks``, ``failed_checks``, ``all_checks_passed``) for the wrapping skill to
reconcile, and exits 0. Like ``vrg-await`` it blocks patiently and
indefinitely — a wait that never returns means the PR has not changed.

If any poll (including the first) finds the PR already merged, it aborts with
an error and exits 1: a merged PR can never settle into actionable output, and
a merge observed mid-watch means the audit cycle was bypassed — failing loudly
surfaces the short-circuit instead of leaving an orphaned poll loop running.
"""

from __future__ import annotations

import argparse
import json
import sys

from vergil_tooling.lib import pr_await


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Block until a PR settles (checks terminal, or new commit/review).",
    )
    parser.add_argument("pr", help="PR URL or number")
    parser.add_argument(
        "--since-sha",
        default=None,
        help="Last seen head SHA; settle immediately if the head has moved",
    )
    parser.add_argument(
        "--since-reviews",
        type=int,
        default=None,
        help="Number of reviews last seen; settle when a new review appears",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        state, reason = pr_await.wait_for_settle(
            args.pr,
            since_sha=args.since_sha,
            since_reviews=args.since_reviews,
        )
    except pr_await.PrMergedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(pr_await.to_output(state, reason), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
