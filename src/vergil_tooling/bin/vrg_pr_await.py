"""Block until a pull request settles, then print its state as JSON.

``vrg-pr-await <PR> [--since-sha <sha>] [--since-reviews <n>]`` is the post-PR
counterpart to ``vrg-await`` (§9 of the Vergil 2.1 workflow design). It polls
the GitHub API and returns when the PR settles: all checks reach a terminal
conclusion, **or** the head SHA moves (a new commit), **or** a new review
appears, **or** the PR is wedged by an orphaned check-run (``reason`` is
``orphaned_check``: it is ``BLOCKED`` with every pending check stuck
non-terminal after its backing run completed, for longer than the
``$VRG_ORPHAN_GRACE`` grace period — 300s by default — since GitHub can write
results back late). The baselines let the caller
thread "what I last saw" so a settle on a fresh commit/review is detected even
when checks are already terminal.

On settle it prints a JSON object (``reason``, ``head_sha``, ``review_count``,
``checks``, ``failed_checks``, ``all_checks_passed``, ``merge_state_status``,
``orphaned_checks``) for the wrapping skill to reconcile, and exits 0. Like
``vrg-await`` it blocks patiently and indefinitely — but the ``orphaned_check``
settle deliberately breaks that block, since an orphaned check emits no further
event and the wait would otherwise never return.

If any poll (including the first) finds the PR already merged, it aborts with
an error and exits 1: a merged PR can never settle into actionable output, and
a merge observed mid-watch means the audit cycle was bypassed — failing loudly
surfaces the short-circuit instead of leaving an orphaned poll loop running.

When the PR is given as a URL, every GitHub call is scoped to the URL's owner
(``github.target_org``) so the App installation token is minted for the PR's
org regardless of the cwd -- e.g. under a Monitor whose cwd is not a git repo,
where cwd org detection finds nothing and no token is injected (#3055). A bare
PR number keeps the cwd-based resolution, matching how ``gh`` resolves it.

A GitHub API failure while polling exits 1 with the underlying ``gh`` error on
stderr instead of a traceback. A permission failure (HTTP 403 / "Resource not
accessible by integration" -- typically on the checks/statusCheckRollup or
Actions reads) is named as a GitHub App permissions problem for the PR's repo,
pointing at the **Checks**, **Commit statuses** and **Actions** repository
permissions (#2862, #3215).
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from vergil_tooling.lib import github, pr_await
from vergil_tooling.lib.cli import cli_main

_PR_URL_OWNER_RE = re.compile(r"github\.com/([^/]+)/[^/]+/pull/\d+")
_PR_URL_REPO_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/\d+")

# Lowercased substrings of gh's output that mark an API permission failure.
# The GraphQL checks read reports only the "not accessible" text; the REST
# Actions read reports "HTTP 403" alongside it.
_PERMISSION_SIGNATURES = ("resource not accessible by integration", "http 403")


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        # Mint the App token for the PR's owner, not the cwd org (#3055).
        with github.target_org(pr_owner(args.pr)):
            state, reason = pr_await.wait_for_settle(
                args.pr,
                since_sha=args.since_sha,
                since_reviews=args.since_reviews,
            )
    except github.NoInstallationError as exc:
        print(f"vrg-pr-await: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    except github.MissingGitHubTokenError:
        print(github.missing_token_message("vrg-pr-await"), file=sys.stderr)
        return 1
    except github.GitHubAPIError as exc:
        if is_permission_error(exc):
            print(f"vrg-pr-await: {permission_error_message(args.pr, exc)}", file=sys.stderr)
        else:
            print(f"vrg-pr-await: GitHub API error: {exc}", file=sys.stderr)
        return 1
    except pr_await.PrMergedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(pr_await.to_output(state, reason), indent=2))
    return 0


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


def pr_owner(pr: str) -> str | None:
    """The owner of *pr* when it is a PR URL, else ``None`` (cwd resolution)."""
    match = _PR_URL_OWNER_RE.search(pr)
    return match.group(1) if match else None


def pr_repo(pr: str) -> str | None:
    """The ``owner/repo`` of *pr* when it is a PR URL, else ``None``."""
    match = _PR_URL_REPO_RE.search(pr)
    return match.group(1) if match else None


def is_permission_error(exc: github.GitHubAPIError) -> bool:
    """True when *exc* is a GitHub API permission (403) failure."""
    text = f"{exc.stderr or ''}\n{exc.stdout or ''}".lower()
    return any(signature in text for signature in _PERMISSION_SIGNATURES)


def permission_error_message(pr: str, exc: github.GitHubAPIError) -> str:
    """An actionable message for a 403 reading the PR's checks/Actions state."""
    repo = pr_repo(pr) or "the current repository"
    return (
        f"GitHub App permissions problem for {repo}: the agent identity's App "
        "installation was denied reading PR checks/Actions state (HTTP 403). "
        "The App likely needs the Checks (checks: read), Commit statuses "
        "(statuses: read) and Actions (actions: read) repository permissions "
        "set to Read-only, accepted on its installation for this repository.\n"
        f"{exc}"
    )


if __name__ == "__main__":
    sys.exit(main())
