"""Tests for vergil_tooling.lib.github."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import github

_real_gh_env = github._gh_env


@pytest.fixture(autouse=True)
def _no_credential_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.github._gh_env", lambda: None)
    github._human_token.cache_clear()


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_delegates_to_subprocess() -> None:
    with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.run("pr", "list")
    mock_run.assert_called_once_with(("gh", "pr", "list"), check=True)


def test_read_output_returns_stripped_stdout() -> None:
    with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed(stdout="  result\n")
        assert github.read_output("pr", "view") == "result"
    mock_run.assert_called_once_with(
        ("gh", "pr", "view"), check=True, text=True, capture_output=True
    )


def test_create_pr_returns_url() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="https://github.com/pr/1"):
        url = github.create_pr(base="main", title="title", body_file="body.md")
    assert url == "https://github.com/pr/1"


def test_wait_for_checks_skips_poll_when_already_registered() -> None:
    with (
        patch("vergil_tooling.lib.github._checks_registered", return_value=True),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1")
    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch", "--fail-fast"
    )


def test_wait_for_checks_polls_until_registered() -> None:
    with (
        patch(
            "vergil_tooling.lib.github._checks_registered",
            side_effect=[False, False, True],
        ),
        patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
        patch("vergil_tooling.lib.github.run") as mock_run,
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
        patch("vergil_tooling.lib.github._checks_registered", return_value=False),
        patch(
            "vergil_tooling.lib.github.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ),
        patch("vergil_tooling.lib.github.time.sleep"),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)

    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch", "--fail-fast"
    )


def test_wait_for_checks_uses_poll_interval_for_sleep() -> None:
    with (
        patch(
            "vergil_tooling.lib.github._checks_registered",
            side_effect=[False, True],
        ),
        patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
        patch("vergil_tooling.lib.github.run"),
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=10, poll_timeout=60)

    mock_sleep.assert_called_once_with(10)


def test_mergeable_returns_conflicting() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="CONFLICTING"):
        assert github.mergeable("https://github.com/pr/1") == "CONFLICTING"


def test_merge_state_status_returns_clean() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="CLEAN"):
        assert github.merge_state_status("https://github.com/pr/1") == "CLEAN"


def test_merge_state_status_returns_behind() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="BEHIND"):
        assert github.merge_state_status("https://github.com/pr/1") == "BEHIND"


def test_update_branch_calls_api() -> None:
    with patch(
        "vergil_tooling.lib.github.read_output",
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
    with patch("vergil_tooling.lib.github.run") as mock_run:
        github.merge("https://github.com/pr/1", strategy="merge")
    mock_run.assert_called_once_with("pr", "merge", "--merge", "https://github.com/pr/1")


def test_merge_squash_strategy() -> None:
    with patch("vergil_tooling.lib.github.run") as mock_run:
        github.merge("https://github.com/pr/1", strategy="squash")
    mock_run.assert_called_once_with("pr", "merge", "--squash", "https://github.com/pr/1")


def test_list_project_repos() -> None:
    with patch(
        "vergil_tooling.lib.github.read_output",
        return_value="acme/repo-b\nacme/repo-a\nacme/repo-a\n",
    ):
        repos = github.list_project_repos("acme", "5")
    assert repos == ["acme/repo-a", "acme/repo-b"]


def test_list_project_repos_empty() -> None:
    with patch(
        "vergil_tooling.lib.github.read_output",
        return_value="",
    ):
        assert github.list_project_repos("acme", "5") == []


def test_read_json_returns_parsed_dict() -> None:
    payload = {"name": "test", "value": 42}
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r")
    assert result == payload


def test_read_json_returns_parsed_list() -> None:
    payload = [{"id": 1}, {"id": 2}]
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r/rulesets")
    assert result == payload


def test_checks_registered_returns_false_when_phrase_in_stdout() -> None:
    cp = _completed(returncode=1, stdout="no checks reported on the 'main' branch\n")
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is False


def test_checks_registered_returns_false_when_phrase_in_stderr() -> None:
    cp = _completed(returncode=1, stderr="no checks reported on the 'main' branch\n")
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is False


def test_checks_registered_returns_true_when_checks_exist() -> None:
    cp = _completed(stdout="ci/tests\tpass\nhttps://example.com\n")
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is True


def test_write_json_sends_body_via_stdin() -> None:
    with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
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
    with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.write_json("PUT", "repos/o/r/actions/permissions", {"allowed_actions": "all"})
    call_args = mock_run.call_args
    expected_cmd = ("gh", "api", "repos/o/r/actions/permissions", "-X", "PUT", "--input", "-")
    assert call_args[0][0] == expected_cmd
    assert json.loads(call_args[1]["input"]) == {"allowed_actions": "all"}


def test_delete_calls_gh_api() -> None:
    with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
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
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        assert github.delete_if_exists("repos/o/r/branches/main/protection") is True


def test_delete_if_exists_returns_false_on_404() -> None:
    cp = _completed(returncode=1, stdout="HTTP/2.0 404 Not Found\n")
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        assert github.delete_if_exists("repos/o/r/branches/main/protection") is False


def test_delete_if_exists_returns_true_on_empty_stdout() -> None:
    cp = _completed(stdout="")
    with patch("vergil_tooling.lib.github.subprocess.run", return_value=cp):
        assert github.delete_if_exists("repos/o/r/branches/main/protection") is True


# --- GitHubAPIError ---


class TestGitHubAPIError:
    def test_is_subclass_of_called_process_error(self) -> None:
        assert issubclass(github.GitHubAPIError, subprocess.CalledProcessError)

    def test_str_includes_stderr(self) -> None:
        exc = github.GitHubAPIError(1, ["gh"], stderr="Validation Failed")
        assert "Validation Failed" in str(exc)

    def test_str_includes_stdout(self) -> None:
        exc = github.GitHubAPIError(1, ["gh"], output='{"message": "Not Found"}')
        assert "Not Found" in str(exc)

    def test_str_includes_both(self) -> None:
        exc = github.GitHubAPIError(1, ["gh"], output='{"message": "Bad"}', stderr="HTTP 422")
        msg = str(exc)
        assert "HTTP 422" in msg
        assert "Bad" in msg

    def test_str_falls_back_to_base_when_no_output(self) -> None:
        exc = github.GitHubAPIError(1, ["gh"])
        assert str(exc) == str(subprocess.CalledProcessError(1, ["gh"]))


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
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="ok")
            result = github._run_with_retry(("gh", "pr", "view"), check=True)
        assert result.stdout == "ok"
        assert mock_run.call_count == 1

    def test_retries_on_504_then_succeeds(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "vergil_tooling.lib.github.subprocess.run",
                side_effect=[err, err, _completed(stdout="ok")],
            ) as mock_run,
            patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
            patch("vergil_tooling.lib.github.random.random", return_value=0.5),
        ):
            result = github._run_with_retry(("gh", "pr", "view"), check=True)
        assert result.stdout == "ok"
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2

    def test_raises_after_max_retries(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "vergil_tooling.lib.github.subprocess.run",
                side_effect=err,
            ),
            patch("vergil_tooling.lib.github.time.sleep"),
            patch("vergil_tooling.lib.github.random.random", return_value=0.5),
            pytest.raises(github.GitHubAPIError, match="Gateway Timeout"),
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)

    def test_raises_immediately_on_non_retryable_error(self) -> None:
        err = _api_error(stderr="HTTP 404 Not Found")
        with (
            patch("vergil_tooling.lib.github.subprocess.run", side_effect=err),
            patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
            pytest.raises(github.GitHubAPIError, match="404 Not Found"),
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)
        mock_sleep.assert_not_called()

    def test_raises_plain_error_when_no_captured_output(self) -> None:
        err = subprocess.CalledProcessError(returncode=1, cmd=["gh"])
        err.stderr = ""
        err.stdout = ""
        with (
            patch("vergil_tooling.lib.github.subprocess.run", side_effect=err),
            pytest.raises(subprocess.CalledProcessError) as exc_info,
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)
        assert type(exc_info.value) is subprocess.CalledProcessError

    def test_backoff_delay_increases(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "vergil_tooling.lib.github.subprocess.run",
                side_effect=[err, err, err, _completed()],
            ),
            patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
            patch("vergil_tooling.lib.github.random.random", return_value=0.5),
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays[0] < delays[1] < delays[2]


class TestRetryIntegration:
    def test_run_retries_on_504(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "vergil_tooling.lib.github.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("vergil_tooling.lib.github.time.sleep"),
            patch("vergil_tooling.lib.github.random.random", return_value=0.5),
        ):
            github.run("pr", "list")
        assert mock_run.call_count == 2

    def test_read_output_retries_on_502(self) -> None:
        err = _api_error(stderr="HTTP 502 Bad Gateway")
        with (
            patch(
                "vergil_tooling.lib.github.subprocess.run",
                side_effect=[err, _completed(stdout="result\n")],
            ) as mock_run,
            patch("vergil_tooling.lib.github.time.sleep"),
            patch("vergil_tooling.lib.github.random.random", return_value=0.5),
        ):
            assert github.read_output("pr", "view") == "result"
        assert mock_run.call_count == 2

    def test_write_json_retries_on_503(self) -> None:
        err = _api_error(stderr="HTTP 503 Service Unavailable")
        with (
            patch(
                "vergil_tooling.lib.github.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("vergil_tooling.lib.github.time.sleep"),
            patch("vergil_tooling.lib.github.random.random", return_value=0.5),
        ):
            github.write_json("PATCH", "repos/o/r", {"key": "val"})
        assert mock_run.call_count == 2

    def test_delete_retries_on_429(self) -> None:
        err = _api_error(stderr="HTTP 429 rate limit exceeded")
        with (
            patch(
                "vergil_tooling.lib.github.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("vergil_tooling.lib.github.time.sleep"),
            patch("vergil_tooling.lib.github.random.random", return_value=0.5),
        ):
            github.delete("repos/o/r/vulnerability-alerts")
        assert mock_run.call_count == 2


# --- Credential discovery ---


_AUTH_TWO_ACCOUNTS = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
  ✓ Logged in to github.com account jdoe-vergil (keyring)
  - Active account: false
"""

