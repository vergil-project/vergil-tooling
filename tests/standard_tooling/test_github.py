"""Tests for standard_tooling.lib.github."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from standard_tooling.lib import github


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_delegates_to_subprocess() -> None:
    with patch("standard_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.run("pr", "list")
    mock_run.assert_called_once_with(("gh", "pr", "list"), check=True)


def test_read_output_returns_stripped_stdout() -> None:
    with patch("standard_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed(stdout="  result\n")
        assert github.read_output("pr", "view") == "result"
    mock_run.assert_called_once_with(
        ("gh", "pr", "view"), check=True, text=True, capture_output=True
    )


def test_create_pr_returns_url() -> None:
    with patch("standard_tooling.lib.github.read_output", return_value="https://github.com/pr/1"):
        url = github.create_pr(base="main", title="title", body_file="body.md")
    assert url == "https://github.com/pr/1"


def test_wait_for_checks_skips_poll_when_already_registered() -> None:
    with (
        patch("standard_tooling.lib.github._checks_registered", return_value=True),
        patch("standard_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1")
    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch", "--fail-fast"
    )


def test_wait_for_checks_polls_until_registered() -> None:
    with (
        patch(
            "standard_tooling.lib.github._checks_registered",
            side_effect=[False, False, True],
        ),
        patch("standard_tooling.lib.github.time.sleep") as mock_sleep,
        patch("standard_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)

    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(5)
    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch", "--fail-fast"
    )


def test_wait_for_checks_proceeds_after_timeout() -> None:
    # monotonic: [initial (deadline), loop iter1 check, loop iter2 check (expired)]
    with (
        patch("standard_tooling.lib.github._checks_registered", return_value=False),
        patch(
            "standard_tooling.lib.github.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ),
        patch("standard_tooling.lib.github.time.sleep"),
        patch("standard_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)

    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch", "--fail-fast"
    )


def test_wait_for_checks_uses_poll_interval_for_sleep() -> None:
    with (
        patch(
            "standard_tooling.lib.github._checks_registered",
            side_effect=[False, True],
        ),
        patch("standard_tooling.lib.github.time.sleep") as mock_sleep,
        patch("standard_tooling.lib.github.run"),
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=10, poll_timeout=60)

    mock_sleep.assert_called_once_with(10)


def test_merge_state_status_returns_clean() -> None:
    with patch("standard_tooling.lib.github.read_output", return_value="CLEAN"):
        assert github.merge_state_status("https://github.com/pr/1") == "CLEAN"


def test_merge_state_status_returns_behind() -> None:
    with patch("standard_tooling.lib.github.read_output", return_value="BEHIND"):
        assert github.merge_state_status("https://github.com/pr/1") == "BEHIND"


def test_update_branch_calls_api() -> None:
    with patch(
        "standard_tooling.lib.github.read_output",
        side_effect=["42", "acme/repo", ""],
    ) as mock_read:
        github.update_branch("https://github.com/pr/1")
    calls = mock_read.call_args_list
    assert calls[0].args == (
        "pr",
        "view",
        "https://github.com/pr/1",
        "--json",
        "number",
        "--jq",
        ".number",
    )
    assert calls[1].args == (
        "repo",
        "view",
        "--json",
        "nameWithOwner",
        "--jq",
        ".nameWithOwner",
    )
    assert calls[2].args == (
        "api",
        "repos/acme/repo/pulls/42/update-branch",
        "-X",
        "PUT",
    )


def test_merge_delegates_to_gh() -> None:
    with patch("standard_tooling.lib.github.run") as mock_run:
        github.merge("https://github.com/pr/1", strategy="merge")
    mock_run.assert_called_once_with("pr", "merge", "--merge", "https://github.com/pr/1")


def test_merge_squash_strategy() -> None:
    with patch("standard_tooling.lib.github.run") as mock_run:
        github.merge("https://github.com/pr/1", strategy="squash")
    mock_run.assert_called_once_with("pr", "merge", "--squash", "https://github.com/pr/1")


def test_list_project_repos() -> None:
    with patch(
        "standard_tooling.lib.github.read_output",
        return_value="acme/repo-b\nacme/repo-a\nacme/repo-a\n",
    ):
        repos = github.list_project_repos("acme", "5")
    assert repos == ["acme/repo-a", "acme/repo-b"]


def test_list_project_repos_empty() -> None:
    with patch(
        "standard_tooling.lib.github.read_output",
        return_value="",
    ):
        assert github.list_project_repos("acme", "5") == []


def test_read_json_returns_parsed_dict() -> None:
    payload = {"name": "test", "value": 42}
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r")
    assert result == payload


def test_read_json_returns_parsed_list() -> None:
    payload = [{"id": 1}, {"id": 2}]
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r/rulesets")
    assert result == payload


def test_checks_registered_returns_false_when_phrase_in_stdout() -> None:
    cp = _completed(returncode=1, stdout="no checks reported on the 'main' branch\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is False


def test_checks_registered_returns_false_when_phrase_in_stderr() -> None:
    cp = _completed(returncode=1, stderr="no checks reported on the 'main' branch\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is False


def test_checks_registered_returns_true_when_checks_exist() -> None:
    cp = _completed(stdout="ci/tests\tpass\nhttps://example.com\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is True


def test_write_json_sends_body_via_stdin() -> None:
    with patch("standard_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.write_json("PATCH", "repos/o/r", {"key": "value"})
    mock_run.assert_called_once_with(
        ("gh", "api", "repos/o/r", "-X", "PATCH", "--input", "-"),
        input='{"key": "value"}',
        check=True,
        text=True,
        capture_output=True,
    )


def test_write_json_put_method() -> None:
    with patch("standard_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.write_json("PUT", "repos/o/r/actions/permissions", {"allowed_actions": "all"})
    call_args = mock_run.call_args
    expected_cmd = ("gh", "api", "repos/o/r/actions/permissions", "-X", "PUT", "--input", "-")
    assert call_args[0][0] == expected_cmd
    assert json.loads(call_args[1]["input"]) == {"allowed_actions": "all"}


def test_delete_calls_gh_api() -> None:
    with patch("standard_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.delete("repos/o/r/vulnerability-alerts")
    mock_run.assert_called_once_with(
        ("gh", "api", "repos/o/r/vulnerability-alerts", "-X", "DELETE"),
        check=True,
        text=True,
        capture_output=True,
    )


def test_delete_if_exists_returns_true_on_success() -> None:
    cp = _completed(stdout="HTTP/2.0 204 No Content\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github.delete_if_exists("repos/o/r/branches/main/protection") is True


def test_delete_if_exists_returns_false_on_404() -> None:
    cp = _completed(returncode=1, stdout="HTTP/2.0 404 Not Found\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github.delete_if_exists("repos/o/r/branches/main/protection") is False


def test_delete_if_exists_returns_true_on_empty_stdout() -> None:
    cp = _completed(stdout="")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github.delete_if_exists("repos/o/r/branches/main/protection") is True


# --- Retry logic ---


def _api_error(
    returncode: int = 1, stderr: str = "", stdout: str = ""
) -> subprocess.CalledProcessError:
    exc = subprocess.CalledProcessError(returncode=returncode, cmd=["gh"])
    exc.stderr = stderr
    exc.stdout = stdout
    return exc


class TestIsRetryable:
    @pytest.mark.parametrize(
        "stderr",
        [
            "HTTP 502 Bad Gateway",
            "HTTP 503 Service Unavailable",
            "HTTP 504 Gateway Timeout",
            "HTTP 429 rate limit exceeded",
            "request timed out",
            "connection reset by peer",
        ],
    )
    def test_retryable_errors(self, stderr: str) -> None:
        assert github._is_retryable(_api_error(stderr=stderr)) is True

    def test_retryable_error_in_stdout(self) -> None:
        assert github._is_retryable(_api_error(stdout="HTTP 504")) is True

    def test_non_retryable_error(self) -> None:
        assert github._is_retryable(_api_error(stderr="HTTP 404 Not Found")) is False

    def test_empty_output(self) -> None:
        assert github._is_retryable(_api_error()) is False


class TestRunWithRetry:
    def test_succeeds_on_first_attempt(self) -> None:
        with patch("standard_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="ok")
            result = github._run_with_retry(("gh", "pr", "view"), check=True)
        assert result.stdout == "ok"
        assert mock_run.call_count == 1

    def test_retries_on_504_then_succeeds(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "standard_tooling.lib.github.subprocess.run",
                side_effect=[err, err, _completed(stdout="ok")],
            ) as mock_run,
            patch("standard_tooling.lib.github.time.sleep") as mock_sleep,
            patch("standard_tooling.lib.github.random.random", return_value=0.5),
        ):
            result = github._run_with_retry(("gh", "pr", "view"), check=True)
        assert result.stdout == "ok"
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2

    def test_raises_after_max_retries(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "standard_tooling.lib.github.subprocess.run",
                side_effect=err,
            ),
            patch("standard_tooling.lib.github.time.sleep"),
            patch("standard_tooling.lib.github.random.random", return_value=0.5),
            pytest.raises(subprocess.CalledProcessError),
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)

    def test_raises_immediately_on_non_retryable_error(self) -> None:
        err = _api_error(stderr="HTTP 404 Not Found")
        with (
            patch("standard_tooling.lib.github.subprocess.run", side_effect=err),
            patch("standard_tooling.lib.github.time.sleep") as mock_sleep,
            pytest.raises(subprocess.CalledProcessError),
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)
        mock_sleep.assert_not_called()

    def test_backoff_delay_increases(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "standard_tooling.lib.github.subprocess.run",
                side_effect=[err, err, err, _completed()],
            ),
            patch("standard_tooling.lib.github.time.sleep") as mock_sleep,
            patch("standard_tooling.lib.github.random.random", return_value=0.5),
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays[0] < delays[1] < delays[2]


class TestRetryIntegration:
    def test_run_retries_on_504(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "standard_tooling.lib.github.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("standard_tooling.lib.github.time.sleep"),
            patch("standard_tooling.lib.github.random.random", return_value=0.5),
        ):
            github.run("pr", "list")
        assert mock_run.call_count == 2

    def test_read_output_retries_on_502(self) -> None:
        err = _api_error(stderr="HTTP 502 Bad Gateway")
        with (
            patch(
                "standard_tooling.lib.github.subprocess.run",
                side_effect=[err, _completed(stdout="result\n")],
            ) as mock_run,
            patch("standard_tooling.lib.github.time.sleep"),
            patch("standard_tooling.lib.github.random.random", return_value=0.5),
        ):
            assert github.read_output("pr", "view") == "result"
        assert mock_run.call_count == 2

    def test_write_json_retries_on_503(self) -> None:
        err = _api_error(stderr="HTTP 503 Service Unavailable")
        with (
            patch(
                "standard_tooling.lib.github.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("standard_tooling.lib.github.time.sleep"),
            patch("standard_tooling.lib.github.random.random", return_value=0.5),
        ):
            github.write_json("PATCH", "repos/o/r", {"key": "val"})
        assert mock_run.call_count == 2

    def test_delete_retries_on_429(self) -> None:
        err = _api_error(stderr="HTTP 429 rate limit exceeded")
        with (
            patch(
                "standard_tooling.lib.github.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("standard_tooling.lib.github.time.sleep"),
            patch("standard_tooling.lib.github.random.random", return_value=0.5),
        ):
            github.delete("repos/o/r/vulnerability-alerts")
        assert mock_run.call_count == 2
