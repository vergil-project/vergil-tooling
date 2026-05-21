"""GitHub CLI (``gh``) subprocess wrappers.

All functions that use ``check=True`` retry transparently on transient
GitHub API errors (HTTP 502, 503, 504, 429 and network-level timeouts)
with exponential backoff.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import re
import subprocess
import sys
import time
from typing import Any

log = logging.getLogger(__name__)


def _discover_accounts() -> tuple[str, str]:
    """Parse ``gh auth status`` to find the human and -vergil accounts."""
    result = subprocess.run(  # noqa: S603
        ["gh", "auth", "status"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    output = result.stdout or result.stderr
    accounts = list(dict.fromkeys(re.findall(r"Logged in to github\.com account (\S+)", output)))
    vergil = [a for a in accounts if a.endswith("-vergil")]
    if len(vergil) != 1:
        print(
            "vergil-tooling: cannot discover -vergil account in gh auth status. "
            f"Expected exactly one, found: {vergil}",
            file=sys.stderr,
        )
        raise SystemExit(1)
    agent = vergil[0]
    human = agent.removesuffix("-vergil")
    return human, agent


def resolve_co_author_trailer() -> str:
    """Discover the agent account and return its ``Co-Authored-By`` trailer.

    Workaround: hardcode the noreply trailer while the agent account is
    flagged by GitHub (#799). The API lookup (gh api users/{agent}) fails
    for shadow-banned accounts. Revert to the API-based approach when
    GitHub support unflags the account.
    """
    _human, agent = _discover_accounts()
    # Workaround (#799/#839): skip API lookup, use known noreply ID.
    known_noreply: dict[str, int] = {
        "wphillipmoore-vergil": 285019742,
    }
    uid = known_noreply.get(agent)
    if uid is not None:
        return f"Co-Authored-By: {agent} <{uid}+{agent}@users.noreply.github.com>"
    data = read_json("api", f"users/{agent}")
    if not isinstance(data, dict):
        msg = f"unexpected API response for users/{agent}"
        raise GitHubAPIError(1, f"gh api users/{agent}", msg)
    api_uid = data["id"]
    return f"Co-Authored-By: {agent} <{api_uid}+{agent}@users.noreply.github.com>"


@functools.lru_cache(maxsize=1)
def _human_token() -> str:
    """Return the human account's token (cached for the process lifetime)."""
    human, _agent = _discover_accounts()
    result = subprocess.run(  # noqa: S603
        ["gh", "auth", "token", "-u", human],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _gh_env() -> dict[str, str] | None:
    """Return env dict with the human account's GH_TOKEN, or None on failure."""
    try:
        token = _human_token()
    except (subprocess.CalledProcessError, SystemExit):
        return None
    return {**os.environ, "GH_TOKEN": token}


class GitHubAPIError(subprocess.CalledProcessError):
    """``CalledProcessError`` that includes captured API output in its message."""

    def __str__(self) -> str:
        base = super().__str__()
        parts: list[str] = []
        if self.stderr:
            parts.append(f"stderr: {self.stderr.strip()}")
        if self.stdout:
            parts.append(f"stdout: {self.stdout.strip()}")
        if parts:
            return f"{base}\n{'\n'.join(parts)}"
        return base


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
    if "env" not in kwargs:
        env = _gh_env()
        if env is not None:
            kwargs["env"] = env
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return subprocess.run(*args, **kwargs)  # noqa: S603
        except subprocess.CalledProcessError as exc:
            if attempt == _MAX_RETRIES or not _is_retryable(exc):
                detail = ((exc.stderr or "") + (exc.stdout or "")).strip()
                if detail:
                    raise GitHubAPIError(exc.returncode, exc.cmd, exc.stdout, exc.stderr) from exc
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
    result = _run_with_retry(("gh", *args), check=True, capture_output=True, text=True)  # noqa: S607
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)


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
        env=_gh_env(),
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
        env=_gh_env(),
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


def merge_status(pr: str) -> dict[str, str]:
    """Return merge state and review decision for a PR.

    Single API call returning ``{"mergeStateStatus": ..., "reviewDecision": ...}``.
    """
    result = read_json(
        "pr",
        "view",
        pr,
        "--json",
        "mergeStateStatus,reviewDecision",
    )
    if not isinstance(result, dict):
        msg = f"unexpected API response for pr view {pr}"
        raise GitHubAPIError(1, f"gh pr view {pr}", msg)
    state = str(result.get("mergeStateStatus", ""))
    review = result.get("reviewDecision")
    return {"mergeStateStatus": state, "reviewDecision": str(review) if review else ""}


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
