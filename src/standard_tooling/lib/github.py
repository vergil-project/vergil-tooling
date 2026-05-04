"""GitHub CLI (``gh``) subprocess wrappers."""

from __future__ import annotations

import json
import subprocess
import time

_NO_CHECKS_PHRASE = "no checks reported"
_POLL_INTERVAL_SECS = 5
_POLL_TIMEOUT_SECS = 60


def run(*args: str) -> None:
    """Run a gh command and raise on failure."""
    subprocess.run(("gh", *args), check=True)  # noqa: S603, S607


def read_output(*args: str) -> str:
    """Run a gh command and return stripped stdout."""
    result = subprocess.run(  # noqa: S603
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

    Surfaces the failure via ``subprocess.CalledProcessError`` — callers are
    responsible for deciding how to react (the release-workflow convention is
    to stop and surface; do not retry).
    """
    deadline = time.monotonic() + poll_timeout
    while not _checks_registered(pr):
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)
    run("pr", "checks", pr, "--watch", "--fail-fast")


def merge(pr: str, *, strategy: str) -> None:
    """Merge a PR synchronously (without ``--auto``).

    ``strategy`` is one of ``"merge"``, ``"squash"``, ``"rebase"`` — passed
    through as ``--merge``, ``--squash``, ``--rebase``.

    Does not pass ``--delete-branch`` — branch cleanup is handled by
    ``st-finalize-repo`` after the merge completes.
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
