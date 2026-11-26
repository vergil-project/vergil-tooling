"""Wait for a workflow run's attempt to complete, by reading the run (#5174).

``gh run watch``'s exit status is not a run's result: it exits non-zero
when it attaches before GitHub has registered a re-run's new attempt, or on
a transient API error, while the run is still going. A resume once failed
``confirm-main`` that way, 26 s into an attempt still ``in_progress``. So
the release never watches a run with ``gh run watch``. It re-reads the run
with ``gh run view`` until the attempt it is waiting for is ``completed``,
and returns that attempt's view, conclusion included, for the caller to
judge.

A failed read is classified first. A transient one (any error
:func:`retry.is_retryable_read` accepts) or a 404 on the run, which was just
listed or re-run, is retried with :func:`retry.compute_delay`'s back-off, as
is a view that cannot be parsed. Any other error fails at once. A read that
keeps failing past :data:`MAX_READ_FAILURES` in a row fails loudly as a
**read failure**, and an attempt still not completed at the deadline as
**result unknown**: neither is ever reported as the run's conclusion, and an
unread run is never taken as a finished one.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import github, retry
from vergil_tooling.lib.cd_runs import cd_timeout_secs
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from collections.abc import Callable

# Seconds between reads of a run still going.
POLL_INTERVAL_SECS = 10
# Consecutive failed reads tolerated before the wait fails as a read failure.
# Each read already retries transient errors inside github.read_output; with
# compute_delay's back-off between them this spans a few minutes of outage.
MAX_READ_FAILURES = 6


@dataclass(frozen=True)
class RunView:
    """One read of a run: its latest attempt's status and conclusion, and its URL."""

    status: str
    conclusion: str
    attempt: int
    url: str


def _parse_view(out: str) -> RunView:
    """The :class:`RunView` in ``gh run view --json`` output; ``ValueError`` if malformed."""
    data = json.loads(out)  # JSONDecodeError is a ValueError
    if not isinstance(data, dict):
        msg = f"gh run view returned {type(data).__name__}, not an object"
        raise ValueError(msg)  # noqa: TRY004 - a malformed read, retried as one
    attempt = data.get("attempt")
    status = data.get("status")
    if not isinstance(attempt, int) or not isinstance(status, str) or not status:
        msg = f"gh run view returned no attempt number or status: {out!r}"
        raise ValueError(msg)
    return RunView(
        status=status,
        conclusion=str(data.get("conclusion") or ""),
        attempt=attempt,
        url=str(data.get("url") or ""),
    )


def _read_view(repo: str, run_id: str) -> RunView:
    out = github.read_output(
        "run", "view", "--repo", repo, run_id, "--json", "status,conclusion,attempt,url"
    )
    return _parse_view(out)


def _is_transient(exc: subprocess.CalledProcessError) -> bool:
    """Whether a failed read of the run is worth another try."""
    return retry.is_retryable_read(exc) or retry.is_not_found(exc)


def wait_run_completed(  # noqa: PLR0913 - the clock and sleep are test seams
    repo: str,
    run_id: str,
    *,
    phase: str,
    attempt: int | None = None,
    timeout: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RunView:
    """Re-read run *run_id* until *attempt* (or, with none, its latest) is ``completed``.

    Returns the completed attempt's view: its ``conclusion`` is the run's
    result, for the caller to judge. A view of an earlier attempt than
    *attempt* (GitHub has not caught up with a re-run yet) is waited
    through, as is any status short of ``completed``. A completed view with
    no conclusion is a read that failed. *timeout* defaults to
    ``$VRG_CD_TIMEOUT`` (:func:`cd_timeout_secs`).

    Raises :class:`ReleaseError` in *phase*, naming which it was, on a read
    error that is not transient, on more than :data:`MAX_READ_FAILURES`
    failed reads in a row (a persistent read failure), and on an attempt
    still not completed at the deadline (result unknown).
    """
    deadline = clock() + (cd_timeout_secs() if timeout is None else timeout)
    which = f"attempt {attempt}" if attempt is not None else "its latest attempt"
    command = f"gh run view {run_id} --json status,conclusion,attempt,url"
    failures = 0
    shown = ""
    last: RunView | None = None
    while True:
        try:
            view = _read_view(repo, run_id)
            if view.status == "completed" and not view.conclusion:
                msg = f"gh run view reports run {run_id} completed with no conclusion"
                raise ValueError(msg)  # noqa: TRY301 - read like any other malformed view
        except subprocess.CalledProcessError as exc:
            if not _is_transient(exc):
                raise ReleaseError(
                    phase=phase,
                    command=command,
                    message=(
                        f"Run {run_id} could not be read while waiting for {which} to "
                        "complete. This is a read failure, not the run's result."
                    ),
                    detail=error_text(exc),
                ) from exc
            failures, error = failures + 1, error_text(exc)
        except ValueError as exc:
            failures, error = failures + 1, error_text(exc)
        else:
            failures, last = 0, view
            if (attempt is None or view.attempt >= attempt) and view.status == "completed":
                print(f"  Run {run_id} attempt {view.attempt} completed: {view.conclusion}")
                return view
            line = f"  Run {run_id} attempt {view.attempt}: {view.status}"
            if line != shown:
                print(line)
                shown = line
        if failures > MAX_READ_FAILURES:
            raise ReleaseError(
                phase=phase,
                command=command,
                message=(
                    f"Run {run_id} could not be read {failures} times in a row while "
                    f"waiting for {which} to complete. This is a persistent read "
                    "failure, not the run's result: check the run in the Actions tab, "
                    "then resume."
                ),
                detail=error,
            )
        remaining = deadline - clock()
        if remaining <= 0:
            seen = f"last read: attempt {last.attempt} {last.status}" if last else "never read"
            raise ReleaseError(
                phase=phase,
                command=command,
                message=(
                    f"Run {run_id}'s {which} had not completed when the wait ran out "
                    f"({seen}). The result is unknown, not a failure: once the run "
                    "finishes, resume."
                ),
            )
        if failures:
            delay = retry.compute_delay(failures - 1)
            print(f"  Could not read run {run_id} ({error}); retrying in {delay:.1f}s")
        else:
            delay = POLL_INTERVAL_SECS
        sleep(min(delay, remaining))
