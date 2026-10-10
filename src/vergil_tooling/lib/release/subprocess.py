"""Subprocess wrappers for noisy release commands (streamed via progress)."""

from __future__ import annotations

import subprocess
import time

from vergil_tooling.lib import progress, retry
from vergil_tooling.lib.github import _gh_env


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


def watch_workflow(repo: str, run_id: str, *, check_status: bool = True) -> None:
    """Block until a workflow run completes, streaming watch output."""
    cmd: tuple[str, ...] = ("gh", "run", "watch", "--repo", repo)
    if check_status:
        cmd = (*cmd, "--exit-status")
    cmd = (*cmd, run_id)
    _stream_with_retry(cmd)  # noqa: S607
