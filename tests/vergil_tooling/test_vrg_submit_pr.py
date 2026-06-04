"""Tests for vergil_tooling.bin.vrg_submit_pr."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_submit_pr import (
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


def test_parse_args_cli_fields() -> None:
    args = parse_args(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert args.issue == "42"
    assert args.summary == "Fix bug"
    assert args.title == "fix: bug"
    assert args.linkage == "Ref"
    assert args.dry_run is False


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


def test_parse_args_base_flag() -> None:
    args = parse_args(
        ["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--base", "develop"]
    )
    assert args.base == "develop"


def test_parse_args_base_defaults_to_none() -> None:
    args = parse_args(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert args.base is None


def test_base_flag_overrides_auto_detected_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A release/ branch normally targets main, but --base should override."""
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
            return_value="release/post-2.1.3",
        ),
    ):
        result = main(
            [
                "--issue",
                "42",
                "--summary",
                "Back-merge",
                "--title",
                "chore: back-merge",
                "--base",
                "develop",
                "--dry-run",
            ]
        )
    assert result == 0
    output = capsys.readouterr().out
    assert "develop" in output
    assert "main" not in output


def test_base_flag_used_in_pr_creation(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
        patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
            return_value="https://github.com/pr/1",
        ) as mock_create_pr,
    ):
        main(
            [
                "--issue",
                "42",
                "--summary",
                "Fix bug",
                "--title",
                "fix: bug",
                "--base",
                "main",
            ]
        )
    mock_create_pr.assert_called_once()
    call_kwargs = mock_create_pr.call_args
    assert call_kwargs.kwargs["base"] == "main"


def test_dry_run_body_has_no_testing_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gh = tmp_path / ".github"
    gh.mkdir()
    (gh / "pull_request_template.md").write_text(
        "> **Do not create PRs manually.**\n> Use `vrg-submit-pr`.\n"
    )
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
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
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--dry-run"])
    assert result == 0


def test_main_dry_run_with_title(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
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
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
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
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
        patch("vergil_tooling.bin.vrg_submit_pr.git.run") as mock_git_run,
        patch(
            "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
            return_value="https://github.com/pr/1",
        ) as mock_create_pr,
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert result == 0
    mock_git_run.assert_called_once_with("push", "-u", "origin", "feature/x")
    mock_create_pr.assert_called_once()


def test_main_submits_pr_with_notes(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
        patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
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


def test_main_prints_pr_watch_oneliner_on_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
        patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
        patch(
            "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
            return_value="https://github.com/pr/1",
        ),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug"])
    assert result == 0
    assert "/vergil:pr-watch https://github.com/pr/1" in capsys.readouterr().out


def test_main_dry_run_omits_pr_watch_oneliner(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_submit_pr.git.current_branch", return_value="feature/x"),
    ):
        result = main(["--issue", "42", "--summary", "Fix bug", "--title", "fix: bug", "--dry-run"])
    assert result == 0
    assert "pr-watch" not in capsys.readouterr().out


class TestIdentityGate:
    """Agent identities are blocked from PR submission."""

    def test_user_mode_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert result != 0
        assert "human maintainer" in capsys.readouterr().err.lower()

    def test_audit_mode_blocked(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("VRG_IDENTITY_MODE", "audit")
        result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert result != 0

    def test_human_mode_allowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
        ):
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--dry-run"])
        assert result == 0


class TestTemplateMode:
    """Template mode reads .vergil/pr-template.yml when no CLI args given."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_template_dry_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\ntitle: 'fix: bug'\nsummary: Fix the bug\nlinkage: Ref\n"
        )
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
        ):
            result = main(["--dry-run"])
        assert result == 0
        out = capsys.readouterr().out
        assert "fix: bug" in out
        assert "#42" in out

    def test_template_creates_pr_on_confirm(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text(
            "issue: 42\ntitle: 'fix: bug'\nsummary: Fix the bug\n"
        )
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/1",
            ) as mock_pr,
            patch("builtins.input", return_value="y"),
        ):
            result = main([])
        assert result == 0
        mock_pr.assert_called_once()
        assert not (vergil / "pr-template.yml").exists()

    def test_template_prints_pr_watch_oneliner(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text("issue: 42\ntitle: fix\nsummary: Fix\n")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run"),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/7",
            ),
            patch("builtins.input", return_value="y"),
        ):
            result = main([])
        assert result == 0
        assert "/vergil:pr-watch https://github.com/pr/7" in capsys.readouterr().out

    def test_template_aborts_on_decline(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text("issue: 42\ntitle: fix\nsummary: Fix\n")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("builtins.input", return_value="n"),
        ):
            result = main([])
        assert result == 1
        assert (vergil / "pr-template.yml").exists()

    def test_template_aborts_on_eof(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text("issue: 42\ntitle: fix\nsummary: Fix\n")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("builtins.input", side_effect=EOFError),
        ):
            result = main([])
        assert result == 1
        assert (vergil / "pr-template.yml").exists()

    def test_missing_issue_arg_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--summary", "Fix", "--title", "fix: bug"])
        assert result != 0
        err = capsys.readouterr().err
        assert "--issue" in err

    def test_template_ensures_pushed(self, tmp_path: Path) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text("issue: 42\ntitle: fix\nsummary: Fix\n")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch("vergil_tooling.bin.vrg_submit_pr.git.run") as mock_git_run,
            patch(
                "vergil_tooling.bin.vrg_submit_pr.github.create_pr",
                return_value="https://github.com/pr/1",
            ) as mock_pr,
            patch("builtins.input", return_value="y"),
        ):
            main([])
        push_calls = [c for c in mock_git_run.call_args_list if c.args and c.args[0] == "push"]
        assert push_calls, "expected vrg-submit-pr to push the branch"
        mock_pr.assert_called_once()

    def test_no_template_and_no_args_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path):
            result = main([])
        assert result != 0
        assert "pr-template.yml" in capsys.readouterr().err

    def test_partial_cli_args_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--issue", "42"])
        assert result != 0
        assert "required" in capsys.readouterr().err.lower()

    def test_template_base_override(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        vergil = tmp_path / ".vergil"
        vergil.mkdir()
        (vergil / "pr-template.yml").write_text("issue: 42\ntitle: fix\nsummary: Fix\n")
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
        ):
            result = main(["--base", "main", "--dry-run"])
        assert result == 0
        out = capsys.readouterr().out
        assert "main" in out