_AUTH_MANY_ACCOUNTS = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
  ✓ Logged in to github.com account jdoe-vergil (keyring)
  - Active account: false
  ✓ Logged in to github.com account jdoe-mimir (keyring)
  - Active account: false
  ✓ Logged in to github.com account jdoe-agent (keyring)
  - Active account: false
"""

_AUTH_NO_VERGIL = """\
github.com
  ✓ Logged in to github.com account jdoe (keyring)
  - Active account: true
"""


class TestDiscoverAccounts:
    def test_finds_vergil_pair(self) -> None:
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_AUTH_TWO_ACCOUNTS)
            human, agent = github._discover_accounts()
        assert human == "jdoe"
        assert agent == "jdoe-vergil"

    def test_ignores_other_accounts(self) -> None:
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=_AUTH_MANY_ACCOUNTS)
            human, agent = github._discover_accounts()
        assert human == "jdoe"
        assert agent == "jdoe-vergil"

    def test_fails_without_vergil_account(self) -> None:
        with (
            patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
            pytest.raises(SystemExit),
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout=_AUTH_NO_VERGIL)
            github._discover_accounts()

    def test_derives_human_from_vergil_suffix(self) -> None:
        auth = "github.com\n  ✓ Logged in to github.com account alice-vergil (keyring)\n"
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=auth)
            human, agent = github._discover_accounts()
        assert human == "alice"
        assert agent == "alice-vergil"


# --- Credential injection ---


class TestCredentialInjection:
    def test_run_with_retry_injects_env(self) -> None:
        fake_env = {"GH_TOKEN": "test-token", "PATH": "/usr/bin"}
        with (
            patch("vergil_tooling.lib.github._gh_env", return_value=fake_env),
            patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            github._run_with_retry(("gh", "pr", "list"), check=True)
        _, kwargs = mock_run.call_args
        assert kwargs["env"] is fake_env

    def test_run_with_retry_skips_env_when_none(self) -> None:
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = _completed()
            github._run_with_retry(("gh", "pr", "list"), check=True)
        _, kwargs = mock_run.call_args
        assert "env" not in kwargs

    def test_run_with_retry_preserves_caller_env(self) -> None:
        caller_env = {"GH_TOKEN": "caller-token"}
        with (
            patch("vergil_tooling.lib.github._gh_env", return_value={"GH_TOKEN": "other"}),
            patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            github._run_with_retry(("gh", "pr", "list"), check=True, env=caller_env)
        _, kwargs = mock_run.call_args
        assert kwargs["env"] is caller_env


# --- _human_token ---


class TestHumanToken:
    def test_returns_stripped_token(self) -> None:
        with (
            patch(
                "vergil_tooling.lib.github._discover_accounts",
                return_value=("jdoe", "jdoe-vergil"),
            ),
            patch("vergil_tooling.lib.github.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0, stdout="ghp_abc123\n")
            token = github._human_token()
        assert token == "ghp_abc123"  # noqa: S105


# --- _gh_env ---


class TestGhEnv:
    def test_returns_env_with_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("vergil_tooling.lib.github._gh_env", _real_gh_env)
        with patch("vergil_tooling.lib.github._human_token", return_value="ghp_abc123"):
            env = github._gh_env()
        assert env is not None
        assert env["GH_TOKEN"] == "ghp_abc123"  # noqa: S105

    def test_returns_none_on_subprocess_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("vergil_tooling.lib.github._gh_env", _real_gh_env)
        with patch(
            "vergil_tooling.lib.github._human_token",
            side_effect=subprocess.CalledProcessError(1, "gh"),
        ):
            assert github._gh_env() is None

    def test_returns_none_on_system_exit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("vergil_tooling.lib.github._gh_env", _real_gh_env)
        with patch(
            "vergil_tooling.lib.github._human_token",
            side_effect=SystemExit(1),
        ):
            assert github._gh_env() is None
