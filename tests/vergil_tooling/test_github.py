"""Tests for vergil_tooling.lib.github."""

from __future__ import annotations

import http.client
import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import github

_real_gh_env = github._gh_env


@pytest.fixture(autouse=True)
def _no_credential_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vergil_tooling.lib.github._gh_env", lambda: None)
    github._token_cache.clear()
    github._installation_cache = None


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_delegates_to_subprocess() -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.run("pr", "list")
    mock_run.assert_called_once_with(
        ("gh", "pr", "list"), check=True, capture_output=True, text=True
    )


def test_run_prints_captured_output(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed(stdout="PR merged\n", stderr="warning\n")
        github.run("pr", "merge")
    captured = capsys.readouterr()
    assert "PR merged" in captured.out
    assert "warning" in captured.err


def test_read_output_returns_stripped_stdout() -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed(stdout="  result\n")
        assert github.read_output("pr", "view") == "result"
    mock_run.assert_called_once_with(
        ("gh", "pr", "view"), check=True, text=True, capture_output=True
    )


def test_current_org_returns_remote_owner() -> None:
    with patch("vergil_tooling.lib.github.current_repo", return_value="logical-minds-foundry/docs"):
        assert github.current_org() == "logical-minds-foundry"


def test_create_pr_returns_url() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="https://github.com/pr/1"):
        url = github.create_pr(base="main", title="title", body_file="body.md")
    assert url == "https://github.com/pr/1"


def test_create_issue_returns_url_and_builds_command() -> None:
    with patch(
        "vergil_tooling.lib.github.read_output",
        return_value="https://github.com/org/repo/issues/123",
    ) as mock_read:
        url = github.create_issue(
            repo="org/repo", title="T", body_file="body.md", labels=["bug", "epic"]
        )
    assert url == "https://github.com/org/repo/issues/123"
    args = mock_read.call_args.args
    assert args[:2] == ("issue", "create")
    assert "--repo" in args and "org/repo" in args
    assert "--title" in args and "T" in args
    assert "--body-file" in args and "body.md" in args
    assert args.count("--label") == 2


def test_create_issue_uses_inline_body_when_no_file() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="url") as mock_read:
        github.create_issue(repo="org/repo", title="T", body="hello")
    args = mock_read.call_args.args
    assert "--body" in args and "hello" in args
    assert "--body-file" not in args


def test_create_issue_includes_assignees() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="url") as mock_read:
        github.create_issue(repo="org/repo", title="T", assignees=["alice", "bob"])
    args = mock_read.call_args.args
    assert args.count("--assignee") == 2
    assert "alice" in args and "bob" in args


def test_edit_pr_body_passes_body_via_file() -> None:
    """edit_pr_body writes the body to a temp file and passes it to gh."""
    captured: dict[str, str] = {}

    def fake_run(*args: str) -> None:
        assert args[:3] == ("pr", "edit", "https://github.com/pr/1")
        assert args[3] == "--body-file"
        captured["body"] = Path(args[4]).read_text()

    with patch("vergil_tooling.lib.github.run", side_effect=fake_run):
        github.edit_pr_body("https://github.com/pr/1", body="# Pull Request\n\nnew body")
    assert captured["body"] == "# Pull Request\n\nnew body"


def test_edit_pr_body_cleans_up_temp_file() -> None:
    paths: list[Path] = []

    def fake_run(*args: str) -> None:
        paths.append(Path(args[4]))

    with patch("vergil_tooling.lib.github.run", side_effect=fake_run):
        github.edit_pr_body("123", body="body")
    assert paths and not paths[0].exists()


def test_wait_for_checks_resolves_sha_and_watches() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123") as mock_sha,
        patch("vergil_tooling.lib.github._checks_registered", return_value=True),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1")
    mock_sha.assert_called_once_with("https://github.com/pr/1")
    mock_run.assert_called_once_with("pr", "checks", "https://github.com/pr/1", "--watch")


def test_wait_for_checks_polls_until_registered() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch(
            "vergil_tooling.lib.github._checks_registered",
            side_effect=[False, False, True, True],
        ),
        patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(5)
    mock_run.assert_called_once_with("pr", "checks", "https://github.com/pr/1", "--watch")


def test_wait_for_checks_passes_repo_and_sha_to_checks_registered() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch("vergil_tooling.lib.github._checks_registered", return_value=True) as mock_reg,
        patch("vergil_tooling.lib.github.run"),
    ):
        github.wait_for_checks("https://github.com/pr/1")
    mock_reg.assert_called_with("owner/repo", "abc123")


