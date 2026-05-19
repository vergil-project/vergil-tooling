"""Extract the release tracking issue number from a PR on main.

Given a commit (typically HEAD on main in a CD workflow), this tool
determines the associated PR number, reads the PR body via the GitHub
API, and prints the tracking issue number found in the ``Ref #N``
linkage pattern.

PR discovery uses three strategies in order:

1. Merge-commit pattern: ``Merge pull request #N from ...``
2. Squash-merge pattern: ``description (#N)``
3. GitHub API fallback: ``repos/{owner}/{repo}/commits/{sha}/pulls``

The ``--pr`` flag bypasses commit parsing entirely.

Consumed by the ``version-bump-pr`` composite action in vergil-actions.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
from typing import cast

from vergil_tooling.lib import git, github
from vergil_tooling.lib.linkage import extract_tracking_issue

_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from ")
_SQUASH_PR_RE = re.compile(r"\(#(\d+)\)\s*$")


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


def _extract_pr_number(subject: str) -> int | None:
    m = _MERGE_PR_RE.match(subject)
    if m:
        return int(m.group(1))
    m = _SQUASH_PR_RE.search(subject)
    if m:
        return int(m.group(1))
    return None


def _pr_from_api(commit: str) -> int | None:
    repo = github.current_repo()
    sha = git.read_output("rev-parse", commit)
    result = github.read_json("api", f"repos/{repo}/commits/{sha}/pulls")
    if not isinstance(result, list):
        return None
    prs = cast("list[dict[str, object]]", [p for p in result if isinstance(p, dict)])
    for pr in prs:
        if pr.get("merged_at"):
            num = pr.get("number")
            if isinstance(num, int):
                return num
    if prs:
        num = prs[0].get("number")
        if isinstance(num, int):
            return num
    return None


def _resolve_from_pr(pr_num: int) -> int:
    try:
        repo = github.current_repo()
        pr_data = github.read_json("api", f"repos/{repo}/pulls/{pr_num}")
    except subprocess.CalledProcessError as exc:
        print(f"ERROR: failed to fetch PR #{pr_num}: {exc}", file=sys.stderr)
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.pr is not None:
        return _resolve_from_pr(args.pr)

    try:
        subject = git.read_output("log", "-1", "--format=%s", args.commit)
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: failed to read commit {args.commit}: {exc}",
            file=sys.stderr,
        )
        return 2

    pr_num = _extract_pr_number(subject)

    if pr_num is None:
        with contextlib.suppress(subprocess.CalledProcessError):
            pr_num = _pr_from_api(args.commit)

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


if __name__ == "__main__":
    sys.exit(main())
