"""Tests for vergil_tooling.bin.vrg_submit_pr."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_submit_pr import (
    _push_branch,
    _run_submit_batch,
    _select_batch_worktrees,
    _submit_one,
    _target_branch,
    main,
    parse_args,
)
from vergil_tooling.lib import worktrees
from vergil_tooling.lib.pr_workflow.errors import AlreadySubmittedError, WorkflowError
from vergil_tooling.lib.worktrees import Worktree

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_submit_pr"


def _write_workflow_state(
    root: Path,
    *,
    issue: str = "42",
    branch: str = "feature/x",
    base: str = "develop",
    title: str = "fix: bug",
    summary: str = "Fix the bug",
    notes: str = "Verified locally",
    linkage: str = "Ref",
) -> None:
    """Write a ready-to-submit ``.vergil/pr-workflow.json`` state file.

    This is the sole metadata source vrg-submit-pr reads in template mode
    (issue #1700).
    """
    from vergil_tooling.lib.pr_workflow import engine
    from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

    now = "2026-06-08T00:00:00Z"
    state = engine.init_state(
        issue=issue,
        branch=branch,
        base=base,
        mode="solo",
        head_sha="h0",
        base_sha="b0",
        user_token="u-1",
        now=now,
    )
    engine.apply_report_ready(
        state,
        title=title,
        summary=summary,
        notes=notes,
        linkage=linkage,
        head_sha="h0",
        now=now,
    )
    LocalFileTransport(root, base=base).write(state)


def _state_submitted(root: Path) -> bool:
    """True if the worktree's state file is marked submitted (in-flight)."""
    from vergil_tooling.lib.pr_workflow.state import WorkflowState

    path = root / ".vergil" / "pr-workflow.json"
    return path.is_file() and WorkflowState.from_json(path.read_text()).submitted is not None


@pytest.fixture(autouse=True)
def _in_worktree() -> Iterator[None]:
    """Default every test to running inside a worktree (legacy behavior).

    Root-launch tests override by patching is_main_worktree directly —
    the innermost patch wins.
    """
    with patch(_MOD + ".git.is_main_worktree", return_value=False):
        yield


def test_target_branch_explicit_override_wins() -> None:
    """An explicit --base always takes precedence."""
    assert _target_branch("main", oracle_base="origin/develop") == "main"
    assert _target_branch("some-branch", oracle_base=None) == "some-branch"


def test_target_branch_honors_oracle_base_origin_stripped() -> None:
    """The oracle-recorded base is honored, with any origin/ prefix stripped."""
    assert _target_branch(None, oracle_base="origin/develop") == "develop"
    assert _target_branch(None, oracle_base="develop") == "develop"
    assert _target_branch(None, oracle_base="origin/main") == "main"


def test_target_branch_defaults_to_develop() -> None:
    """With neither override nor oracle base, the default is develop."""
    assert _target_branch(None, oracle_base=None) == "develop"
    assert _target_branch(None) == "develop"


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
    mock_git_run.assert_called_once_with("push", "--force-with-lease", "-u", "origin", "feature/x")
    mock_create_pr.assert_called_once()


def test_push_branch_uses_force_with_lease() -> None:
    """Issue #1557: submission follows a rebase, so the push must tolerate a
    diverged remote via --force-with-lease (never bare --force)."""
    with patch("vergil_tooling.bin.vrg_submit_pr.git.run") as run:
        _push_branch("feature/x")
    run.assert_called_once_with("push", "--force-with-lease", "-u", "origin", "feature/x")
    args = run.call_args[0]
    assert "--force" not in args  # the bare, unsafe variant is never used


def test_push_branch_raises_clear_error_on_rejection() -> None:
    """A refused force-with-lease (remote moved since fetch) must surface a
    clear, actionable message — not a raw CalledProcessError traceback."""
    err = subprocess.CalledProcessError(1, ["git", "push"])
    with (
        patch("vergil_tooling.bin.vrg_submit_pr.git.run", side_effect=err),
        pytest.raises(SystemExit, match="force-with-lease was refused"),
    ):
        _push_branch("feature/x")


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
    """Template mode reads .vergil/pr-workflow.json when no CLI args given."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_already_submitted_worktree_does_not_resubmit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Running submit-pr inside a worktree whose PR is already submitted must
        not push a duplicate PR; it reports the existing PR and exits cleanly."""
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/x",
            ),
            patch(
                _MOD + ".submission.read_pr_fields",
                side_effect=AlreadySubmittedError(
                    pr_url="https://github.com/o/r/pull/312", pr_number=312
                ),
            ),
            patch(_MOD + ".github.create_pr") as create_pr,
        ):
            result = main([])
        assert result == 0
        create_pr.assert_not_called()
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "already submitted" in combined
        assert "312" in combined

    def test_template_dry_run(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _write_workflow_state(tmp_path)
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

    def test_state_file_dry_run_previews_metadata(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Template mode reads pr_metadata from the workflow state file."""
        from vergil_tooling.lib.pr_workflow import engine
        from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

        now = "2026-06-08T00:00:00Z"
        state = engine.init_state(
            issue="1534",
            branch="feature/1534-x",
            base="develop",
            mode="solo",
            head_sha="h0",
            base_sha="b0",
            user_token="u-1",
            now=now,
        )
        engine.apply_report_ready(
            state,
            title="feat: oracle",
            summary="did it",
            notes="n",
            linkage="Ref",
            head_sha="h0",
            now=now,
        )
        LocalFileTransport(tmp_path, base="develop").write(state)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="feature/1534-x",
            ),
        ):
            result = main(["--dry-run"])
        assert result == 0
        out = capsys.readouterr().out
        assert "feat: oracle" in out
        assert "#1534" in out

    def test_oracle_base_overrides_release_branch_inference(self, tmp_path: Path) -> None:
        """Regression (#1609): a release/* branch whose oracle base is develop
        must target develop, not main. The legacy branch-name inference must
        not override the base the oracle recorded."""
        from vergil_tooling.lib.pr_workflow import engine
        from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

        now = "2026-06-08T00:00:00Z"
        state = engine.init_state(
            issue="1606",
            branch="release/post-2.1.27",
            base="origin/develop",
            mode="solo",
            head_sha="h0",
            base_sha="b0",
            user_token="u-1",
            now=now,
        )
        engine.apply_report_ready(
            state,
            title="chore(release): back-merge",
            summary="back-merge",
            notes="n",
            linkage="Ref",
            head_sha="h0",
            now=now,
        )
        LocalFileTransport(tmp_path, base="origin/develop").write(state)
        with (
            patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path),
            patch(
                "vergil_tooling.bin.vrg_submit_pr.git.current_branch",
                return_value="release/post-2.1.27",
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
        assert mock_pr.call_args.kwargs["base"] == "develop"

    def test_template_creates_pr_on_confirm(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path)
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
        # The state file is retained and marked submitted (in-flight tracking),
        # not deleted as the legacy template was.
        assert _state_submitted(tmp_path)

    def test_template_yes_flag_submits_without_prompting(self, tmp_path: Path) -> None:
        """--yes pre-answers the submit confirmation, so the PR is created
        without reading stdin (issue #1644)."""
        _write_workflow_state(tmp_path)
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
            patch("builtins.input", side_effect=AssertionError("stdin read")) as mock_input,
        ):
            result = main(["--yes"])
        assert result == 0
        mock_pr.assert_called_once()
        mock_input.assert_not_called()

    def test_template_prints_pr_watch_oneliner(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
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

    def test_template_rejects_forbidden_linkage(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix", linkage="Closes")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
        ):
            result = main([])
        assert result == 1
        err = capsys.readouterr().err
        assert "linkage" in err.lower()
        assert "Ref" in err

    def test_template_belt_and_suspenders_linkage_check(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Even if the fields carry a bad linkage, template mode rejects it."""
        fields = {"issue": "42", "title": "fix", "summary": "Fix", "linkage": "Closes"}
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".submission.read_pr_fields", return_value=fields),
        ):
            result = main([])
        assert result == 1
        err = capsys.readouterr().err
        assert "linkage" in err.lower()
        assert "Ref" in err

    def test_template_mode_reports_unready_workflow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A state file with no PR metadata yet (the USER agent has not run
        `report-ready`) surfaces the WorkflowError cleanly and exits non-zero,
        rather than crashing."""
        from vergil_tooling.lib.pr_workflow import engine
        from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

        state = engine.init_state(
            issue="42",
            branch="feature/x",
            base="develop",
            mode="solo",
            head_sha="h0",
            base_sha="b0",
            user_token="u-1",
            now="2026-06-08T00:00:00Z",
        )
        LocalFileTransport(tmp_path, base="develop").write(state)
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
        ):
            result = main([])
        assert result == 1
        assert "cannot read PR submission fields" in capsys.readouterr().err

    def test_template_aborts_on_decline(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
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
        # Declining leaves the state file in place and un-submitted.
        assert not _state_submitted(tmp_path)
        assert (tmp_path / ".vergil" / "pr-workflow.json").is_file()

    def test_template_aborts_on_eof(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
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
        assert not _state_submitted(tmp_path)
        assert (tmp_path / ".vergil" / "pr-workflow.json").is_file()

    def test_missing_issue_arg_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--summary", "Fix", "--title", "fix: bug"])
        assert result != 0
        err = capsys.readouterr().err
        assert "--issue" in err

    def test_template_ensures_pushed(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
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

    def test_no_metadata_and_no_args_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_submit_pr.git.repo_root", return_value=tmp_path):
            result = main([])
        assert result != 0
        assert "pr-workflow.json" in capsys.readouterr().err

    def test_partial_cli_args_errors(self, capsys: pytest.CaptureFixture[str]) -> None:
        result = main(["--issue", "42"])
        assert result != 0
        assert "required" in capsys.readouterr().err.lower()

    def test_template_base_override(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
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
            patch(_MOD + ".submission.read_pr_fields", return_value=fields),
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
            patch(_MOD + ".submission.read_pr_fields", side_effect=FileNotFoundError("x")),
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
            patch(_MOD + ".submission.read_pr_fields", return_value=fields),
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

    def test_root_unready_workflow_skipped_with_reason(self) -> None:
        wt = self._wt("issue-7-foo", "feature/7-foo")
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
            patch(
                _MOD + ".submission.read_pr_fields",
                side_effect=WorkflowError("the workflow has no PR metadata yet"),
            ),
            pytest.raises(SystemExit, match="no PR metadata yet"),
        ):
            main(["--dry-run"])

    def test_root_separates_in_flight_from_not_ready(self) -> None:
        submitted = self._wt("issue-141-spike", "feature/141-spike")
        not_ready = self._wt("issue-211-bootstrap", "feature/211-bootstrap")
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(
                _MOD + ".worktrees.list_worktrees",
                return_value=[submitted, not_ready],
            ),
            patch(
                _MOD + ".submission.read_pr_fields",
                side_effect=[
                    AlreadySubmittedError(pr_url="https://github.com/o/r/pull/312", pr_number=312),
                    FileNotFoundError("x"),
                ],
            ),
            pytest.raises(SystemExit) as exc,
        ):
            main(["--dry-run"])
        msg = str(exc.value)
        assert "In flight" in msg
        assert "issue-141-spike" in msg
        assert "PR #312" in msg
        assert "https://github.com/o/r/pull/312" in msg
        assert "Not ready" in msg
        assert "issue-211-bootstrap" in msg

    def test_root_no_worktrees_at_all_errors(self) -> None:
        with (
            patch(_MOD + ".git.is_main_worktree", return_value=True),
            patch(_MOD + ".git.repo_root", return_value="/repo"),
            patch(_MOD + ".worktrees.require_tty"),
            patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
            pytest.raises(SystemExit, match="no .worktrees/ entries exist"),
        ):
            main(["--dry-run"])


# -- --finalize: chain straight into wait-and-merge (issue #1491) -------------


class TestFinalizeFlag:
    """--finalize hands off to vrg-finalize-pr right after PR creation."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_parse_args_finalize_defaults_false(self) -> None:
        args = parse_args(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert args.finalize is False

    def test_parse_args_finalize_flag(self) -> None:
        args = parse_args(
            ["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--finalize"]
        )
        assert args.finalize is True

    def test_cli_mode_chains_into_finalize(self, tmp_path: Path) -> None:
        """After PR creation, vrg-finalize-pr runs with the PR URL from
        the main worktree root."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(
                _MOD + ".github.create_pr",
                return_value="https://github.com/pr/1",
            ),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = main(
                ["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--finalize"]
            )
        assert result == 0
        mock_run.assert_called_once()
        call = mock_run.call_args
        assert call.args[0] == ("vrg-finalize-pr", "https://github.com/pr/1")
        assert call.kwargs["cwd"] == tmp_path

    def test_cli_mode_finalize_replaces_pr_watch(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Merge-on-green was decided at submit time — the pr-watch loop
        does not apply."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(
                _MOD + ".github.create_pr",
                return_value="https://github.com/pr/1",
            ),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = main(
                ["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--finalize"]
            )
        assert result == 0
        assert "pr-watch" not in capsys.readouterr().out

    def test_finalize_failure_reports_created_pr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A finalize failure must not obscure that the PR exists; the
        human re-runs vrg-finalize-pr alone."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(
                _MOD + ".github.create_pr",
                return_value="https://github.com/pr/1",
            ),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            result = main(
                ["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--finalize"]
            )
        assert result != 0
        err = capsys.readouterr().err
        assert "https://github.com/pr/1" in err
        assert "vrg-finalize-pr https://github.com/pr/1" in err

    def test_submit_failure_never_reaches_finalize(self, tmp_path: Path) -> None:
        """A submit failure leaves no half-finalized state — finalize
        only runs after the PR exists."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run", side_effect=RuntimeError("push rejected")),
            patch(_MOD + ".github.create_pr") as mock_pr,
            patch(_MOD + ".subprocess.run") as mock_run,
            pytest.raises(RuntimeError, match="push rejected"),
        ):
            main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--finalize"])
        mock_pr.assert_not_called()
        mock_run.assert_not_called()

    def test_dry_run_notes_finalize_without_running(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            result = main(
                [
                    "--issue",
                    "42",
                    "--summary",
                    "Fix",
                    "--title",
                    "fix: bug",
                    "--finalize",
                    "--dry-run",
                ]
            )
        assert result == 0
        mock_run.assert_not_called()
        assert "vrg-finalize-pr" in capsys.readouterr().out

    def test_template_mode_chains_into_finalize(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(
                _MOD + ".github.create_pr",
                return_value="https://github.com/pr/7",
            ),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
            patch("builtins.input", return_value="y"),
        ):
            mock_run.return_value.returncode = 0
            result = main(["--finalize"])
        assert result == 0
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == ("vrg-finalize-pr", "https://github.com/pr/7")
        assert _state_submitted(tmp_path)

    def test_template_mode_decline_never_reaches_finalize(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".subprocess.run") as mock_run,
            patch("builtins.input", return_value="n"),
        ):
            result = main(["--finalize"])
        assert result == 1
        mock_run.assert_not_called()

    def test_template_dry_run_notes_finalize(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            result = main(["--finalize", "--dry-run"])
        assert result == 0
        mock_run.assert_not_called()
        assert "vrg-finalize-pr" in capsys.readouterr().out

    def test_agent_identity_still_blocked_with_finalize(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--finalize must not weaken the identity-mode gate."""
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch(_MOD + ".subprocess.run") as mock_run:
            result = main(
                ["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--finalize"]
            )
        assert result != 0
        mock_run.assert_not_called()
        assert "human maintainer" in capsys.readouterr().err.lower()


# -- --release: cascade submit -> finalize -> release (issue #1634) ------------


class TestReleaseFlag:
    """--release implies --finalize and passes --release through to the chain."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_parse_args_release_defaults_false(self) -> None:
        args = parse_args(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert args.release is False

    def test_parse_args_release_flag(self) -> None:
        args = parse_args(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--release"])
        assert args.release is True

    def test_cli_mode_chains_into_finalize_with_release(self, tmp_path: Path) -> None:
        """--release implies --finalize and appends --release to the chain."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--release"])
        assert result == 0
        mock_run.assert_called_once()
        call = mock_run.call_args
        assert call.args[0] == ("vrg-finalize-pr", "https://github.com/pr/1", "--release")
        assert call.kwargs["cwd"] == tmp_path

    def test_cli_mode_release_prints_three_way_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """On success the final summary reports the full cascade."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--release"])
        assert result == 0
        out = capsys.readouterr().out
        assert "submitted, finalized, and released" in out
        assert "pr-watch" not in out

    def test_finalize_only_prints_two_way_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--finalize without --release stops the summary at 'finalized'."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = main(
                ["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--finalize"]
            )
        assert result == 0
        out = capsys.readouterr().out
        assert "submitted and finalized" in out
        assert "released" not in out

    def test_release_failure_reports_created_pr_with_release_rerun(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A cascade failure must not obscure the created PR; the re-run hint
        carries --release so the human can resume the whole cascade."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--release"])
        assert result != 0
        err = capsys.readouterr().err
        assert "https://github.com/pr/1" in err
        assert "vrg-finalize-pr https://github.com/pr/1 --release" in err

    def test_dry_run_notes_release_chain_without_running(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            result = main(
                [
                    "--issue",
                    "42",
                    "--summary",
                    "Fix",
                    "--title",
                    "fix: bug",
                    "--release",
                    "--dry-run",
                ]
            )
        assert result == 0
        mock_run.assert_not_called()
        assert "vrg-finalize-pr --release" in capsys.readouterr().out

    def test_template_mode_chains_into_finalize_with_release(self, tmp_path: Path) -> None:
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/7"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
            patch("builtins.input", return_value="y"),
        ):
            mock_run.return_value.returncode = 0
            result = main(["--release"])
        assert result == 0
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0] == (
            "vrg-finalize-pr",
            "https://github.com/pr/7",
            "--release",
        )

    def test_template_mode_chain_failure_propagates_and_skips_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A failed cascade from template mode returns the child's code and
        never prints a success summary — the failure helper reported the PR."""
        _write_workflow_state(tmp_path, title="fix", summary="Fix")
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/7"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
            patch("builtins.input", return_value="y"),
        ):
            mock_run.return_value.returncode = 3
            result = main(["--release"])
        assert result == 3
        out = capsys.readouterr().out
        assert "submitted, finalized, and released" not in out

    def test_agent_identity_still_blocked_with_release(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--release must not weaken the identity-mode gate."""
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch(_MOD + ".subprocess.run") as mock_run:
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--release"])
        assert result != 0
        mock_run.assert_not_called()
        assert "human maintainer" in capsys.readouterr().err.lower()


# -- --install: cascade submit -> finalize -> release -> install (issue #1643) -


class TestInstallFlag:
    """--install implies --release (hence --finalize) and passes --install
    through, so the chain runs vrg-release's consumer-refresh install step."""

    @pytest.fixture(autouse=True)
    def _human_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("VRG_IDENTITY_MODE", raising=False)
        monkeypatch.delenv("VRG_APP_ID", raising=False)

    def test_parse_args_install_defaults_false(self) -> None:
        args = parse_args(["--issue", "42", "--summary", "Fix", "--title", "fix: bug"])
        assert args.install is False

    def test_parse_args_install_flag(self) -> None:
        args = parse_args(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--install"])
        assert args.install is True

    def test_cli_mode_chains_finalize_with_release_and_install(self, tmp_path: Path) -> None:
        """--install implies --release and appends both flags to the chain."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--install"])
        assert result == 0
        mock_run.assert_called_once()
        call = mock_run.call_args
        assert call.args[0] == (
            "vrg-finalize-pr",
            "https://github.com/pr/1",
            "--release",
            "--install",
        )
        assert call.kwargs["cwd"] == tmp_path

    def test_cli_mode_install_prints_four_way_summary(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """On success the final summary reports the full submit→install cascade."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--install"])
        assert result == 0
        out = capsys.readouterr().out
        assert "submitted, finalized, released, and installed" in out

    def test_install_failure_reports_created_pr_with_install_rerun(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A cascade failure must not obscure the created PR; the re-run hint
        carries --release --install so the human can resume the whole cascade."""
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".git.run"),
            patch(_MOD + ".github.create_pr", return_value="https://github.com/pr/1"),
            patch(_MOD + ".git.main_worktree_root", return_value=tmp_path),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 1
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--install"])
        assert result != 0
        err = capsys.readouterr().err
        assert "https://github.com/pr/1" in err
        assert "vrg-finalize-pr https://github.com/pr/1 --release --install" in err

    def test_dry_run_notes_install_chain_without_running(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch(_MOD + ".git.repo_root", return_value=tmp_path),
            patch(_MOD + ".git.current_branch", return_value="feature/x"),
            patch(_MOD + ".subprocess.run") as mock_run,
        ):
            result = main(
                [
                    "--issue",
                    "42",
                    "--summary",
                    "Fix",
                    "--title",
                    "fix: bug",
                    "--install",
                    "--dry-run",
                ]
            )
        assert result == 0
        mock_run.assert_not_called()
        assert "vrg-finalize-pr --release --install" in capsys.readouterr().out

    def test_agent_identity_still_blocked_with_install(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--install must not weaken the identity-mode gate."""
        monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
        with patch(_MOD + ".subprocess.run") as mock_run:
            result = main(["--issue", "42", "--summary", "Fix", "--title", "fix: bug", "--install"])
        assert result != 0
        mock_run.assert_not_called()
        assert "human maintainer" in capsys.readouterr().err.lower()


def test_submit_one_pushes_creates_records_and_returns_url() -> None:
    fields = {
        "issue": "1673",
        "title": "Batch",
        "summary": "s",
        "notes": "",
        "linkage": "Ref",
        "base": "origin/develop",
    }
    with (
        patch(_MOD + ".submission.read_pr_fields", return_value=fields),
        patch(_MOD + ".git.current_branch", return_value="feature/1673-x"),
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + "._create_pr", return_value="https://example/pull/9") as create,
        patch(_MOD + ".submission.record_submission") as record,
        patch(_MOD + ".resolve_issue_ref", return_value="#1673"),
        patch(_MOD + ".build_pr_body", return_value="BODY"),
    ):
        url = _submit_one(
            Path("/repo/.worktrees/issue-1673-x"), base_override=None, assume_yes=True
        )
    assert url == "https://example/pull/9"
    push.assert_called_once_with("feature/1673-x")
    create.assert_called_once()
    record.assert_called_once()


def _batch_wt(name: str) -> Worktree:
    num = name.split("-")[1]
    return Worktree(path=Path(f"/repo/.worktrees/{name}"), branch=f"feature/{num}-x")


def test_submit_batch_rebases_submits_then_finalizes_each_and_releases_once() -> None:
    a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
    finalize_calls: list[tuple[str, ...]] = []

    def fake_run(cmd, **_kwargs):
        finalize_calls.append(tuple(cmd))
        return MagicMock(returncode=0)

    with (
        patch(_MOD + ".worktrees.rebase_onto") as rebase,
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._submit_one", side_effect=["https://x/pull/1", "https://x/pull/2"]),
        patch(_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = _run_submit_batch(
            [a, b], base="develop", finalize=True, release=True, install=False, assume_yes=True
        )
    assert rc == 0
    assert rebase.call_count == 2
    assert ("vrg-finalize-pr", "https://x/pull/1", "--skip-post-checks") in finalize_calls
    assert ("vrg-finalize-pr", "https://x/pull/2", "--skip-post-checks") in finalize_calls
    assert ("vrg-finalize-pr", "--cleanup-only") in finalize_calls
    assert ("vrg-release",) in finalize_calls


def test_submit_batch_rebase_conflict_stops_batch() -> None:
    a, b = _batch_wt("issue-1-a"), _batch_wt("issue-2-b")
    with (
        patch(
            _MOD + ".worktrees.rebase_onto",
            side_effect=subprocess.CalledProcessError(1, "git"),
        ),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._submit_one") as submit,
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = _run_submit_batch(
            [a, b], base="develop", finalize=True, release=False, install=False, assume_yes=True
        )
    assert rc == 1
    submit.assert_not_called()


def test_all_and_select_flags_parse() -> None:
    assert parse_args(["--all"]).all_worktrees is True
    assert parse_args(["--select", "1,2"]).select == "1,2"


def _ready_pair(name: str, num: str) -> tuple[Worktree, dict[str, str]]:
    wt = Worktree(path=Path(f"/r/.worktrees/{name}"), branch=f"feature/{num}-x")
    return (wt, {"issue": num, "title": "T"})


def test_select_batch_all_returns_all_candidates() -> None:
    ready = [_ready_pair("issue-1-a", "1"), _ready_pair("issue-2-b", "2")]
    with patch(_MOD + "._ready_worktrees", return_value=ready):
        out = _select_batch_worktrees(Path("/r"), parse_args(["--all"]))
    assert [w.path.name for w in out] == ["issue-1-a", "issue-2-b"]


def test_select_batch_select_tokens() -> None:
    ready = [_ready_pair("issue-1-a", "1"), _ready_pair("issue-2-b", "2")]
    with patch(_MOD + "._ready_worktrees", return_value=ready):
        out = _select_batch_worktrees(Path("/r"), parse_args(["--select", "2"]))
    assert [w.path.name for w in out] == ["issue-2-b"]


def test_select_batch_bad_token_exits() -> None:
    ready = [_ready_pair("issue-1-a", "1")]
    with (
        patch(_MOD + "._ready_worktrees", return_value=ready),
        pytest.raises(SystemExit, match="--select"),
    ):
        _select_batch_worktrees(Path("/r"), parse_args(["--select", "999"]))


def test_select_batch_interactive_uses_multi_select() -> None:
    ready = [_ready_pair("issue-1-a", "1"), _ready_pair("issue-2-b", "2")]
    with (
        patch(_MOD + "._ready_worktrees", return_value=ready),
        patch(_MOD + ".worktrees.select_worktrees", return_value=[ready[0][0]]) as sw,
    ):
        out = _select_batch_worktrees(Path("/r"), parse_args([]))
    assert out == [ready[0][0]]
    sw.assert_called_once()


def test_submit_one_bad_linkage_exits() -> None:
    fields = {"issue": "1", "title": "T", "summary": "s", "notes": "", "linkage": "Closes"}
    with (
        patch(_MOD + ".submission.read_pr_fields", return_value=fields),
        patch(_MOD + ".resolve_issue_ref", return_value="#1"),
        patch(_MOD + ".git.current_branch", return_value="feature/1-x"),
        pytest.raises(SystemExit, match="bare keyword"),
    ):
        _submit_one(Path("/r"), base_override=None, assume_yes=True)


def test_submit_one_strips_issue_number_from_linkage_and_warns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A linkage carrying the issue number is unambiguous: strip it, warn, proceed."""
    fields = {"issue": "1", "title": "T", "summary": "s", "notes": "", "linkage": "Ref #1761"}
    with (
        patch(_MOD + ".submission.read_pr_fields", return_value=fields),
        patch(_MOD + ".resolve_issue_ref", return_value="#1"),
        patch(_MOD + ".git.current_branch", return_value="feature/1-x"),
        patch(_MOD + ".build_pr_body", return_value="BODY") as build,
        patch(_MOD + ".confirm", return_value=True),
        patch(_MOD + "._push_create_record", return_value="https://example/pull/1"),
    ):
        url = _submit_one(Path("/r"), base_override=None, assume_yes=True)
    assert url == "https://example/pull/1"
    assert build.call_args.kwargs["linkage"] == "Ref"
    assert "Ref #1761" in capsys.readouterr().err


def test_submit_one_declined_exits() -> None:
    fields = {"issue": "1", "title": "T", "summary": "s", "notes": "", "linkage": "Ref"}
    with (
        patch(_MOD + ".submission.read_pr_fields", return_value=fields),
        patch(_MOD + ".resolve_issue_ref", return_value="#1"),
        patch(_MOD + ".git.current_branch", return_value="feature/1-x"),
        patch(_MOD + ".build_pr_body", return_value="BODY"),
        patch(_MOD + ".confirm", return_value=False),
        pytest.raises(SystemExit, match="declined"),
    ):
        _submit_one(Path("/r"), base_override=None, assume_yes=False)


def test_submit_batch_finalize_failure_stops() -> None:
    a = _batch_wt("issue-1-a")
    with (
        patch(_MOD + ".worktrees.rebase_onto"),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._submit_one", return_value="https://x/pull/1"),
        patch(_MOD + ".subprocess.run", return_value=MagicMock(returncode=1)),
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = _run_submit_batch(
            [a], base="develop", finalize=True, release=False, install=False, assume_yes=True
        )
    assert rc == 1


def test_submit_batch_validation_failure_reported() -> None:
    a = _batch_wt("issue-1-a")

    def fake_run(cmd, **_kwargs):
        rc = 1 if tuple(cmd)[:2] == ("vrg-finalize-pr", "--cleanup-only") else 0
        return MagicMock(returncode=rc)

    with (
        patch(_MOD + ".worktrees.rebase_onto"),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._submit_one", return_value="https://x/pull/1"),
        patch(_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = _run_submit_batch(
            [a], base="develop", finalize=True, release=False, install=False, assume_yes=True
        )
    assert rc == 1


def test_submit_batch_release_install_failure_reported() -> None:
    a = _batch_wt("issue-1-a")

    def fake_run(cmd, **_kwargs):
        rc = 1 if tuple(cmd)[0] == "vrg-release" else 0
        return MagicMock(returncode=rc)

    with (
        patch(_MOD + ".worktrees.rebase_onto"),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._submit_one", return_value="https://x/pull/1"),
        patch(_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = _run_submit_batch(
            [a], base="develop", finalize=True, release=True, install=True, assume_yes=True
        )
    assert rc == 1


def test_submit_batch_no_finalize_just_submits() -> None:
    a = _batch_wt("issue-1-a")
    with (
        patch(_MOD + ".worktrees.rebase_onto"),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._submit_one", return_value="https://x/pull/1"),
        patch(_MOD + ".subprocess.run", return_value=MagicMock(returncode=0)) as run,
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = _run_submit_batch(
            [a], base="develop", finalize=False, release=False, install=False, assume_yes=True
        )
    assert rc == 0
    run.assert_not_called()  # no finalize, no post-steps


def test_main_batch_routes_to_run_submit_batch() -> None:
    with (
        patch(_MOD + ".identity_mode.is_agent", return_value=False),
        patch(_MOD + ".git.is_main_worktree", return_value=True),
        patch(_MOD + ".git.repo_root", return_value="/r"),
        patch(_MOD + "._select_batch_worktrees", return_value=["wt"]),
        patch(_MOD + "._run_submit_batch", return_value=0) as run_batch,
    ):
        rc = main(["--all", "--finalize", "--base", "develop"])
    assert rc == 0
    run_batch.assert_called_once()
