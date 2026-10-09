"""Tests for vergil_tooling.lib.retry."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import retry

if TYPE_CHECKING:
    from collections.abc import Callable


def _api_error(
    returncode: int = 1, stderr: str = "", stdout: str = ""
) -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(returncode=returncode, cmd=["gh"])
    exc.stderr = stderr
    exc.stdout = stdout
    return exc


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestIsRetryable:
    @pytest.mark.parametrize(
        "stderr",
        [
            "HTTP 502 Bad Gateway",
            "HTTP 503 Service Unavailable",
            "HTTP 504 Gateway Timeout",
            "HTTP 429 rate limit exceeded",
            "HTTP 401: Bad credentials (https://api.github.com/graphql)",
            "Bad credentials",
            "request timed out",
            "connection reset by peer",
            # net/http transport-layer transients
            'Post "https://api.github.com/graphql": net/http: TLS handshake timeout',
            'Get "https://api.github.com/...": net/http: TLS handshake timeout',
            "dial tcp: i/o timeout",
            "dial tcp 140.82.112.5:443: connect: connection refused",
            "lookup api.github.com on 127.0.0.53:53: no such host",
            "lookup api.github.com: server misbehaving",
            "unexpected EOF",
        ],
    )
    def test_retryable_errors(self, stderr: str) -> None:
        assert retry.is_retryable(_api_error(stderr=stderr)) is True

    def test_retryable_error_in_stdout(self) -> None:
        assert retry.is_retryable(_api_error(stdout="HTTP 504")) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            # gh's 502/503/504 phrasing during the 2026-08-13 incident, which
            # carries no "HTTP <code>" token so the code-only patterns miss it.
            'non-200 OK status code: 502 Bad Gateway body: "<html>..."',
            "502 Bad Gateway",
            "error: 503 Service Unavailable",
            "504 Gateway Timeout",
            "504 Gateway Time-out",
            # raw git transport transients (git never reached retry before #2835)
            "fatal: Could not read from remote repository.",
            "fatal: the remote end hung up unexpectedly",
            "Connection closed by 140.82.112.3 port 22",
            "kex_exchange_identification: Connection closed by remote host",
            # SSH auth backend hiccup rejecting a valid, static key: retried
            # loudly per the issue #2835 decision (a real misconfig still
            # surfaces after the loud retries and the final raise).
            "git@github.com: Permission denied (publickey).",
        ],
    )
    def test_retryable_transport_and_incident_errors(self, stderr: str) -> None:
        assert retry.is_retryable(_api_error(stderr=stderr)) is True

    @pytest.mark.parametrize(
        "stderr",
        [
            # GitHub's generic GraphQL server-side failure, as gh renders it
            # (seen in vrg-release confirm/wait stages, #3148).
            "GraphQL: Something went wrong while executing your query. Please include "
            "`C0DE:1234:ABCD:5678:9EF0` when reporting this issue.",
            "Something went wrong while executing your query. This may be the result of a timeout,"
            " or it could be a GitHub bug.",
        ],
    )
    def test_retryable_graphql_something_went_wrong(self, stderr: str) -> None:
        assert retry.is_retryable(_api_error(stderr=stderr)) is True

    def test_generic_something_went_wrong_is_not_retryable(self) -> None:
        # Only GitHub's exact GraphQL phrasing is transient; a bare
        # "something went wrong" from any other source must still fail fast.
        assert retry.is_retryable(_api_error(stderr="error: something went wrong")) is False

    @pytest.mark.parametrize(
        "stderr",
        [
            "HTTP 404 Not Found",
            "HTTP 422 Unprocessable Entity",
            "GraphQL: Pull request is not mergeable (mergePullRequest)",
            "could not resolve to a Repository with the name 'x/y'",
            "HTTP 403: Resource not accessible by integration",
        ],
    )
    def test_non_retryable_error(self, stderr: str) -> None:
        assert retry.is_retryable(_api_error(stderr=stderr)) is False

    def test_empty_output(self) -> None:
        assert retry.is_retryable(_api_error()) is False


class TestComputeDelay:
    def test_increases_with_attempt(self) -> None:
        with patch("vergil_tooling.lib.retry.random.random", return_value=0.5):
            delays = [retry.compute_delay(i) for i in range(4)]
        assert delays[0] < delays[1] < delays[2] < delays[3]

    def test_capped_at_max(self) -> None:
        with patch("vergil_tooling.lib.retry.random.random", return_value=0.5):
            delay = retry.compute_delay(100)
        assert delay <= retry.MAX_DELAY_SECS * 1.5

    def test_jitter_range(self) -> None:
        with patch("vergil_tooling.lib.retry.random.random", return_value=0.0):
            low = retry.compute_delay(0)
        with patch("vergil_tooling.lib.retry.random.random", return_value=1.0):
            high = retry.compute_delay(0)
        assert low < high
        assert low == retry.BASE_DELAY_SECS * 0.5
        assert high == retry.BASE_DELAY_SECS * 1.5


class TestRunWithRetry:
    def test_succeeds_on_first_attempt(self) -> None:
        with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="ok")
            result = retry.run_with_retry(("gh", "pr", "view"), check=True)
        assert result.stdout == "ok"
        assert mock_run.call_count == 1

    def test_retries_on_504_then_succeeds(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, err, _completed(stdout="ok")],
            ) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            result = retry.run_with_retry(("gh", "pr", "view"), check=True)
        assert result.stdout == "ok"
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2

    def test_raises_after_max_retries(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
            pytest.raises(subprocess.CalledProcessError, match=""),
        ):
            retry.run_with_retry(("gh", "pr", "view"), check=True)

    def test_retries_graphql_blip_then_succeeds(self) -> None:
        err = _api_error(stderr="GraphQL: Something went wrong while executing your query.")
        with (
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, _completed(stdout="ok")],
            ) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            result = retry.run_with_retry(("gh", "pr", "view"), check=True)
        assert result.stdout == "ok"
        assert mock_run.call_count == 2
        assert mock_sleep.call_count == 1

    def test_persistent_graphql_failure_raises_original_error(self) -> None:
        err = _api_error(stderr="GraphQL: Something went wrong while executing your query.")
        with (
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
            pytest.raises(subprocess.CalledProcessError) as excinfo,
        ):
            retry.run_with_retry(("gh", "pr", "view"), check=True)
        assert excinfo.value is err
        assert mock_run.call_count == retry.MAX_RETRIES + 1
        assert mock_sleep.call_count == retry.MAX_RETRIES

    def test_raises_immediately_on_non_retryable(self) -> None:
        err = _api_error(stderr="HTTP 404 Not Found")
        with (
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            pytest.raises(subprocess.CalledProcessError),
        ):
            retry.run_with_retry(("gh", "pr", "view"), check=True)
        mock_sleep.assert_not_called()

    def test_backoff_delay_increases(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, err, err, _completed()],
            ),
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            retry.run_with_retry(("gh", "pr", "view"), check=True)
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays[0] < delays[1] < delays[2]


class TestCallWithRetry:
    """The callable form behind run_with_retry, for injected ``Run`` transports (#3153)."""

    def test_returns_first_success_without_sleeping(self) -> None:
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            return "ok"

        with patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep:
            assert retry.call_with_retry(fn) == "ok"
        assert calls == [1]
        mock_sleep.assert_not_called()

    def test_retries_transient_then_succeeds(self) -> None:
        outcomes: list[Any] = [_api_error(stderr="HTTP 502"), "ok"]

        def fn() -> str:
            out = outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return str(out)

        with (
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            assert retry.call_with_retry(fn) == "ok"
        assert mock_sleep.call_count == 1

    def test_persistent_transient_raises_original_after_budget(self) -> None:
        err = _api_error(stderr="HTTP 503")
        calls: list[int] = []

        def fn() -> str:
            calls.append(1)
            raise err

        with (
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            pytest.raises(subprocess.CalledProcessError) as excinfo,
        ):
            retry.call_with_retry(fn)
        assert excinfo.value is err
        assert len(calls) == retry.MAX_RETRIES + 1
        assert mock_sleep.call_count == retry.MAX_RETRIES

    def test_non_transient_raises_immediately(self) -> None:
        err = _api_error(stderr="release not found")

        def fn() -> str:
            raise err

        with (
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            pytest.raises(subprocess.CalledProcessError) as excinfo,
        ):
            retry.call_with_retry(fn)
        assert excinfo.value is err
        mock_sleep.assert_not_called()


class _FakeClock:
    """Deterministic monotonic clock whose ``sleep`` advances time instantly."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


def _sequence(*outcomes: object) -> tuple[list[int], Callable[[], object]]:
    """Return ``(calls, fn)`` where *fn* raises/returns *outcomes* in order."""
    calls: list[int] = []
    queue = list(outcomes)

    def fn() -> object:
        calls.append(1)
        outcome = queue.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return calls, fn


class TestIsNotFound:
    @pytest.mark.parametrize(
        "stderr",
        [
            "HTTP 404: Not Found (https://api.github.com/repos/o/r/actions/runs/1)",
            "release not found",
            "GraphQL: Could not resolve to a PullRequest with the number of 7.",
            "GraphQL: Could not resolve to an issue or pull request with the number of 7.",
        ],
    )
    def test_not_found_errors(self, stderr: str) -> None:
        assert retry.is_not_found(_api_error(stderr=stderr)) is True

    def test_not_found_in_stdout(self) -> None:
        assert retry.is_not_found(_api_error(stdout="HTTP 404")) is True

    @pytest.mark.parametrize("stderr", ["", "HTTP 422 Unprocessable Entity", "HTTP 403"])
    def test_other_errors(self, stderr: str) -> None:
        assert retry.is_not_found(_api_error(stderr=stderr)) is False

    def test_general_retry_still_treats_404_as_fatal(self) -> None:
        # The known-resource rule is opt-in per call: the general retryable set
        # must never absorb 404, or a genuinely missing resource would retry.
        assert retry.is_retryable(_api_error(stderr="HTTP 404: Not Found")) is False


class TestRetryKnownResource:
    def test_returns_immediately_on_success(self) -> None:
        clock = _FakeClock()
        calls, fn = _sequence("ok")
        result = retry.retry_known_resource(
            fn, resource="run 1", sleep=clock.sleep, clock=clock.monotonic
        )
        assert result == "ok"
        assert len(calls) == 1
        assert clock.sleeps == []

    def test_404_then_success(self) -> None:
        clock = _FakeClock()
        err = _api_error(stderr="HTTP 404: Not Found")
        calls, fn = _sequence(err, err, "https://github.com/o/r/actions/runs/1")
        with patch("vergil_tooling.lib.retry.random.random", return_value=0.5):
            result = retry.retry_known_resource(
                fn, resource="run 1", sleep=clock.sleep, clock=clock.monotonic
            )
            expected = [retry.compute_delay(0), retry.compute_delay(1)]
        assert result == "https://github.com/o/r/actions/runs/1"
        assert len(calls) == 3
        # Reuses compute_delay's exponential backoff.
        assert clock.sleeps == expected

    def test_404_until_budget_exhausted_raises_clear_error(self) -> None:
        clock = _FakeClock()
        err = _api_error(stderr="HTTP 404: Not Found")
        calls, fn = _sequence(*([err] * 50))
        with (
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
            pytest.raises(retry.KnownResourceNotFoundError) as excinfo,
        ):
            retry.retry_known_resource(
                fn, resource="CD run 37784562892", sleep=clock.sleep, clock=clock.monotonic
            )
        message = str(excinfo.value)
        assert "CD run 37784562892" in message
        assert "still returns 404 after 60s" in message
        assert "HTTP 404: Not Found" in message
        assert excinfo.value.__cause__ is err
        # Bounded by the time budget, not an attempt count: total sleep lands
        # exactly on the deadline (the last sleep is clamped), with one final
        # read at the deadline before giving up.
        assert sum(clock.sleeps) == pytest.approx(retry.KNOWN_RESOURCE_BUDGET_SECS)
        assert len(calls) == len(clock.sleeps) + 1

    def test_exhausted_error_is_a_called_process_error(self) -> None:
        clock = _FakeClock()
        err = _api_error(returncode=4, stderr="HTTP 404", stdout="body")
        _, fn = _sequence(*([err] * 50))
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            retry.retry_known_resource(
                fn, resource="run 1", budget=5.0, sleep=clock.sleep, clock=clock.monotonic
            )
        assert excinfo.value.returncode == 4
        assert excinfo.value.cmd == ["gh"]
        assert excinfo.value.stderr == "HTTP 404"
        assert excinfo.value.stdout == "body"
        assert "after 5s" in str(excinfo.value)

    def test_exhausted_error_without_output(self) -> None:
        clock = _FakeClock()
        err = subprocess.CalledProcessError(1, ["gh"], stderr="not found")
        _, fn = _sequence(*([err] * 50))
        with pytest.raises(retry.KnownResourceNotFoundError) as excinfo:
            retry.retry_known_resource(
                fn, resource="run 1", budget=1.0, sleep=clock.sleep, clock=clock.monotonic
            )
        assert str(excinfo.value).endswith("not found")

    def test_non_404_error_is_not_retried(self) -> None:
        clock = _FakeClock()
        err = _api_error(stderr="HTTP 422 Unprocessable Entity")
        calls, fn = _sequence(err, "never")
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            retry.retry_known_resource(
                fn, resource="run 1", sleep=clock.sleep, clock=clock.monotonic
            )
        assert excinfo.value is err
        assert len(calls) == 1
        assert clock.sleeps == []

    def test_defaults_use_real_time(self) -> None:
        err = _api_error(stderr="HTTP 404")
        calls, fn = _sequence(err, "ok")
        with patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep:
            assert retry.retry_known_resource(fn, resource="run 1") == "ok"
        assert len(calls) == 2
        mock_sleep.assert_called_once()
