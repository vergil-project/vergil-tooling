"""Block until a pull request merges, or cannot merge, and print the outcome as JSON.

``vrg-pr-await <PR>`` waits for a PR submitted with auto-merge on (epic
vergil-project/.github#1218) to merge. It is what ``pr-watch`` blocks on. The
judging lives in :mod:`vergil_tooling.lib.pr_await`; this command maps its
outcome to an exit status, so no caller has to look the PR up again to tell a
merge from a failure:

- **0, merged.** The PR merged, during the wait or before it started.
- **1, cannot merge.** The PR cannot merge as it stands. The JSON's
  ``reason`` names why: ``check_failed`` (``failed_checks`` names the
  required checks), ``check_cancelled`` (a required check was cancelled
  and no newer run replaced it; ``failed_checks`` names it), ``conflict``,
  ``auto_merge_off``, ``closed``, ``orphaned_check`` (``orphaned_checks``
  names them) or ``timed_out`` (``$VRG_FINALIZE_DEADLINE`` passed with the
  PR still pending). The same
  reason is printed on stderr. The agent running pr-watch deals with it.
- **2, any other error.** Bad arguments, a malformed ``$VRG_FINALIZE_DEADLINE``
  or ``$VRG_ORPHAN_GRACE`` (checked before any waiting), a GitHub API or
  permission failure, a PR state that cannot be read: a message on stderr
  and nothing on stdout. Exit 1 never means anything but "cannot merge".

On 0 and 1 it prints one JSON object: ``pr``, ``outcome`` (``merged`` or
``cannot_merge``), ``reason`` (``null`` when merged), ``detail``,
``head_sha``, ``merge_commit`` (``null`` unless merged),
``merge_state_status``, ``failed_checks`` and ``orphaned_checks``.

A new review or a new commit does not end the wait. The ``--since-sha`` and
``--since-reviews`` baselines of the old settle-and-return contract are
removed: passing either is an error (exit 2) that says so.

When the PR is given as a URL, every GitHub call is scoped to the URL's owner
(``github.target_org``) so the App installation token is minted for the PR's
org regardless of the cwd -- e.g. under a Monitor whose cwd is not a git repo,
where cwd org detection finds nothing and no token is injected (#3055). A bare
PR number keeps the cwd-based resolution, matching how ``gh`` resolves it.

A permission failure (HTTP 403 / "Resource not accessible by integration" --
typically on the checks/statusCheckRollup or Actions reads) is named as a
GitHub App permissions problem for the PR's repo, pointing at the **Checks**,
**Commit statuses** and **Actions** repository permissions (#2862, #3215).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import traceback

from vergil_tooling.lib import finalize_watch, github, pr_await
from vergil_tooling.lib.cli import cli_main

#: Exit statuses: merged, cannot merge, any other error.
EXIT_MERGED = 0
EXIT_CANNOT_MERGE = 1
EXIT_ERROR = 2

_PR_URL_OWNER_RE = re.compile(r"github\.com/([^/]+)/[^/]+/pull/\d+")
_PR_URL_REPO_RE = re.compile(r"github\.com/([^/]+/[^/]+)/pull/\d+")

# Lowercased substrings of gh's output that mark an API permission failure.
# The GraphQL checks read reports only the "not accessible" text; the REST
# Actions read reports "HTTP 403" alongside it.
_PERMISSION_SIGNATURES = ("resource not accessible by integration", "http 403")

_REMOVED_FLAGS = ("--since-sha", "--since-reviews")


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        # Mint the App token for the PR's owner, not the cwd org (#3055).
        with github.target_org(pr_owner(args.pr)):
            result = pr_await.wait_for_merge(args.pr)
    except github.AppAuthError as exc:
        print(f"vrg-pr-await: {github.app_auth_message(exc)}", file=sys.stderr)
        return EXIT_ERROR
    except github.NoInstallationError as exc:
        print(f"vrg-pr-await: {github.no_installation_message(exc)}", file=sys.stderr)
        return EXIT_ERROR
    except github.GitHubAPIError as exc:
        print(api_error_message(args.pr, exc), file=sys.stderr)
        return EXIT_ERROR
    except finalize_watch.FinalizeWatchError as exc:
        # The PR read wraps gh's failure; a missing token or a 403 keeps its guidance.
        cause = exc.__cause__
        if isinstance(cause, github.GitHubAPIError):
            print(api_error_message(args.pr, cause), file=sys.stderr)
        print(f"vrg-pr-await: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except (pr_await.PrAwaitError, ValueError) as exc:
        print(f"vrg-pr-await: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception:  # noqa: BLE001 -- exit 1 is reserved for "cannot merge"
        # Python exits 1 on an uncaught exception, which a caller would read as
        # "cannot merge". Print the whole traceback and exit 2 instead.
        print("vrg-pr-await: unexpected error:", file=sys.stderr)
        traceback.print_exc()
        return EXIT_ERROR
    print(json.dumps(result.to_output(), indent=2))
    if result.merged:
        return EXIT_MERGED
    print(
        f"vrg-pr-await: {result.pr} cannot merge ({result.reason}): {result.detail}",
        file=sys.stderr,
    )
    return EXIT_CANNOT_MERGE


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments; a removed flag is an error (exit 2)."""
    parser = argparse.ArgumentParser(
        description=(
            "Block until a PR merges (exit 0) or cannot merge (exit 1, with the "
            "reason); any other error exits 2."
        ),
    )
    parser.add_argument("pr", help="PR URL or number")
    for flag in _REMOVED_FLAGS:
        parser.add_argument(flag, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    for flag in _REMOVED_FLAGS:
        if getattr(args, flag.removeprefix("--").replace("-", "_")) is not None:
            parser.error(
                f"{flag} was removed: vrg-pr-await now waits for the PR to merge, "
                "and a new commit or review no longer ends the wait"
            )
    return args


def pr_owner(pr: str) -> str | None:
    """The owner of *pr* when it is a PR URL, else ``None`` (cwd resolution)."""
    match = _PR_URL_OWNER_RE.search(pr)
    return match.group(1) if match else None


def pr_repo(pr: str) -> str | None:
    """The ``owner/repo`` of *pr* when it is a PR URL, else ``None``."""
    match = _PR_URL_REPO_RE.search(pr)
    return match.group(1) if match else None


def api_error_message(pr: str, exc: github.GitHubAPIError) -> str:
    """The stderr message for a failed GitHub call: missing token, 403 or plain."""
    if isinstance(exc, github.MissingGitHubTokenError):
        return github.missing_token_message("vrg-pr-await")
    if is_permission_error(exc):
        return f"vrg-pr-await: {permission_error_message(pr, exc)}"
    return f"vrg-pr-await: GitHub API error: {exc}"


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