def test_wait_for_checks_raises_after_timeout_with_sha() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123def456"),
        patch("vergil_tooling.lib.github._checks_registered", return_value=False),
        patch(
            "vergil_tooling.lib.github.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ),
        patch("vergil_tooling.lib.github.time.sleep"),
        patch("vergil_tooling.lib.github.run") as mock_run,
        pytest.raises(github.GitHubAPIError, match="abc123de"),
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)
    mock_run.assert_not_called()


def test_wait_for_checks_uses_poll_interval_for_sleep() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch(
            "vergil_tooling.lib.github._checks_registered",
            side_effect=[False, True, True],
        ),
        patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
        patch("vergil_tooling.lib.github.run"),
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=10, poll_timeout=60)
    mock_sleep.assert_called_once_with(10)


def test_wait_for_checks_does_not_use_fail_fast() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch("vergil_tooling.lib.github._checks_registered", return_value=True),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1")
    call_args = mock_run.call_args[0]
    assert "--fail-fast" not in call_args


def test_wait_for_checks_reresolves_head_while_polling() -> None:
    """The head SHA is re-resolved each poll — update-branch can move it (#1490)."""

    def registered(_repo: str, sha: str) -> bool:
        return sha == "new"

    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", side_effect=["old", "new"]) as mock_sha,
        patch("vergil_tooling.lib.github._checks_registered", side_effect=registered) as mock_reg,
        patch("vergil_tooling.lib.github.time.sleep"),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)
    assert mock_sha.call_count == 2
    assert mock_reg.call_args[0] == ("owner/repo", "new")
    mock_run.assert_called_once_with("pr", "checks", "https://github.com/pr/1", "--watch")


def test_wait_for_checks_restarts_watch_on_no_checks_reported() -> None:
    """A watch killed by "no checks reported" (head moved mid-watch) restarts (#1490)."""
    no_checks = github.GitHubAPIError(
        1,
        ("gh", "pr", "checks", "1", "--watch"),
        None,
        "no checks reported on the 'feature/x' branch",
    )
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", side_effect=["old", "new"]),
        patch("vergil_tooling.lib.github._checks_registered", return_value=True),
        patch("vergil_tooling.lib.github.time.sleep"),
        patch("vergil_tooling.lib.github.run", side_effect=[no_checks, None]) as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1")
    assert mock_run.call_count == 2


def test_wait_for_checks_propagates_other_watch_failures() -> None:
    """Only the "no checks reported" watch failure is transient; the rest raise."""
    boom = github.GitHubAPIError(1, ("gh",), None, "HTTP 401: Bad credentials")
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch("vergil_tooling.lib.github._checks_registered", return_value=True),
        patch("vergil_tooling.lib.github.run", side_effect=boom) as mock_run,
        pytest.raises(github.GitHubAPIError, match="Bad credentials"),
    ):
        github.wait_for_checks("https://github.com/pr/1")
    mock_run.assert_called_once()


def test_wait_for_checks_watch_restart_bounded_by_deadline() -> None:
    """The no-checks restart shares the poll deadline — past it, the error raises (#1490)."""
    no_checks = github.GitHubAPIError(
        1,
        ("gh", "pr", "checks", "1", "--watch"),
        None,
        "no checks reported on the 'feature/x' branch",
    )
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch("vergil_tooling.lib.github._checks_registered", return_value=True),
        patch("vergil_tooling.lib.github.time.monotonic", side_effect=[0.0, 61.0]),
        patch("vergil_tooling.lib.github.time.sleep"),
        patch("vergil_tooling.lib.github.run", side_effect=no_checks) as mock_run,
        pytest.raises(github.GitHubAPIError, match="no checks reported"),
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)
    mock_run.assert_called_once()


def test_failed_check_names_returns_failing_checks() -> None:
    payload = json.dumps(
        [
            {"name": "security / codeql", "bucket": "fail"},
            {"name": "build", "bucket": "pass"},
            {"name": "security / semgrep", "bucket": "fail"},
            {"name": "optional-lint", "bucket": "skipping"},
        ]
    )
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=8, stdout=payload)
        result = github.failed_check_names("https://github.com/pr/1")
    assert result == ["security / codeql", "security / semgrep"]
    mock_run.assert_called_once_with(
        ("gh", "pr", "checks", "https://github.com/pr/1", "--json", "name,bucket"),
        check=False,
        text=True,
        capture_output=True,
    )


def test_failed_check_names_empty_when_all_pass() -> None:
    payload = json.dumps(
        [
            {"name": "build", "bucket": "pass"},
            {"name": "optional-lint", "bucket": "skipping"},
        ]
    )
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0, stdout=payload)
        assert github.failed_check_names("https://github.com/pr/1") == []


