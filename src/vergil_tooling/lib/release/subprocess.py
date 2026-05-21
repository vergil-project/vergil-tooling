"""Verbose-aware subprocess wrappers for noisy release commands."""

from __future__ import annotations

import subprocess as _subprocess
import sys
import time

from vergil_tooling.lib.github import _checks_registered, _gh_env

_POLL_INTERVAL_SECS = 5
_POLL_TIMEOUT_SECS = 60


def wait_for_checks(pr: str, *, verbose: bool) -> None:
    """Block until CI checks on *pr* pass. Verbose controls output."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECS
    while not _checks_registered(pr):
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_SECS)

    env = _gh_env()
    result = _subprocess.run(  # noqa: S603
        ("gh", "pr", "checks", pr, "--watch", "--fail-fast"),  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if verbose:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

    if result.returncode != 0:
        raise _subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )


def watch_workflow(repo: str, run_id: str, *, verbose: bool) -> None:
    """Block until a workflow run completes. Verbose controls output."""
    env = _gh_env()
    result = _subprocess.run(  # noqa: S603
        ("gh", "run", "watch", "--repo", repo, "--exit-status", run_id),  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if verbose:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)

    if result.returncode != 0:
        raise _subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )
