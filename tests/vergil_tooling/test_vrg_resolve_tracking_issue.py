"""Tests for vergil_tooling.bin.vrg_resolve_tracking_issue."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_resolve_tracking_issue import main, parse_args

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_resolve_tracking_issue"


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.commit == "HEAD"


def test_parse_args_custom_commit() -> None:
    args = parse_args(["--commit", "abc123"])
    assert args.commit == "abc123"


def test_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_json",
            return_value={"body": "## Summary\n\nRef #100\n"},
        ),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"


def test_commit_arg_forwarded() -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ) as mock_git,
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": "Ref #100\n"}),
    ):
        main(["--commit", "abc123"])
    mock_git.assert_called_once_with("log", "-1", "--format=%s", "abc123")


def test_not_a_merge_commit(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.git.read_output", return_value="feat: add widget"):
        result = main([])
    assert result == 1
    assert "not a merge commit" in capsys.readouterr().err


def test_empty_pr_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": ""}),
    ):
        result = main([])
    assert result == 1
    assert "has no body" in capsys.readouterr().err


def test_null_pr_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": None}),
    ):
        result = main([])
    assert result == 1
    assert "has no body" in capsys.readouterr().err


def test_no_ref_in_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_json",
            return_value={"body": "No linkage here.\n"},
        ),
    ):
        result = main([])
    assert result == 1
    assert "no tracking issue linkage" in capsys.readouterr().err


def test_multiple_refs_in_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_json",
            return_value={"body": "Ref #100\nRef #200\n"},
        ),
    ):
        result = main([])
    assert result == 1
    assert "multiple tracking issue references" in capsys.readouterr().err


def test_gh_api_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(returncode=1, cmd=["gh", "api"], stderr="Not Found")
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", side_effect=err),
    ):
        result = main([])
    assert result == 2
    assert "failed to" in capsys.readouterr().err.lower()


def test_integration_fixture_merge_commit(capsys: pytest.CaptureFixture[str]) -> None:
    """Full main() with fixture data matching a real merge commit pattern."""
    commit_subject = "Merge pull request #856 from vergil-project/release/2.0.18"
    pr_body = (
        "## Release 2.0.18\n\n"
        "### Changes\n\n"
        "- fix(repo-config): skip local checks when --repo targets a different repository\n\n"
        "Ref #830\n"
    )
    with (
        patch(f"{_MOD}.git.read_output", return_value=commit_subject),
        patch(
            f"{_MOD}.github.current_repo",
            return_value="vergil-project/vergil-tooling",
        ),
        patch(f"{_MOD}.github.read_json", return_value={"body": pr_body}),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "830"


def test_unexpected_api_response(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value=[]),
    ):
        result = main([])
    assert result == 2
    assert "unexpected API response" in capsys.readouterr().err


def test_git_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(returncode=128, cmd=["git", "log"], stderr="bad revision")
    with patch(f"{_MOD}.git.read_output", side_effect=err):
        result = main([])
    assert result == 2
    assert "failed to" in capsys.readouterr().err.lower()