def test_failed_check_names_treats_cancelled_as_failure() -> None:
    payload = json.dumps([{"name": "deploy", "bucket": "cancel"}])
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=8, stdout=payload)
        assert github.failed_check_names("https://github.com/pr/1") == ["deploy"]


def test_failed_check_names_raises_on_empty_output() -> None:
    with (
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        pytest.raises(github.GitHubAPIError),
    ):
        mock_run.return_value = _completed(returncode=1, stdout="", stderr="boom")
        github.failed_check_names("https://github.com/pr/1")


def test_pr_checks_returns_parsed_checks() -> None:
    payload = json.dumps(
        [
            {"name": "build", "bucket": "pass", "state": "SUCCESS"},
            {"name": "deploy", "bucket": "pending", "state": "IN_PROGRESS"},
        ]
    )
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=8, stdout=payload)
        result = github.pr_checks("https://github.com/pr/1")
    assert result == [
        {"name": "build", "bucket": "pass", "state": "SUCCESS"},
        {"name": "deploy", "bucket": "pending", "state": "IN_PROGRESS"},
    ]
    mock_run.assert_called_once_with(
        ("gh", "pr", "checks", "https://github.com/pr/1", "--json", "name,bucket,state"),
        check=False,
        text=True,
        capture_output=True,
    )


def test_pr_checks_empty_when_no_checks_reported() -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed(
            returncode=1, stdout="", stderr="no checks reported on the 'feature/x' branch"
        )
        assert github.pr_checks("https://github.com/pr/1") == []


def test_pr_checks_raises_on_empty_output_without_no_checks_message() -> None:
    with (
        patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        pytest.raises(github.GitHubAPIError),
    ):
        mock_run.return_value = _completed(returncode=1, stdout="", stderr="boom")
        github.pr_checks("https://github.com/pr/1")


def test_pr_reviews_returns_list() -> None:
    reviews = [{"id": "r1", "state": "APPROVED"}, {"id": "r2", "state": "COMMENTED"}]
    with patch("vergil_tooling.lib.github.read_json", return_value=reviews):
        assert github.pr_reviews("https://github.com/pr/1") == reviews


def test_pr_reviews_empty_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=[]):
        assert github.pr_reviews("https://github.com/pr/1") == []


def test_post_check_run_posts_completed_check() -> None:
    with patch("vergil_tooling.lib.github.write_json") as mock_write:
        github.post_check_run(
            "owner/repo",
            name="vergil-audit/approved",
            head_sha="abc123",
            conclusion="success",
            title="Approved",
            summary="Looks good.",
        )
    mock_write.assert_called_once_with(
        "POST",
        "repos/owner/repo/check-runs",
        {
            "name": "vergil-audit/approved",
            "head_sha": "abc123",
            "status": "completed",
            "conclusion": "success",
            "output": {"title": "Approved", "summary": "Looks good."},
        },
    )


def test_mergeable_returns_conflicting() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="CONFLICTING"):
        assert github.mergeable("https://github.com/pr/1") == "CONFLICTING"


def test_merge_state_status_returns_clean() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="CLEAN"):
        assert github.merge_state_status("https://github.com/pr/1") == "CLEAN"


def test_merge_state_status_returns_behind() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="BEHIND"):
        assert github.merge_state_status("https://github.com/pr/1") == "BEHIND"


def test_merge_status_returns_both_fields() -> None:
    with patch(
        "vergil_tooling.lib.github.read_json",
        return_value={"mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED"},
    ):
        result = github.merge_status("https://github.com/pr/1")
    assert result == {"mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED"}


def test_merge_status_with_empty_review_decision() -> None:
    with patch(
        "vergil_tooling.lib.github.read_json",
        return_value={"mergeStateStatus": "BLOCKED", "reviewDecision": ""},
    ):
        result = github.merge_status("https://github.com/pr/1")
    assert result == {"mergeStateStatus": "BLOCKED", "reviewDecision": ""}


def test_merge_status_raises_on_non_dict_response() -> None:
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        pytest.raises(github.GitHubAPIError),
    ):
        github.merge_status("https://github.com/pr/1")


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


def test_head_sha_returns_commit_sha() -> None:
    with patch(
        "vergil_tooling.lib.github.read_output",
        return_value="abc123def456",
    ) as mock_read:
        result = github.head_sha("https://github.com/pr/1")
    assert result == "abc123def456"
    mock_read.assert_called_once_with(
        "pr",
        "view",
        "https://github.com/pr/1",
        "--json",
        "headRefOid",
        "--jq",
        ".headRefOid",
    )


