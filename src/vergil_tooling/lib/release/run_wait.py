"""Wait for a workflow run's attempt to complete, by reading the run (#5174).

``gh run watch``'s exit status is not a run's result: it exits non-zero
when it attaches before GitHub has registered a re-run's new attempt, or on
a transient API error, while the run is still going. A resume once failed
``confirm-main`` that way, 26 s into an attempt still ``in_progress``. So
the release never watches a run with ``gh run watch``. It re-reads the run
with ``gh run view`` until the attempt it is waiting for is ``completed``,
and returns that attempt's view, conclusion included, for the caller to
judge.

The wait is :func:`cd_runs.wait_attempt_completed`, the one every CD wait
goes through. A failed read is classified first. A transient one (any error
:func:`retry.is_retryable_read` accepts) or a 404 on the run, which was just
listed or re-run, is retried with :func:`retry.compute_delay`'s back-off, as
is a view that cannot be parsed. Any other error fails at once. A read that
keeps failing past :data:`MAX_READ_FAILURES` in a row, or is still failing
when the wait runs out, fails loudly as a **read failure**, and an attempt
read but still not completed at the deadline as **result unknown**: neither
is ever reported as the run's conclusion, and an unread run is never taken
as a finished one. This module words each as a :class:`ReleaseError` in the
stage's phase.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import cd_runs
from vergil_tooling.lib.cd_runs import (
    MAX_READ_FAILURES,
    CDRunReadError,
    CDRunWaitTimeoutError,
    RunView,
    cd_timeout_secs,
)
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["MAX_READ_FAILURES", "POLL_INTERVAL_SECS", "RunView", "wait_run_completed"]

# Seconds between reads of a run still going.
POLL_INTERVAL_SECS = 10


def wait_run_completed(  # noqa: PLR0913 - the clock and sleep are test seams
    repo: str,
    run_id: str,
    *,
    phase: str,
    attempt: int | None = None,
    timeout: float | None = None,
    sleep: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
) -> RunView:
    """Re-read run *run_id* until *attempt* (or, with none, its latest) is ``completed``.

    Returns the completed attempt's view: its ``conclusion`` is the run's
    result, for the caller to judge. *timeout* defaults to
    ``$VRG_CD_TIMEOUT`` (:func:`cd_timeout_secs`).

    Raises :class:`ReleaseError` in *phase*, naming which it was, on a read
    error that is not transient, on a persistent read failure, and on an
    attempt still not completed at the deadline (result unknown).
    """
    command = f"gh run view {run_id} --json status,conclusion,attempt,url"
    try:
        return cd_runs.wait_attempt_completed(
            run_id,
            timeout=cd_timeout_secs() if timeout is None else timeout,
            repo=repo,
            attempt=attempt,
            poll_interval=POLL_INTERVAL_SECS,
            sleep=sleep,
            clock=clock,
        )
    except CDRunReadError as exc:
        message = exc.message
        if exc.persistent:
            message += " Check the run in the Actions tab, then resume."
        raise ReleaseError(
            phase=phase, command=command, message=message, detail=exc.detail
        ) from exc
    except CDRunWaitTimeoutError as exc:
        raise ReleaseError(
            phase=phase,
            command=command,
            message=f"{exc.message} Once the run finishes, resume.",
        ) from exc
