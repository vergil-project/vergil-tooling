"""Extract the release tracking issue number from a merge commit.

Given a merge commit on main (typically HEAD in a CD workflow), this
tool extracts the PR number from the commit subject, reads the PR
body via the GitHub API, and prints the tracking issue number found
in the ``Ref #N`` linkage pattern.

Consumed by the ``version-bump-pr`` composite action in vergil-actions.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

from vergil_tooling.lib import git, github
from vergil_tooling.lib.linkage import extract_tracking_issue

_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the tracking issue number from a merge commit.",
    )
    parser.add_argument(
        "--commit",
        default="HEAD",
        help="Merge commit to inspect (default: HEAD)",
    )
    return parser.parse_args(argv)


def _extract_pr_number(subject: str) -> int | None:
    m = _MERGE_PR_RE.match(subject)
    return int(m.group(1)) if m else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

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
        print(
            f"ERROR: commit {args.commit} is not a merge commit "
            "(expected 'Merge pull request #N from ...' "
            "— squash and rebase merges are not supported)",
            file=sys.stderr,
        )
        return 1

    try:
        repo = github.current_repo()
        pr_data = github.read_json("api", f"repos/{repo}/pulls/{pr_num}")
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: failed to fetch PR #{pr_num}: {exc}",
            file=sys.stderr,
        )
        return 2

    body: str = pr_data.get("body", "") or ""  # type: ignore[union-attr]
    if not body:
        print(f"ERROR: PR #{pr_num} has no body", file=sys.stderr)
        return 1

    try:
        issue_num = extract_tracking_issue(body)
    except ValueError as exc:
        print(
            f"ERROR: PR #{pr_num} body has {exc}",
            file=sys.stderr,
        )
        return 1

    if issue_num is None:
        print(
            f"ERROR: PR #{pr_num} body has no tracking issue linkage "
            "(expected 'Ref #N')",
            file=sys.stderr,
        )
        return 1

    print(issue_num)
    return 0


if __name__ == "__main__":
    sys.exit(main())
