"""Tests for vergil_tooling.bin.vrg_pr_await: every exit path of the merge contract."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_pr_await import (
    EXIT_CANNOT_MERGE,
    EXIT_ERROR,
    EXIT_MERGED,
    is_permission_error,
    main,
    parse_args,
    pr_owner,
    pr_repo,
)
from vergil_tooling.lib import finalize_watch, github
from vergil_tooling.lib.pr_await import AwaitResult, PrAwaitError

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_await import Reason

_MOD = "vergil_tooling.bin.vrg_pr_await"
_WAIT = f"{_MOD}.pr_await.wait_for_merge"
_PR = "https://github.com/o/r/pull/1"


def _merged() -> AwaitResult:
    return AwaitResult(
        pr=_PR,
        reason=None,
        detail="",
        head_sha="abc",
        merge_state_status="UNKNOWN",
        merge_commit="def",
    )


def _cannot(reason: Reason, detail: str, **kwargs: Any) -> AwaitResult:
    return AwaitResult(
        pr=_PR, reason=reason, detail=detail, head_sha="abc", merge_state_status="BLOCKED", **kwargs
    )


def test_exit_codes_are_distinct() -> None:
    assert (EXIT_MERGED, EXIT_CANNOT_MERGE, EXIT_ERROR) == (0, 1, 2)


def test_parse_args_takes_the_pr() -> None:
    assert parse_args([_PR]).pr == _PR


@pytest.mark.parametrize("flag", ["--since-sha", "--since-reviews"])
def test_removed_flags_are_fatal_and_name_the_new_behaviour(
    flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main([_PR, flag, "x"])
    assert exc.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert f"{flag} was removed" in err
    assert "waits for the PR to merge" in err


def test_missing_pr_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == EXIT_ERROR


def test_merged_exits_0_with_json(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_WAIT, return_value=_merged()):
        assert main([_PR]) == EXIT_MERGED
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["outcome"] == "merged"
    assert payload["reason"] is None
    assert payload["merge_commit"] == "def"
    assert captured.err == ""


@pytest.mark.parametrize(
    ("reason", "kwargs", "field", "value"),
    [
        ("check_failed", {"failed_checks": ("test / unit",)}, "failed_checks", ["test / unit"]),
        ("conflict", {}, None, None),
        ("auto_merge_off", {}, None, None),
        ("closed", {}, None, None),
        ("orphaned_check", {"orphaned_checks": ("CodeQL",)}, "orphaned_checks", ["CodeQL"]),
        ("timed_out", {}, None, None),
    ],
)
def test_cannot_merge_exits_1_naming_the_reason(
    reason: Reason,
    kwargs: dict[str, Any],
    field: str | None,
    value: list[str] | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(_WAIT, return_value=_cannot(reason, f"why {reason}", **kwargs)):
        assert main([_PR]) == EXIT_CANNOT_MERGE
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["outcome"] == "cannot_merge"
    assert payload["reason"] == reason
    assert payload["merge_commit"] is None
    if field is not None:
        assert payload[field] == value
    assert f"cannot merge ({reason}): why {reason}" in captured.err


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("VRG_FINALIZE_DEADLINE must be a positive whole number of seconds, got '0'"),
        finalize_watch.FinalizeWatchError("https://github.com/o/r/pull/1: reading the PR failed"),
        PrAwaitError("https://github.com/o/r/pull/1: merged but no merge commit"),
    ],
)
def test_errors_exit_2_with_no_json(exc: Exception, capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_WAIT, side_effect=exc):
        assert main([_PR]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"vrg-pr-await: {exc}\n"


def test_unexpected_error_exits_2_with_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(_WAIT, side_effect=KeyError("boom")):
        assert main([_PR]) == EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "vrg-pr-await: unexpected error:" in captured.err
    assert "Traceback" in captured.err
    assert "KeyError: 'boom'" in captured.err


def test_pr_owner_from_url() -> None:
    assert pr_owner("https://github.com/some-org/some-repo/pull/215") == "some-org"


def test_pr_owner_none_for_bare_number() -> None:
    assert pr_owner("215") is None


def _wait_minting_token(*_args: object, **_kwargs: object) -> AwaitResult:
    """Stand-in wait that exercises the real token-injection path once."""
    github._gh_env()  # noqa: SLF001
    return _merged()


def test_main_mints_token_for_pr_url_owner_not_cwd() -> None:
    """A PR URL scopes App-token minting to its owner, not the cwd org (#3055)."""
    with (
        patch(_WAIT, side_effect=_wait_minting_token),
        patch("vergil_tooling.lib.github.get_installation_token", return_value="t") as mint,
    ):
        assert main(["https://github.com/other-org/repo/pull/7"]) == EXIT_MERGED
    mint.assert_called_once_with(org="other-org", require_installation=True)


def test_main_bare_number_keeps_cwd_resolution() -> None:
    with (
        patch(_WAIT, side_effect=_wait_minting_token),
        patch("vergil_tooling.lib.github.get_installation_token", return_value="t") as mint,
    ):
        assert main(["7"]) == EXIT_MERGED
    mint.assert_called_once_with(org=None, require_installation=False)


def test_main_errors_when_owner_has_no_installation(capsys: pytest.CaptureFixture[str]) -> None:
    exc = github.NoInstallationError("other-org", ["vergil-project"])
    with patch(_WAIT, side_effect=exc):
        result = main(["https://github.com/other-org/repo/pull/7"])
    assert result == EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("vrg-pr-await: ")
    assert "other-org" in captured.err
    assert "vergil-project" in captured.err


def test_main_app_auth_error_exits_2(capsys: pytest.CaptureFixture[str], tmp_path: Any) -> None:
    exc = github.AppAuthError("o", "123", tmp_path / "key.pem", "bad key")
    with patch(_WAIT, side_effect=exc):
        assert main([_PR]) == EXIT_ERROR
    assert capsys.readouterr().err.startswith("vrg-pr-await: ")


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
    with patch(_WAIT, side_effect=exc):
        result = main(["https://github.com/other-org/repo/pull/7"])
    assert result == EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    expected = "vrg-pr-await: GitHub App permissions problem for other-org/repo"
    assert captured.err.startswith(expected)
    assert "checks: read" in captured.err
    assert "statuses: read" in captured.err
    assert "actions: read" in captured.err
    # The underlying gh error is never swallowed.
    assert "Resource not accessible by integration" in captured.err
    assert "statusCheckRollup" in captured.err
    assert "Traceback" not in captured.err


def test_main_permission_problem_bare_number_names_current_repo(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exc = github.GitHubAPIError(1, _CHECKS_CMD, stderr=_REST_403)
    with patch(_WAIT, side_effect=exc):
        result = main(["7"])
    assert result == EXIT_ERROR
    err = capsys.readouterr().err
    assert "permissions problem for the current repository" in err
    assert "HTTP 403" in err


def test_main_reports_other_api_errors_plainly(capsys: pytest.CaptureFixture[str]) -> None:
    exc = github.GitHubAPIError(1, _CHECKS_CMD, stderr="HTTP 502: Bad Gateway")
    with patch(_WAIT, side_effect=exc):
        result = main([_PR])
    assert result == EXIT_ERROR
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("vrg-pr-await: GitHub API error: ")
    assert "HTTP 502: Bad Gateway" in captured.err
    assert "permissions" not in captured.err


def test_main_reports_missing_token(capsys: pytest.CaptureFixture[str]) -> None:
    stderr = "gh: In a GitHub Actions workflow, set the GH_TOKEN environment variable."
    exc = github.MissingGitHubTokenError(4, _CHECKS_CMD, stderr=stderr)
    with patch(_WAIT, side_effect=exc):
        result = main([_PR])
    assert result == EXIT_ERROR
    err = capsys.readouterr().err
    assert err.startswith("vrg-pr-await: no GitHub token available")