def test_pr_state_returns_state() -> None:
    with patch(
        "vergil_tooling.lib.github.read_output",
        return_value="MERGED",
    ) as mock_read:
        result = github.pr_state("https://github.com/pr/1")
    assert result == "MERGED"
    mock_read.assert_called_once_with(
        "pr",
        "view",
        "https://github.com/pr/1",
        "--json",
        "state",
        "--jq",
        ".state",
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
    with patch("vergil_tooling.lib.retry.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r")
    assert result == payload


def test_read_json_returns_parsed_list() -> None:
    payload = [{"id": 1}, {"id": 2}]
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("vergil_tooling.lib.retry.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r/rulesets")
    assert result == payload


def test_checks_registered_returns_true_when_checks_exist() -> None:
    cp = _completed(stdout="1\n")
    with patch("vergil_tooling.lib.retry.subprocess.run", return_value=cp):
        assert github._checks_registered("owner/repo", "abc123") is True


def test_checks_registered_returns_false_when_no_checks() -> None:
    cp = _completed(stdout="0\n")
    with patch("vergil_tooling.lib.retry.subprocess.run", return_value=cp):
        assert github._checks_registered("owner/repo", "abc123") is False


def test_checks_registered_calls_correct_api_endpoint() -> None:
    cp = _completed(stdout="0\n")
    with patch("vergil_tooling.lib.retry.subprocess.run", return_value=cp) as mock_run:
        github._checks_registered("owner/repo", "abc123def456")
    args = mock_run.call_args[0][0]
    assert "repos/owner/repo/commits/abc123def456/check-runs" in args
    assert "--jq" in args
    assert ".total_count" in args


def test_write_json_sends_body_via_stdin() -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
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
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.write_json("PUT", "repos/o/r/actions/permissions", {"allowed_actions": "all"})
    call_args = mock_run.call_args
    expected_cmd = ("gh", "api", "repos/o/r/actions/permissions", "-X", "PUT", "--input", "-")
    assert call_args[0][0] == expected_cmd
    assert json.loads(call_args[1]["input"]) == {"allowed_actions": "all"}


def test_delete_calls_gh_api() -> None:
    with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
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


class TestRunWithRetry:
    def test_succeeds_on_first_attempt(self) -> None:
        with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="ok")
            result = github._run_with_retry(("gh", "pr", "view"), check=True)
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
            result = github._run_with_retry(("gh", "pr", "view"), check=True)
        assert result.stdout == "ok"
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2

    def test_raises_after_max_retries(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
            pytest.raises(github.GitHubAPIError, match="Gateway Timeout"),
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)

    def test_raises_immediately_on_non_retryable_error(self) -> None:
        err = _api_error(stderr="HTTP 404 Not Found")
        with (
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
            patch("vergil_tooling.lib.retry.time.sleep") as mock_sleep,
            pytest.raises(github.GitHubAPIError, match="404 Not Found"),
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)
        mock_sleep.assert_not_called()

    def test_raises_plain_error_when_no_captured_output(self) -> None:
        err = subprocess.CalledProcessError(returncode=1, cmd=["gh"])
        err.stderr = ""
        err.stdout = ""
        with (
            patch("vergil_tooling.lib.retry.subprocess.run", side_effect=err),
            pytest.raises(subprocess.CalledProcessError) as exc_info,
        ):
            github._run_with_retry(("gh", "pr", "view"), check=True)
        assert type(exc_info.value) is subprocess.CalledProcessError

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
            github._run_with_retry(("gh", "pr", "view"), check=True)
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays[0] < delays[1] < delays[2]


