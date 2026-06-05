"""Tests for vergil_tooling.bin.vrg_submit_pr."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_submit_pr import (
    _resolve_issue_ref,
    main,
    parse_args,
)
from vergil_tooling.lib import worktrees

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_submit_pr"


@pytest.fixture(autouse=True)
def _in_worktree() -> Iterator[None]:
    """Default every test to running inside a worktree (legacy behavior).

    Root-launch tests override by patching is_main_worktree directly —
    the innermost patch wins.
    """
    with patch(_MOD + ".git.is_main_worktree", return_value=False):
        yield


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


# -- root-launch worktree resolution (issue #1423) ----------------------------


class TestRootLaunch:
    """From the repo root, submit-pr resolves the target worktree itself."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    @staticmethod
    def _wt(name: str, branch: str) -> worktrees.Worktree:
        return worktrees.Worktree(path=Path(f"/repo/.worktrees/{name}"), branch=branch)

    def test_root_single_ready_worktree_chdirs_and_submits(self) -> None:
        wt = self._wt("issue-7-foo", "feature/7-foo")
        fields = {"issue": "7", "title": "Foo title", "summary": "S"}
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),  # pytest stdin is not a TTY
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".pr_template.read_template", return_value=fields),
            patch(_MOD + ".os.chdir") as chdir,
            patch(_MOD + ".git.current_branch", return_value="feature/7-foo"),
        ):
            rc = main(["--dry-run"])
        assert rc == 0
        chdir.assert_called_once_with(wt.path)

    def test_root_no_ready_worktrees_errors_with_reasons(self) -> None:
        wt = self._wt("issue-7-foo", "feature/7-foo")
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),  # pytest stdin is not a TTY
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(_MOD + ".pr_template.read_template", side_effect=FileNotFoundError("x")),
            pytest.raises(SystemExit, match="no submittable worktrees"),
        ):
            main(["--dry-run"])

    def test_root_non_tty_fails_fast(self) -> None:
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(
                _MOD + ".worktrees.require_tty",
                side_effect=SystemExit("requires an interactive terminal"),
            ),
            patch(_MOD + ".worktrees.list_worktrees") as listing,
            pytest.raises(SystemExit, match="interactive terminal"),
        ):
            main([])
        listing.assert_not_called()

    def test_root_multiple_ready_worktrees_prompts(self) -> None:
        wts = [self._wt("issue-7-foo", "feature/7-foo"), self._wt("issue-8-bar", "feature/8-bar")]
        fields = {"issue": "8", "title": "Bar title", "summary": "S"}
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=wts),
            patch(_MOD + ".pr_template.read_template", return_value=fields),
            patch(
                "vergil_tooling.lib.worktrees.prompt_choice",
                return_value="issue-8-bar — issue 8: Bar title",
            ),
            patch(_MOD + ".os.chdir") as chdir,
            patch(_MOD + ".git.current_branch", return_value="feature/8-bar"),
        ):
            rc = main(["--dry-run"])
        assert rc == 0
        chdir.assert_called_once_with(wts[1].path)
