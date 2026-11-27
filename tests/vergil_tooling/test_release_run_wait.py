"""Tests for vergil_tooling.lib.release.run_wait (#5174)."""

from __future__ import annotations

import itertools
import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.run_wait import (
    MAX_READ_FAILURES,
    POLL_INTERVAL_SECS,
    RunView,
    wait_run_completed,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# The wait reads the run through lib/github, from lib/cd_runs.
_READ = "vergil_tooling.lib.github.read_output"


def _view(status: str, conclusion: str | None = None, attempt: int = 1) -> str:
    return json.dumps(
        {"status": status, "conclusion": conclusion, "attempt": attempt, "url": "https://run/5"}
    )


def _err(stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["gh"], output="", stderr=stderr)


def _wait(
    reads: Sequence[str | Exception], *, attempt: int | None = None, timeout: float = 1e9
) -> tuple[RunView, MagicMock, MagicMock]:
    sleep = MagicMock()
    with patch(_READ, side_effect=list(reads)) as read:
        view = wait_run_completed(
            "o/r",
            "5",
            phase="confirm-main",
            attempt=attempt,
            timeout=timeout,
            sleep=sleep,
            clock=lambda: 0.0,
        )
    return view, read, sleep


def test_returns_the_completed_view_with_its_conclusion() -> None:
    view, read, sleep = _wait([_view("in_progress"), _view("completed", "failure")])
    assert view == RunView("completed", "failure", 1, "https://run/5")
    assert read.call_count == 2
    sleep.assert_called_once_with(POLL_INTERVAL_SECS)
    assert read.call_args.args == (
        "run",
        "view",
        "--repo",
        "o/r",
        "5",
        "--json",
        "status,conclusion,attempt,url",
    )


def test_an_earlier_attempt_already_completed_is_waited_through() -> None:
    """The #5174 case: the re-run's attempt 3 is still going; attempt 2 is done."""
    view, read, _ = _wait(
        [
            _view("completed", "failure", attempt=2),
            _view("in_progress", attempt=3),
            _view("completed", "success", attempt=3),
        ],
        attempt=3,
    )
    assert view.attempt == 3
    assert view.conclusion == "success"
    assert read.call_count == 3


def test_a_transient_read_error_is_retried_with_back_off() -> None:
    view, _, sleep = _wait(
        [
            _err("HTTP 502: Bad Gateway"),
            _err("HTTP 500: Internal Server Error"),
            _err("HTTP 404: Not Found"),
            "not json",
            _view("completed", "success"),
        ]
    )
    assert view.conclusion == "success"
    assert sleep.call_count == 4


def test_a_view_of_the_wrong_shape_is_read_again() -> None:
    view, read, _ = _wait(
        ["[]", json.dumps({"status": "completed"}), _view("completed", "success")]
    )
    assert view.conclusion == "success"
    assert read.call_count == 3


def test_a_completed_view_with_no_conclusion_is_read_again() -> None:
    view, read, _ = _wait([_view("completed", None), _view("completed", "success")])
    assert view.conclusion == "success"
    assert read.call_count == 2


def test_a_success_resets_the_read_failure_count() -> None:
    flaky = [_err("HTTP 503"), _view("in_progress")] * (MAX_READ_FAILURES + 2)
    view, _, _ = _wait([*flaky, _view("completed", "success")])
    assert view.conclusion == "success"


def test_a_persistent_read_failure_fails_loudly_as_one() -> None:
    with pytest.raises(ReleaseError, match="persistent read failure, not the run's result") as e:
        _wait([_err("HTTP 503: Service Unavailable")] * (MAX_READ_FAILURES + 1))
    assert e.value.phase == "confirm-main"
    assert e.value.detail is not None
    assert "503" in e.value.detail


def test_a_non_transient_read_error_fails_at_once() -> None:
    with (
        patch(_READ, side_effect=[_err("HTTP 403: Forbidden")]) as read,
        pytest.raises(ReleaseError, match="read failure, not the run's result"),
    ):
        wait_run_completed("o/r", "5", phase="cd-publish", sleep=MagicMock(), clock=lambda: 0.0)
    assert read.call_count == 1


def test_an_attempt_still_going_at_the_deadline_is_result_unknown() -> None:
    clock = itertools.count(0.0, 100.0)
    with (
        patch(_READ, return_value=_view("in_progress", attempt=3)),
        pytest.raises(
            ReleaseError,
            match=r"attempt 3 had not completed .*last read: attempt 3 in_progress.*unknown",
        ),
    ):
        wait_run_completed(
            "o/r",
            "5",
            phase="confirm-main",
            attempt=3,
            timeout=250,
            sleep=MagicMock(),
            clock=lambda: next(clock),
        )


def test_a_read_still_failing_at_the_deadline_is_a_read_failure() -> None:
    clock = itertools.count(0.0, 100.0)
    with (
        patch(_READ, side_effect=[_view("in_progress"), _err("HTTP 503")]),
        pytest.raises(ReleaseError, match="persistent read failure") as e,
    ):
        wait_run_completed(
            "o/r",
            "5",
            phase="confirm-main",
            timeout=150,
            sleep=MagicMock(),
            clock=lambda: next(clock),
        )
    assert e.value.detail is not None
    assert "503" in e.value.detail


def test_the_deadline_defaults_to_vrg_cd_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_CD_TIMEOUT", "30")
    clock = itertools.count(0.0, 20.0)
    sleep = MagicMock()
    with (
        patch(_READ, return_value=_view("queued")),
        pytest.raises(ReleaseError, match="had not completed"),
    ):
        wait_run_completed("o/r", "5", phase="confirm-main", sleep=sleep, clock=lambda: next(clock))
    # Sleeps are clamped to the deadline.
    assert all(call.args[0] <= 30 for call in sleep.call_args_list)