class TestRetryIntegration:
    def test_run_retries_on_504(self) -> None:
        err = _api_error(stderr="HTTP 504 Gateway Timeout")
        with (
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            github.run("pr", "list")
        assert mock_run.call_count == 2

    def test_read_output_retries_on_502(self) -> None:
        err = _api_error(stderr="HTTP 502 Bad Gateway")
        with (
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, _completed(stdout="result\n")],
            ) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            assert github.read_output("pr", "view") == "result"
        assert mock_run.call_count == 2

    def test_write_json_retries_on_503(self) -> None:
        err = _api_error(stderr="HTTP 503 Service Unavailable")
        with (
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            github.write_json("PATCH", "repos/o/r", {"key": "val"})
        assert mock_run.call_count == 2

    def test_delete_retries_on_429(self) -> None:
        err = _api_error(stderr="HTTP 429 rate limit exceeded")
        with (
            patch(
                "vergil_tooling.lib.retry.subprocess.run",
                side_effect=[err, _completed()],
            ) as mock_run,
            patch("vergil_tooling.lib.retry.time.sleep"),
            patch("vergil_tooling.lib.retry.random.random", return_value=0.5),
        ):
            github.delete("repos/o/r/vulnerability-alerts")
        assert mock_run.call_count == 2


# --- Credential injection ---


class TestCredentialInjection:
    def test_run_with_retry_injects_env(self) -> None:
        fake_env = {"GH_TOKEN": "test-token", "PATH": "/usr/bin"}
        with (
            patch("vergil_tooling.lib.github._gh_env", return_value=fake_env),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            github._run_with_retry(("gh", "pr", "list"), check=True)
        _, kwargs = mock_run.call_args
        assert kwargs["env"] is fake_env

    def test_run_with_retry_skips_env_when_none(self) -> None:
        with patch("vergil_tooling.lib.retry.subprocess.run") as mock_run:
            mock_run.return_value = _completed()
            github._run_with_retry(("gh", "pr", "list"), check=True)
        _, kwargs = mock_run.call_args
        assert "env" not in kwargs

    def test_run_with_retry_preserves_caller_env(self) -> None:
        caller_env = {"GH_TOKEN": "caller-token"}
        with (
            patch("vergil_tooling.lib.github._gh_env", return_value={"GH_TOKEN": "other"}),
            patch("vergil_tooling.lib.retry.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed()
            github._run_with_retry(("gh", "pr", "list"), check=True, env=caller_env)
        _, kwargs = mock_run.call_args
        assert kwargs["env"] is caller_env


# --- _gh_env ---


class TestGhEnvNew:
    def test_returns_env_with_app_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("vergil_tooling.lib.github._gh_env", _real_gh_env)
        with patch(
            "vergil_tooling.lib.github.get_installation_token",
            return_value="ghs_app_token",
        ):
            env = github._gh_env()
        assert env is not None
        assert env["GH_TOKEN"] == "ghs_app_token"  # noqa: S105

    def test_returns_none_when_no_app_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("vergil_tooling.lib.github._gh_env", _real_gh_env)
        with patch(
            "vergil_tooling.lib.github.get_installation_token",
            return_value=None,
        ):
            assert github._gh_env() is None


# --- App token exchange ---


class TestLoadAppConfig:
    def test_returns_none_when_no_config_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        assert github._load_app_config() is None

    def test_returns_config_from_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        result = github._load_app_config()
        assert result is not None
        app_id, key_path = result
        assert app_id == "12345"
        assert key_path == config_dir / "app.pem"

    def test_returns_none_when_missing_app_id(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("# no app id\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        assert github._load_app_config() is None

    def test_env_vars_override_files(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        key_file = tmp_path / "override.pem"
        key_file.write_text("override-key\n")
        monkeypatch.setenv("VRG_APP_ID", "99999")
        monkeypatch.setenv("VRG_PRIVATE_KEY_PATH", str(key_file))
        result = github._load_app_config()
        assert result is not None
        app_id, key_path = result
        assert app_id == "99999"
        assert key_path == key_file

    def test_ignores_comments_in_env_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("# GitHub App credentials\nAPP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        result = github._load_app_config()
        assert result is not None
        assert result[0] == "12345"


class TestIsAppMode:
    """``is_app_mode`` must detect App credentials minted either by tooling
    (``_load_app_config``) or upstream and passed in as an ambient
    installation token (``ghs_`` prefix).
    """

    def _clear_app_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Point HOME at an empty dir and drop env creds so _load_app_config
        # returns None, isolating the ambient-token detection path.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)

    def test_true_for_ambient_installation_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The CI regression: a ghs_ token from actions/create-github-app-token
        # passed via GH_TOKEN, with no VRG_APP_* config of our own.
        self._clear_app_config(tmp_path, monkeypatch)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "ghs_installation_token_abc")
        assert github.is_app_mode() is True

    def test_true_for_installation_token_in_github_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clear_app_config(tmp_path, monkeypatch)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_installation_token_abc")
        assert github.is_app_mode() is True

    def test_false_for_personal_access_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clear_app_config(tmp_path, monkeypatch)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", "ghp_classic_personal_token")
        assert github.is_app_mode() is False

    def test_false_when_no_token_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clear_app_config(tmp_path, monkeypatch)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert github.is_app_mode() is False

    def test_gh_token_takes_precedence_over_github_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # gh prefers GH_TOKEN; a non-App GH_TOKEN must win even if a stray
        # ghs_ value sits in GITHUB_TOKEN.
        self._clear_app_config(tmp_path, monkeypatch)
        monkeypatch.setenv("GH_TOKEN", "ghp_personal_token")
        monkeypatch.setenv("GITHUB_TOKEN", "ghs_installation_token_abc")
        assert github.is_app_mode() is False

    def test_true_when_tooling_holds_app_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The self-minting path: env-provided App key, regardless of token.
        key_file = tmp_path / "app.pem"
        key_file.write_text("fake-key\n")
        monkeypatch.setenv("VRG_APP_ID", "12345")
        monkeypatch.setenv("VRG_PRIVATE_KEY_PATH", str(key_file))
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert github.is_app_mode() is True


class TestGenerateJwt:
    def test_produces_three_part_token(self, tmp_path: Path) -> None:
        key_path = tmp_path / "test.pem"
        key_path.write_text("fake-key\n")
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"\x00" * 256
            )
            jwt_str = github._generate_jwt("12345", key_path)
        parts = jwt_str.split(".")
        assert len(parts) == 3

    def test_calls_openssl_with_key_path(self, tmp_path: Path) -> None:
        key_path = tmp_path / "test.pem"
        key_path.write_text("fake-key\n")
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"\x00" * 256
            )
            github._generate_jwt("12345", key_path)
        args = mock_run.call_args[0][0]
        assert args[0] == "openssl"
        assert str(key_path) in args

    def test_header_contains_rs256(self, tmp_path: Path) -> None:
        import base64

        key_path = tmp_path / "test.pem"
        key_path.write_text("fake-key\n")
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"\x00" * 256
            )
            jwt_str = github._generate_jwt("12345", key_path)
        header_b64 = jwt_str.split(".")[0]
        padding = 4 - len(header_b64) % 4
        header = json.loads(base64.urlsafe_b64decode(header_b64 + "=" * padding))
        assert header == {"alg": "RS256", "typ": "JWT"}

    def test_payload_contains_app_id_as_iss(self, tmp_path: Path) -> None:
        import base64

        key_path = tmp_path / "test.pem"
        key_path.write_text("fake-key\n")
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=b"\x00" * 256
            )
            jwt_str = github._generate_jwt("12345", key_path)
        payload_b64 = jwt_str.split(".")[1]
        padding = 4 - len(payload_b64) % 4
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * padding))
        assert payload["iss"] == 12345
        assert "iat" in payload
        assert "exp" in payload


class TestDetectOrg:
    def test_parses_ssh_remote(self) -> None:
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = _completed(
                stdout="git@github.com:vergil-project/vergil-tooling.git\n"
            )
            assert github.detect_org() == "vergil-project"

    def test_parses_https_remote(self) -> None:
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = _completed(
                stdout="https://github.com/vergil-project/vergil-tooling.git\n"
            )
            assert github.detect_org() == "vergil-project"

    def test_returns_none_on_git_failure(self) -> None:
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, "git")
            assert github.detect_org() is None

    def test_returns_none_for_unrecognized_url(self) -> None:
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="https://gitlab.com/org/repo.git\n")
            assert github.detect_org() is None

    def test_returns_none_for_empty_org(self) -> None:
        with patch("vergil_tooling.lib.github.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="git@github.com:/repo.git\n")
            assert github.detect_org() is None


_URLOPEN = "vergil_tooling.lib.github.urllib.request.urlopen"


def _mock_http_response(body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestJwtApiRequest:
    def test_sends_bearer_auth_header(self) -> None:
        resp = _mock_http_response(b'[{"id": 1}]')
        with patch(_URLOPEN, return_value=resp) as mock_open:
            github._jwt_api_request("/app/installations", "test-jwt")
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer test-jwt"
        assert req.get_header("Accept") == "application/vnd.github+json"

    def test_get_does_not_send_body(self) -> None:
        resp = _mock_http_response(b"[]")
        with patch(_URLOPEN, return_value=resp) as mock_open:
            github._jwt_api_request("/app/installations", "jwt")
        req = mock_open.call_args[0][0]
        assert req.data is None
        assert req.get_method() == "GET"

    def test_post_sends_empty_body(self) -> None:
        resp = _mock_http_response(b'{"token": "ghs_abc"}')
        with patch(_URLOPEN, return_value=resp) as mock_open:
            github._jwt_api_request(
                "/app/installations/123/access_tokens",
                "jwt",
                method="POST",
            )
        req = mock_open.call_args[0][0]
        assert req.data == b""
        assert req.get_method() == "POST"

    def test_returns_parsed_json(self) -> None:
        resp = _mock_http_response(b'{"token": "ghs_abc"}')
        with patch(_URLOPEN, return_value=resp):
            result = github._jwt_api_request("/endpoint", "jwt")
        assert result == {"token": "ghs_abc"}


class TestResolveInstallations:
    def test_returns_org_to_id_mapping(self) -> None:
        api_response = [
            {"account": {"login": "vergil-project"}, "id": 111},
            {"account": {"login": "wphillipmoore"}, "id": 222},
        ]
        with patch("vergil_tooling.lib.github._jwt_api_request", return_value=api_response):
            result = github._resolve_installations("fake-jwt")
        assert result == {"vergil-project": "111", "wphillipmoore": "222"}

    def test_caches_result(self) -> None:
        api_response = [{"account": {"login": "org1"}, "id": 100}]
        with patch(
            "vergil_tooling.lib.github._jwt_api_request", return_value=api_response
        ) as mock_req:
            first = github._resolve_installations("jwt1")
            second = github._resolve_installations("jwt2")
        assert first is second
        assert mock_req.call_count == 1

    def test_skips_incomplete_entries(self) -> None:
        api_response = [
            {"account": {"login": "good-org"}, "id": 100},
            {"account": {}, "id": 200},
            {"account": {"login": "no-id"}},
        ]
        with patch("vergil_tooling.lib.github._jwt_api_request", return_value=api_response):
            result = github._resolve_installations("jwt")
        assert result == {"good-org": "100"}


class TestGetInstallationToken:
    def test_returns_none_when_no_app_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        assert github.get_installation_token(org="some-org") is None

    def test_returns_none_when_no_org_detected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        with patch("vergil_tooling.lib.github.detect_org", return_value=None):
            assert github.get_installation_token() is None

    def test_exchanges_jwt_for_installation_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="fake-jwt"),
            patch(
                "vergil_tooling.lib.github._resolve_installations",
                return_value={"test-org": "67890"},
            ),
            patch("vergil_tooling.lib.github._jwt_api_request") as mock_req,
        ):
            mock_req.return_value = {"token": "ghs_install_token_abc"}
            token = github.get_installation_token(org="test-org")
        assert token == "ghs_install_token_abc"  # noqa: S105
        mock_req.assert_called_once_with(
            "/app/installations/67890/access_tokens",
            "fake-jwt",
            method="POST",
        )

    def test_returns_none_when_org_not_in_installations(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="jwt"),
            patch(
                "vergil_tooling.lib.github._resolve_installations",
                return_value={"other-org": "111"},
            ),
        ):
            assert github.get_installation_token(org="missing-org") is None

    def test_raises_when_required_and_org_has_no_installation(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="jwt"),
            patch(
                "vergil_tooling.lib.github._resolve_installations",
                return_value={"org-b": "222", "org-a": "111"},
            ),
            pytest.raises(github.NoInstallationError) as excinfo,
        ):
            github.get_installation_token(org="missing-org", require_installation=True)
        assert excinfo.value.org == "missing-org"
        assert excinfo.value.known == ["org-a", "org-b"]

    def test_require_installation_returns_none_without_app_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # No App credentials -> ambient gh auth applies, which is not
        # installation-scoped; requiring an installation must not raise.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        assert github.get_installation_token(org="some-org", require_installation=True) is None

    def test_caches_token_per_org(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="jwt"),
            patch(
                "vergil_tooling.lib.github._resolve_installations",
                return_value={"test-org": "67890"},
            ),
            patch("vergil_tooling.lib.github._jwt_api_request") as mock_req,
        ):
            mock_req.return_value = {"token": "ghs_token"}
            first = github.get_installation_token(org="test-org")
            second = github.get_installation_token(org="test-org")
        assert first == second == "ghs_token"
        assert mock_req.call_count == 1

    def test_returns_none_when_jwt_generation_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        with patch(
            "vergil_tooling.lib.github._generate_jwt",
            side_effect=subprocess.CalledProcessError(1, "openssl"),
        ):
            assert github.get_installation_token(org="test-org") is None

    def test_returns_none_when_resolve_installations_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        err = subprocess.CalledProcessError(1, "gh")
        err.stderr = "HTTP 401 Unauthorized"
        err.stdout = ""
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="fake-jwt"),
            patch(
                "vergil_tooling.lib.github._resolve_installations",
                side_effect=err,
            ),
        ):
            assert github.get_installation_token(org="test-org") is None

    def test_returns_none_when_token_exchange_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        err = urllib.error.HTTPError(
            "https://api.github.com/app/installations/67890/access_tokens",
            401,
            "Unauthorized",
            http.client.HTTPMessage(),
            None,
        )
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="fake-jwt"),
            patch(
                "vergil_tooling.lib.github._resolve_installations",
                return_value={"test-org": "67890"},
            ),
            patch("vergil_tooling.lib.github._jwt_api_request", side_effect=err),
        ):
            assert github.get_installation_token(org="test-org") is None

    def test_returns_none_when_openssl_not_found(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        with patch(
            "vergil_tooling.lib.github._generate_jwt",
            side_effect=FileNotFoundError("openssl"),
        ):
            assert github.get_installation_token(org="test-org") is None

    def test_logs_warning_on_app_auth_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        import logging

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        err = subprocess.CalledProcessError(1, "gh")
        err.stderr = "HTTP 401 Unauthorized"
        err.stdout = ""
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="fake-jwt"),
            patch(
                "vergil_tooling.lib.github._resolve_installations",
                side_effect=err,
            ),
            caplog.at_level(logging.WARNING, logger="vergil_tooling.lib.github"),
        ):
            github.get_installation_token(org="test-org")
        assert "App auth failed" in caplog.text
        assert "401" in caplog.text

    def test_refreshes_expired_cache(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        monkeypatch.delenv("VRG_PRIVATE_KEY_PATH", raising=False)
        config_dir = tmp_path / ".config" / "vergil"
        config_dir.mkdir(parents=True)
        (config_dir / "app.env").write_text("APP_ID=12345\n")
        (config_dir / "app.pem").write_text("fake-key\n")
        github._token_cache["test-org"] = ("old_token", 0.0)
        with (
            patch("vergil_tooling.lib.github._generate_jwt", return_value="jwt"),
            patch(
                "vergil_tooling.lib.github._resolve_installations",
                return_value={"test-org": "67890"},
            ),
            patch("vergil_tooling.lib.github._jwt_api_request") as mock_req,
        ):
            mock_req.return_value = {"token": "new_token"}
            token = github.get_installation_token(org="test-org")
        assert token == "new_token"  # noqa: S105


# -- pr_for_branch / is_draft / head_ref (issue #1423) ------------------------


def test_pr_for_branch_returns_first_open_pr() -> None:
    payload = [
        {
            "number": 1423,
            "url": "https://github.com/o/r/pull/1423",
            "title": "T",
            "headRefOid": "abc123",
        }
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=payload) as rj:
        result = github.pr_for_branch("feature/1423-pr-interface")
    assert result == {
        "number": "1423",
        "url": "https://github.com/o/r/pull/1423",
        "title": "T",
        "headRefOid": "abc123",
    }
    rj.assert_called_once_with(
        "pr",
        "list",
        "--head",
        "feature/1423-pr-interface",
        "--state",
        "open",
        "--json",
        "number,url,title,headRefOid",
    )


def test_pr_for_branch_none_when_no_open_pr() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=[]):
        assert github.pr_for_branch("feature/1423-pr-interface") is None


