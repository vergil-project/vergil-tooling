"""Extract the release tracking issue number from a PR on main.

Given a commit (typically HEAD on main in a CD workflow), this tool
determines the associated PR number, reads the PR body via the GitHub
API, and prints the tracking issue number found in the ``Ref #N``
linkage pattern.

PR discovery uses three strategies in order:

1. Merge-commit pattern: ``Merge pull request #N from ...``
2. Squash-merge pattern: ``description (#N)``
3. GitHub API fallback: ``repos/{owner}/{repo}/commits/{sha}/pulls``,
   taking the commit's merged PR. Its other PRs (open or closed unmerged
   ones, a fork's included) are never taken in its place: with no merged PR
   the lookup fails, naming them (GHSA-r4j2-cwcp-xg56).

The ``--pr`` flag bypasses commit parsing entirely.

Consumed by the ``version-bump-pr`` composite action in vergil-actions.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import cast

from vergil_tooling.lib import git, github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.linkage import extract_merge_pr, extract_tracking_issue
from vergil_tooling.lib.process_errors import error_text

# Backward-compatible alias: the merge/squash subject → PR-number extraction now
# lives in ``lib.linkage`` (shared with the CI-evidence harvest layer).
_extract_pr_number = extract_merge_pr


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.pr is not None:
        return _resolve_from_pr(args.pr)

    try:
        subject = git.read_output("log", "-1", "--format=%s", args.commit)
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: failed to read commit {args.commit}: {error_text(exc)}",
            file=sys.stderr,
        )
        return 2

    pr_num = _extract_pr_number(subject)

    if pr_num is None:
        try:
            pr_num = _pr_from_api(args.commit)
        except subprocess.CalledProcessError as exc:
            print(
                f"ERROR: failed to look up the PR of commit {args.commit}: {error_text(exc)}",
                file=sys.stderr,
            )
            return 2
        except ValueError as exc:
            print(
                f"ERROR: malformed GitHub API response for commit {args.commit}: {exc}",
                file=sys.stderr,
            )
            return 2
        except UnmergedPRsError as exc:
            print(
                f"ERROR: commit {args.commit} has no merged PR, only {exc}. "
                "Use --pr N to specify the PR number directly.",
                file=sys.stderr,
            )
            return 1

    if pr_num is None:
        print(
            f"ERROR: cannot determine PR for commit {args.commit}. "
            f"Subject: {subject!r}. "
            "Tried: merge-commit pattern, squash-merge pattern, GitHub API lookup. "
            "Use --pr N to specify the PR number directly.",
            file=sys.stderr,
        )
        return 1

    return _resolve_from_pr(pr_num)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the tracking issue number from a merge commit.",
    )
    parser.add_argument(
        "--commit",
        default="HEAD",
        help="Commit to inspect (default: HEAD)",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="PR number (bypasses commit parsing)",
    )
    return parser.parse_args(argv)


class UnmergedPRsError(Exception):
    """The commit has PRs, but none of them merged: its message names them."""


def _pr_from_api(commit: str) -> int | None:
    """The merged PR of *commit*, or ``None`` when it has no PR at all.

    A commit's PR list also holds PRs that never merged it, a fork's
    included, so only a merged one is taken: with none, the PRs found raise
    :class:`UnmergedPRsError`, never the first of them (GHSA-r4j2-cwcp-xg56).
    """
    repo = github.current_repo()
    sha = git.read_output("rev-parse", commit)
    # Paginated (issue #3162) so a merged PR past the first page is still seen.
    # A malformed page raises ValueError, reported by main() — never read as empty.
    result = github.read_json_paginated(f"repos/{repo}/commits/{sha}/pulls")
    prs = cast("list[dict[str, object]]", [p for p in result if isinstance(p, dict)])
    for pr in prs:
        if pr.get("merged_at"):
            num = pr.get("number")
            if not isinstance(num, int) or isinstance(num, bool):
                msg = f"a merged PR has no usable number ({num!r})"
                raise ValueError(msg)
            return num
    if prs:
        named = ", ".join(f"#{pr.get('number')}" for pr in prs)
        msg = f"{len(prs)} unmerged PR(s): {named}"
        raise UnmergedPRsError(msg)
    return None


def _resolve_from_pr(pr_num: int) -> int:
    try:
        repo = github.current_repo()
        pr_data = github.read_json("api", f"repos/{repo}/pulls/{pr_num}")
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: failed to fetch PR #{pr_num}: {error_text(exc)}", file=sys.stderr)
        return 2

    if not isinstance(pr_data, dict):
        print(f"ERROR: unexpected API response for PR #{pr_num}", file=sys.stderr)
        return 2
    raw_body = pr_data.get("body")
    body = raw_body if isinstance(raw_body, str) else ""
    if not body:
        print(f"ERROR: PR #{pr_num} has no body", file=sys.stderr)
        return 1

    try:
        issue_num = extract_tracking_issue(body)
    except ValueError as exc:
        print(f"ERROR: PR #{pr_num} body has {exc}", file=sys.stderr)
        return 1

    if issue_num is None:
        print(
            f"ERROR: PR #{pr_num} body has no tracking issue linkage (expected 'Ref #N')",
            file=sys.stderr,
        )
        return 1

    print(issue_num)
    return 0


if __name__ == "__main__":
    sys.exit(main())
