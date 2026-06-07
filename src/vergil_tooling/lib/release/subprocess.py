"""Subprocess wrappers for noisy release commands (streamed via progress)."""

from __future__ import annotations

import subprocess
import time

from vergil_tooling.lib import progress, retry
from vergil_tooling.lib.github import _gh_env, _poll_and_watch_checks

_POLL_INTERVAL_SECS = 5
_POLL_TIMEOUT_SECS = 180


def _stream_with_retry(cmd: tuple[str, ...]) -> None:
    """Stream *cmd* via progress.run, retrying transient GitHub failures.

    Streaming-compatible analogue of github._run_with_retry: progress.run
    raises CalledProcessError carrying captured output, which is what
    retry.is_retryable inspects. Preserves _gh_env credential injection.
    """
    env = _gh_env()
    for attempt in range(retry.MAX_RETRIES + 1):
        try:
            progress.run(cmd, env=env)
        except subprocess.CalledProcessError as exc:
            if attempt == retry.MAX_RETRIES or not retry.is_retryable(exc):
                raise
            delay = retry.compute_delay(attempt)
            progress.emit(
                f"transient GitHub failure, retrying in {delay:.1f}s"
                f" (attempt {attempt + 1}/{retry.MAX_RETRIES + 1})"
            )
            time.sleep(delay)
        else:
            return
    raise AssertionError("unreachable")  # pragma: no cover


def wait_for_checks(pr: str) -> None:
    """Block until CI checks on *pr* pass, streaming watch output.

    Delegates to the shared poll-and-watch engine — resilient to the PR
    head moving mid-wait (#1490) — with the streaming watch runner.
    """
    _poll_and_watch_checks(
        pr,
        lambda: _stream_with_retry(("gh", "pr", "checks", pr, "--watch")),  # noqa: S607
        poll_interval=_POLL_INTERVAL_SECS,
        poll_timeout=_POLL_TIMEOUT_SECS,
    )


def watch_workflow(repo: str, run_id: str, *, check_status: bool = True) -> None:
    """Block until a workflow run completes, streaming watch output."""
    cmd: tuple[str, ...] = ("gh", "run", "watch", "--repo", repo)
    if check_status:
        cmd = (*cmd, "--exit-status")
    cmd = (*cmd, run_id)
    _stream_with_retry(cmd)  # noqa: S607