def test_is_draft_true() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="true"):
        assert github.is_draft("1423") is True


def test_is_draft_false() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="false"):
        assert github.is_draft("1423") is False


def test_head_ref() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="feature/1423-x"):
        assert github.head_ref("1423") == "feature/1423-x"


def test_pr_for_branch_none_when_payload_not_dict() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=["nope"]):
        assert github.pr_for_branch("feature/1423-pr-interface") is None


# -- closed_pr_for_branch (issue #1445) ----------------------------------------


def test_closed_pr_for_branch_returns_first_closed_pr() -> None:
    payload = [
        {
            "number": 1445,
            "url": "https://github.com/o/r/pull/1445",
            "title": "T",
            "headRefOid": "deadbeef",
        }
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=payload) as rj:
        result = github.closed_pr_for_branch("feature/1445-finalize-cleanup-race")
    assert result == {
        "number": "1445",
        "url": "https://github.com/o/r/pull/1445",
        "title": "T",
        "headRefOid": "deadbeef",
    }
    rj.assert_called_once_with(
        "pr",
        "list",
        "--head",
        "feature/1445-finalize-cleanup-race",
        "--state",
        "closed",
        "--json",
        "number,url,title,headRefOid",
    )


def test_closed_pr_for_branch_none_when_no_closed_pr() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=[]):
        assert github.closed_pr_for_branch("feature/1445-finalize-cleanup-race") is None


def test_closed_pr_for_branch_none_when_payload_not_dict() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=["nope"]):
        assert github.closed_pr_for_branch("feature/1445-finalize-cleanup-race") is None
