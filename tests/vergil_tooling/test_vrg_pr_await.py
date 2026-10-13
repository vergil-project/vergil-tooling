"""Tests for vergil_tooling.bin.vrg_pr_await."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_pr_await import (
    is_permission_error,
    main,
    parse_args,
    pr_owner,
    pr_repo,
)
from vergil_tooling.lib import github
from vergil_tooling.lib.pr_await import PrMergedError, PrState

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_pr_await"
_PR = "https://github.com/pr/1"


def test_parse_args_defaults() -> None:
    args = parse_args([_PR])
    assert args.pr == _PR
    assert args.since_sha is None
    assert args.since_reviews is None


def test_parse_args_with_baselines() -> None:
    args = parse_args([_PR, "--since-sha", "abc", "--since-reviews", "2"])
    assert args.since_sha == "abc"
    assert args.since_reviews == 2


def test_main_prints_settled_state_json(capsys: pytest.CaptureFixture[str]) -> None:
    state = PrState("abc123", [{"name": "build", "bucket": "pass", "state": "SUCCESS"}], [])
    with patch(f"{_MOD}.pr_await.wait_for_settle", return_value=(state, "checks_terminal")):
        result = main([_PR])
    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "checks_terminal"
    assert payload["head_sha"] == "abc123"
    assert payload["all_checks_passed"] is True


def test_main_passes_baselines_through() -> None:
    state = PrState("sha", [], [])
    with patch(f"{_MOD}.pr_await.wait_for_settle", return_value=(state, "new_commit")) as waiter:
        main([_PR, "--since-sha", "old", "--since-reviews", "3"])
    _, kwargs = waiter.call_args
    assert kwargs["since_sha"] == "old"
    assert kwargs["since_reviews"] == 3


def test_main_errors_when_pr_already_merged(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.pr_await.wait_for_settle", side_effect=PrMergedError(_PR)):
        result = main([_PR])
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("error:")
    assert "already merged" in captured.err


def test_pr_owner_from_url() -> None:
    assert pr_owner("https://github.com/some-org/some-repo/pull/215") == "some-org"


def test_pr_owner_none_for_bare_number() -> None:
    assert pr_owner("215") is None


def _settle_minting_token(*_args: object, **_kwargs: object) -> tuple[PrState, str]:
    """Stand-in settle that exercises the real token-injection path once."""
    github._gh_env()  # noqa: SLF001
    return PrState("sha", [], []), "checks_terminal"


def test_main_mints_token_for_pr_url_owner_not_cwd() -> None:
    """A PR URL scopes App-token minting to its owner, not the cwd org (#3055)."""
    with (
        patch(f"{_MOD}.pr_await.wait_for_settle", side_effect=_settle_minting_token),
        patch("vergil_tooling.lib.github.get_installation_token", return_value="t") as mint,
    ):
        assert main(["https://github.com/other-org/repo/pull/7"]) == 0
    mint.assert_called_once_with(org="other-org", require_installation=True)


def test_main_bare_number_keeps_cwd_resolution() -> None:
    with (
        patch(f"{_MOD}.pr_await.wait_for_settle", side_effect=_settle_minting_token),
        patch("vergil_tooling.lib.github.get_installation_token", return_value="t") as mint,
    ):
        assert main(["7"]) == 0
    mint.assert_called_once_with(org=None, require_installation=False)


def test_main_errors_when_owner_has_no_installation(capsys: pytest.CaptureFixture[str]) -> None:
    exc = github.NoInstallationError("other-org", ["vergil-project"])
    with patch(f"{_MOD}.pr_await.wait_for_settle", side_effect=exc):
        result = main(["https://github.com/other-org/repo/pull/7"])
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("vrg-pr-await: ")
    assert "other-org" in captured.err
    assert "vergil-project" in captured.err


_CHECKS_CMD = ("gh", "pr", "checks", "7", "--json", "name,bucket,state,link")
_GRAPHQL_403 = (
    "GraphQL: Resource not accessible by integration "
    "(node.statusCheckRollup.nodes.0.commit.statusCheckRollup)"
)
_REST_403 = "failed to get runs: HTTP 403: Resource not accessible by integration"


def test_pr_repo_from_url() -> None:
    assert pr_repo("https://github.com/some-org/some-repo/pull/215") == "some-org/some-repo"


def test_pr_repo_none_for_bare_number() -> None:
    assert pr_repo("215") is None


def test_is_permission_error_graphql_message() -> None:
    assert is_permission_error(github.GitHubAPIError(1, _CHECKS_CMD, stderr=_GRAPHQL_403))


def test_is_permission_error_http_403_in_stdout() -> None:
    assert is_permission_error(github.GitHubAPIError(1, _CHECKS_CMD, output="HTTP 403: Forbidden"))


def test_is_permission_error_false_for_other_failures() -> None:
    assert not is_permission_error(github.GitHubAPIError(1, _CHECKS_CMD, stderr="HTTP 502"))


def test_main_names_permission_problem_on_403(capsys: pytest.CaptureFixture[str]) -> None:
    """A 403 on the checks read is reported as an App permission problem (#2862)."""
    exc = github.GitHubAPIError(1, _CHECKS_CMD, stderr=_GRAPHQL_403)
    with patch(f"{_MOD}.pr_await.wait_for_settle", side_effect=exc):
        result = main(["https://github.com/other-org/repo/pull/7"])
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    expected = "vrg-pr-await: GitHub App permissions problem for other-org/repo"
    assert captured.err.startswith(expected)
    assert "checks: read" in captured.err
    assert "actions: read" in captured.err
    # The underlying gh error is never swallowed.
    assert "Resource not accessible by integration" in captured.err
    assert "statusCheckRollup" in captured.err
    assert "Traceback" not in captured.err


def test_main_permission_problem_bare_number_names_current_repo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exc = github.GitHubAPIError(1, _CHECKS_CMD, stderr=_REST_403)
    with patch(f"{_MOD}.pr_await.wait_for_settle", side_effect=exc):
        result = main(["7"])
    assert result == 1
    err = capsys.readouterr().err
    assert "permissions problem for the current repository" in err
    assert "HTTP 403" in err


def test_main_reports_other_api_errors_plainly(capsys: pytest.CaptureFixture[str]) -> None:
    exc = github.GitHubAPIError(1, _CHECKS_CMD, stderr="HTTP 502: Bad Gateway")
    with patch(f"{_MOD}.pr_await.wait_for_settle", side_effect=exc):
        result = main([_PR])
    assert result == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("vrg-pr-await: GitHub API error: ")
    assert "HTTP 502: Bad Gateway" in captured.err
    assert "permissions" not in captured.err


def test_main_reports_missing_token(capsys: pytest.CaptureFixture[str]) -> None:
    stderr = "gh: In a GitHub Actions workflow, set the GH_TOKEN environment variable."
    exc = github.MissingGitHubTokenError(4, _CHECKS_CMD, stderr=stderr)
    with patch(f"{_MOD}.pr_await.wait_for_settle", side_effect=exc):
        result = main([_PR])
    assert result == 1
    err = capsys.readouterr().err
    assert err.startswith("vrg-pr-await: no GitHub token available")
