"""Check whether a gh pr merge / gh pr review --approve targets a release-workflow PR.

Called by the block-agent-merge hook in standard-tooling-plugin.
Takes the raw Bash command string, extracts the PR reference,
resolves the branch via the GitHub API, and checks the allow-list.

Exit codes follow the three-state convention (standard-tooling#373):
  0 — allowed (release-workflow branch)
  1 — denied (tool ran, branch not on allow-list)
  2 — unknown (tool could not determine the answer)
"""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys

from standard_tooling.lib import github
from standard_tooling.lib.release import is_release_branch

_CHAIN_RE = re.compile(r"\s*(?:&&|\|\||[;|])\s*")

_DENY_MESSAGE = (
    "Blocked: agents may not merge non-release PRs. The pr-workflow\n"
    "policy requires human review and merge for feature/bugfix PRs.\n"
    "Hand off the PR URL to the user and stop the work cycle.\n"
    "\n"
    "Only release-workflow PRs (release/*) may be agent-merged,\n"
    "and only via st-merge-when-green from the publish skill.\n"
    "See issue #162."
)

_GH_MERGE_FLAGS = frozenset(
    {
        "--squash",
        "--merge",
        "--rebase",
        "--delete-branch",
        "--auto",
        "--disable-auto",
        "--admin",
    }
)

_GH_REVIEW_FLAGS = frozenset(
    {
        "--approve",
        "-a",
        "--request-changes",
        "-r",
        "--comment",
        "-c",
    }
)

_GH_FLAGS_WITH_VALUE = frozenset(
    {
        "--repo",
        "-R",
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--subject",
        "-t",
        "--match-head-commit",
    }
)


def extract_pr_ref(command: str) -> tuple[str, str | None]:
    """Extract the PR reference and optional repo from a command string.

    Returns (pr_ref, repo) where repo is None if --repo was not specified.
    Raises ValueError if no gh pr merge/review --approve is found.
    """
    segments = _CHAIN_RE.split(command)

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue

        result = _extract_from_tokens(tokens)
        if result is not None:
            return result

    raise ValueError(f"No gh pr merge or gh pr review --approve found in: {command}")


def _extract_from_tokens(tokens: list[str]) -> tuple[str, str | None] | None:
    """Extract PR ref from a tokenized command segment."""
    try:
        gh_idx = tokens.index("gh")
    except ValueError:
        return None

    rest = tokens[gh_idx + 1 :]
    if len(rest) < 2 or rest[0] != "pr":
        return None

    subcommand = rest[1]

    if subcommand == "merge":
        return _parse_args(rest[2:], _GH_MERGE_FLAGS)
    elif subcommand == "review":
        if "--approve" not in rest[2:]:
            return None
        return _parse_args(rest[2:], _GH_REVIEW_FLAGS)

    return None


def _parse_args(args: list[str], known_flags: frozenset[str]) -> tuple[str, str | None] | None:
    """Walk args to find the PR ref and optional --repo value."""
    repo: str | None = None
    pr_ref: str | None = None
    i = 0

    while i < len(args):
        arg = args[i]

        if arg in _GH_FLAGS_WITH_VALUE and i + 1 < len(args):
            if arg in ("--repo", "-R"):
                repo = args[i + 1]
            i += 2
            continue

        if arg in known_flags or arg.startswith("-"):
            i += 1
            continue

        pr_ref = arg
        i += 1

    if pr_ref is None:
        return None

    return (pr_ref, repo)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Check whether a PR merge/approval targets a release-workflow branch.",
    )
    parser.add_argument("command", help="Raw Bash command string to check")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        pr_ref, repo = extract_pr_ref(args.command)
    except ValueError:
        return 0

    try:
        api_args = ["pr", "view", pr_ref]
        if repo:
            api_args.extend(["--repo", repo])
        api_args.extend(["--json", "headRefName", "--jq", ".headRefName"])
        branch = github.read_output(*api_args)
    except subprocess.CalledProcessError as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        print(
            f"Could not resolve PR branch: {detail}",
            file=sys.stderr,
        )
        return 2

    if is_release_branch(branch):
        return 0

    print(_DENY_MESSAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
