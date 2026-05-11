"""Tests for standard_tooling.bin.st_submit_pr."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from standard_tooling.bin.st_submit_pr import (
    _resolve_issue_ref,
    main,
    parse_args,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_resolve_plain_number() -> None:
    assert _resolve_issue_ref("42") == "#42"


def test_resolve_cross_repo() -> None:
    assert _resolve_issue_ref("owner/repo#42") == "owner/repo#42"


def test_resolve_invalid() -> None:
    with pytest.raises(SystemExit, match="must be a number"):
        _resolve_issue_ref("bad-ref")


def test_resolve_zero() -> None:
    with pytest.raises(SystemExit, match="must be a number"):
        _resolve_issue_ref("0")


def test_parse_args_required() -> None:
    args = parse_args(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert args.issue == "42"
    assert args.summary == "Fix bug"
    assert args.title == "fix: bug"
    assert args.linkage == "Ref"
    assert args.dry_run is False


def test_parse_args_title_is_required() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--issue", "42", "--summary", "Fix bug"])


def test_parse_args_all_options() -> None:
    args = parse_args(
        [
            "--issue",
            "owner/repo#10",
            "--summary",
            "Add feature",
            "--linkage",
            "Ref",
            "--notes",
            "Tested",
            "--title",
            "My PR",
            "--dry-run",
        ]
    )
    assert args.linkage == "Ref"
    assert args.dry_run is True


def test_dry_run_body_has_no_testing_section(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "pull_request_template.md").write_text(
        "> **Do not create PRs manually.**\n"
        "> Use `st-submit-pr`.\n"
    )
    with (
        patch("standard_tooling.bin.st_submit_pr.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--dry-run"])
    assert result == 0
    output = capsys.readouterr().out
    assert "## Summary" in output
    assert "## Issue Linkage" in output
    assert "## Notes" in output
    assert "## Testing" not in output


def test_main_dry_run(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.st_submit_pr.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--dry-run"])
    assert result == 0


def test_main_dry_run_with_title(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.st_submit_pr.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(
            [
                "--issue",
                "42",
                "--summary",
                "Fix bug",
                "--title",
                "Custom Title",
                "--dry-run",
            ]
        )
    assert result == 0


def test_main_dry_run_release_branch(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.st_submit_pr.git.repo_root", return_value=tmp_path),
        patch(
            "standard_tooling.bin.st_submit_pr.git.current_branch",
            return_value="release/1.0.0",
        ),
    ):
        result = main(
            [
                "--issue",
                "42",
                "--summary",
                "Release 1.0.0",
                "--title",
                "release: 1.0.0",
                "--dry-run",
            ]
        )
    assert result == 0


def test_main_submits_pr(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.st_submit_pr.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_submit_pr.git.current_branch", return_value="feature/x"),
        patch("standard_tooling.bin.st_submit_pr.git.run") as mock_git_run,
        patch(
            "standard_tooling.bin.st_submit_pr.github.create_pr",
            return_value="https://github.com/pr/1",
        ) as mock_create_pr,
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert result == 0
    mock_git_run.assert_called_once_with("push", "-u", "origin", "feature/x")
    mock_create_pr.assert_called_once()


def test_main_submits_pr_with_notes(tmp_path: Path) -> None:
    with (
        patch("standard_tooling.bin.st_submit_pr.git.repo_root", return_value=tmp_path),
        patch("standard_tooling.bin.st_submit_pr.git.current_branch", return_value="feature/x"),
        patch("standard_tooling.bin.st_submit_pr.git.run"),
        patch(
            "standard_tooling.bin.st_submit_pr.github.create_pr",
            return_value="https://github.com/pr/1",
        ),
    ):
        result = main(
            [
                "--issue",
                "42",
                "--summary",
                "Fix bug",
                "--title",
                "fix: bug",
                "--notes",
                "Tested on macOS",
            ]
        )
    assert result == 0
