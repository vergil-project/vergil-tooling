"""Tests for vergil_tooling.lib.retry."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vergil_tooling.lib import retry


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
