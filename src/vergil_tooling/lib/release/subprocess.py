"""Verbose-aware subprocess wrappers for noisy release commands."""

from __future__ import annotations

import subprocess as _subprocess
import sys
import time

from vergil_tooling.lib.github import _checks_registered, _run_with_retry

_POLL_INTERVAL_SECS = 5
_POLL_TIMEOUT_SECS = 60


def _run_verbose(cmd: tuple[str, ...], *, verbose: bool) -> None:
    """Run *cmd* through the GitHub retry wrapper, printing output if verbose."""
    try:
        result = _run_with_retry(cmd, capture_output=True, text=True, check=True)
    except _subprocess.CalledProcessError as exc:
        if verbose:
            if exc.stdout:
                print(exc.stdout, end="")
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
        raise
    if verbose:
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)


def wait_for_checks(pr: str, *, verbose: bool) -> None:
    """Block until CI checks on *pr* pass. Verbose controls output."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECS
    while not _checks_registered(pr):
        if time.monotonic() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_SECS)

    _run_verbose(
        ("gh", "pr", "checks", pr, "--watch", "--fail-fast"),  # noqa: S607
        verbose=verbose,
    )


def watch_workflow(repo: str, run_id: str, *, verbose: bool) -> None:
    """Block until a workflow run completes. Verbose controls output."""
    _run_verbose(
        ("gh", "run", "watch", "--repo", repo, "--exit-status", run_id),  # noqa: S607
        verbose=verbose,
    )
