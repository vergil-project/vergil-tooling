"""GitHub CLI (``gh``) subprocess wrappers.

All functions that use ``check=True`` retry transparently on transient
GitHub API errors (HTTP 502, 503, 504, 429 and network-level timeouts)
with exponential backoff.
"""

from __future__ import annotations

import json
import logging
import random
import subprocess
import time
from typing import Any

log = logging.getLogger(__name__)

_NO_CHECKS_PHRASE = "no checks reported"
_POLL_INTERVAL_SECS = 5
_POLL_TIMEOUT_SECS = 60

_MAX_RETRIES = 4
_BASE_DELAY_SECS = 2.0
_MAX_DELAY_SECS = 60.0
_RETRYABLE_PATTERNS = (
    "http 502",
    "http 503",
    "http 504",
    "http 429",
    "timed out",
    "connection reset",
)


def _is_retryable(exc: subprocess.CalledProcessError) -> bool:
    detail = ((exc.stderr or "") + (exc.stdout or "")).lower()
    return any(p in detail for p in _RETRYABLE_PATTERNS)


def _run_with_retry(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return subprocess.run(*args, **kwargs)  # noqa: S603
        except subprocess.CalledProcessError as exc:
            if attempt == _MAX_RETRIES or not _is_retryable(exc):
                raise
            delay = min(_BASE_DELAY_SECS * (2**attempt), _MAX_DELAY_SECS)
            delay *= 0.5 + random.random()  # noqa: S311
            log.warning(
                "GitHub API error (attempt %d/%d), retrying in %.1fs",
                attempt + 1,
                _MAX_RETRIES + 1,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def run(*args: str) -> None:
    """Run a gh command and raise on failure."""
    _run_with_retry(("gh", *args), check=True)  # noqa: S607


def read_output(*args: str) -> str:
    """Run a gh command and return stripped stdout."""
    result = _run_with_retry(
        ("gh", *args),  # noqa: S607
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def read_json(*args: str) -> dict[str, object] | list[object]:
    """Run a gh command and return parsed JSON from stdout."""
    raw = read_output(*args)
    result: dict[str, object] | list[object] = json.loads(raw)
    return result


def write_json(method: str, endpoint: str, body: dict[str, object]) -> None:
    """Call gh api with a JSON body via stdin."""
    _run_with_retry(
        ("gh", "api", endpoint, "-X", method, "--input", "-"),  # noqa: S607
        input=json.dumps(body),
        check=True,
        text=True,
        capture_output=True,
    )


def delete(endpoint: str) -> None:
    """Call gh api with DELETE method."""
    _run_with_retry(
        ("gh", "api", endpoint, "-X", "DELETE"),  # noqa: S607
        check=True,
        text=True,
        capture_output=True,
    )


def delete_if_exists(endpoint: str) -> bool:
    """Call gh api DELETE; return True if deleted (2xx), False if 404."""
    result = subprocess.run(  # noqa: S603
        ("gh", "api", endpoint, "-X", "DELETE", "-i"),  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    first_line = result.stdout.split("\n")[0] if result.stdout else ""
    return "404" not in first_line


def create_pr(*, base: str, title: str, body_file: str) -> str:
    """Create a pull request and return its URL."""
    return read_output("pr", "create", "--base", base, "--title", title, "--body-file", body_file)


def _checks_registered(pr: str) -> bool:
    """Return True if at least one check is registered on ``pr``."""
    result = subprocess.run(  # noqa: S603
        ("gh", "pr", "checks", pr),  # noqa: S607
        capture_output=True,
        text=True,
    )
    return _NO_CHECKS_PHRASE not in (result.stdout + result.stderr)


def wait_for_checks(
    pr: str,
    *,
    poll_interval: int = _POLL_INTERVAL_SECS,
    poll_timeout: int = _POLL_TIMEOUT_SECS,
) -> None:
    """Block until all required checks on ``pr`` complete; fail fast on the first red.

    Polls internally when no checks have registered yet (the window between
    git push and GitHub registering the checks run). Polls every
    ``poll_interval`` seconds for up to ``poll_timeout`` seconds before
    falling through to the blocking watch.

    Transient GitHub API errors (502/503/504/429) are retried automatically
    via the library-level retry wrapper.  Persistent failures surface as
    ``subprocess.CalledProcessError``.
    """
    deadline = time.monotonic() + poll_timeout
    while not _checks_registered(pr):
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    run("pr", "checks", pr, "--watch", "--fail-fast")


def mergeable(pr: str) -> str:
    """Return the PR's mergeable status (e.g. ``MERGEABLE``, ``CONFLICTING``, ``UNKNOWN``)."""
    return read_output(
        "pr",
        "view",
        pr,
        "--json",
        "mergeable",
        "--jq",
        ".mergeable",
    )


def merge_state_status(pr: str) -> str:
    """Return the PR's mergeStateStatus (e.g. ``CLEAN``, ``BEHIND``, ``DIRTY``)."""
    return read_output(
        "pr",
        "view",
        pr,
        "--json",
        "mergeStateStatus",
        "--jq",
        ".mergeStateStatus",
    )


def current_repo() -> str:
    """Return ``OWNER/REPO`` for the current directory's git remote."""
    return read_output("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")


def update_branch(pr: str) -> None:
    """Fast-forward merge the base branch into the PR branch.

    Uses the GitHub REST API ``PUT /repos/{owner}/{repo}/pulls/{number}/update-branch``.
    Only appropriate when the branch is behind the base — not when there are
    merge conflicts.
    """
    number = read_output("pr", "view", pr, "--json", "number", "--jq", ".number")
    repo = current_repo()
    read_output("api", f"repos/{repo}/pulls/{number}/update-branch", "-X", "PUT")


def merge(pr: str, *, strategy: str) -> None:
    """Merge a PR synchronously (without ``--auto``).

    ``strategy`` is one of ``"merge"``, ``"squash"``, ``"rebase"`` — passed
    through as ``--merge``, ``--squash``, ``--rebase``.

    Does not pass ``--delete-branch`` — branch cleanup is handled by
    ``vrg-finalize-repo`` after the merge completes.
    """
    run("pr", "merge", f"--{strategy}", pr)


def list_project_repos(owner: str, project: str) -> list[str]:
    """Return sorted, unique repos linked to a GitHub Project."""
    jq_filter = (
        f".[] | select(.projectsV2.Nodes | length > 0) "
        f"| select(.projectsV2.Nodes[].number == {project}) "
        f"| .nameWithOwner"
    )
    output = read_output(
        "repo",
        "list",
        owner,
        "--json",
        "nameWithOwner,projectsV2",
        "--limit",
        "100",
        "--jq",
        jq_filter,
    )
    return sorted({r for r in output.splitlines() if r})
