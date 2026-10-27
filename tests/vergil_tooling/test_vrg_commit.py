"""Tests for vergil_tooling.bin.vrg_commit."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.vergil_tooling.conftest import local_settings_violation
from vergil_tooling.bin.vrg_commit import _validate_commit_context, main, parse_args
from vergil_tooling.lib.local_settings import AGENT_LINE, REFUSAL_TEMPLATE, WARNING_TEMPLATE

if TYPE_CHECKING:
    from collections.abc import Generator


_TEST_TOML_TEMPLATE = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "{branching_model}"
release-model = "tagged-release"
primary-language = "python"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


@contextlib.contextmanager
def _commit_environment(
    tmp_path: Path,
    *,
    branch: str = "feature/42-test",
    is_main_worktree: bool = False,
    branching_model: str = "library-release",
    has_staged: bool = True,
    write_config: bool = True,
) -> Generator[None]:
    if write_config:
        (tmp_path / "vergil.toml").write_text(
            _TEST_TOML_TEMPLATE.format(branching_model=branching_model)
        )

    with (
        patch("vergil_tooling.bin.vrg_commit.git.current_branch", return_value=branch),
        patch("vergil_tooling.bin.vrg_commit.git.repo_root", return_value=tmp_path),
        patch(
            "vergil_tooling.bin.vrg_commit.git.is_main_worktree",
            return_value=is_main_worktree,
        ),
        patch(
            "vergil_tooling.bin.vrg_commit.git.has_staged_changes",
            return_value=has_staged,
        ),
        patch("vergil_tooling.bin.vrg_commit.git.run"),
    ):
        yield


def test_parse_args_required() -> None:
    args = parse_args(["--type", "feat", "--scope", "core", "--message", "add thing"])
    assert args.commit_type == "feat"
    assert args.scope == "core"
    assert args.message == "add thing"
    assert args.body == ""
    assert args.allow_empty is False


def test_parse_args_allow_empty() -> None:
    args = parse_args(
        ["--type", "chore", "--scope", "ci", "--message", "trigger rebuild", "--allow-empty"]
    )
    assert args.allow_empty is True


def test_parse_args_with_scope_and_body() -> None:
    args = parse_args(
        [
            "--type",
            "fix",
            "--scope",
            "lint",
            "--message",
            "correct regex",
            "--body",
            "Fixed edge case",
        ]
    )
    assert args.commit_type == "fix"
    assert args.scope == "lint"
    assert args.body == "Fixed edge case"


def test_parse_args_revert_type() -> None:
    args = parse_args(["--type", "revert", "--scope", "auth", "--message", "undo token change"])
    assert args.commit_type == "revert"


def test_parse_args_invalid_type() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--type", "invalid", "--scope", "core", "--message", "x"])


def test_main_no_staged_changes(tmp_path: Path) -> None:
    with _commit_environment(tmp_path, has_staged=False):
        result = main(["--type", "feat", "--scope", "core", "--message", "test"])
    assert result == 1


def test_main_allow_empty_skips_staged_check(tmp_path: Path) -> None:
    git_args: list[tuple[str, ...]] = []

    def capture_run(*args: str) -> None:
        git_args.append(args)

    with (
        _commit_environment(tmp_path, has_staged=False),
        patch("vergil_tooling.bin.vrg_commit.git.run", side_effect=capture_run),
    ):
        result = main(
            ["--type", "chore", "--scope", "ci", "--message", "trigger rebuild", "--allow-empty"]
        )
    assert result == 0
    commit_call = git_args[0]
    assert "--allow-empty" in commit_call


def test_main_with_staged_changes(tmp_path: Path) -> None:
    commit_file_content = ""

    def capture_run(*args: str) -> None:
        nonlocal commit_file_content
        if args[0] == "commit" and args[1] == "--file":
            commit_file_content = Path(args[2]).read_text(encoding="utf-8")

    with (
        _commit_environment(tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.run", side_effect=capture_run),
    ):
        result = main(["--type", "feat", "--scope", "core", "--message", "add feature"])
    assert result == 0
    assert commit_file_content.startswith("feat(core): add feature\n")
    assert "Co-Authored-By" not in commit_file_content


def test_main_with_scope_and_body(tmp_path: Path) -> None:
    commit_file_content = ""

    def capture_run(*args: str) -> None:
        nonlocal commit_file_content
        if args[0] == "commit" and args[1] == "--file":
            commit_file_content = Path(args[2]).read_text(encoding="utf-8")

    with (
        _commit_environment(tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.run", side_effect=capture_run),
    ):
        result = main(
            [
                "--type",
                "fix",
                "--scope",
                "lint",
                "--message",
                "correct regex",
                "--body",
                "Fixed edge case",
            ]
        )
    assert result == 0
    assert "fix(lint): correct regex" in commit_file_content
    assert "Fixed edge case" in commit_file_content
    assert "Co-Authored-By" not in commit_file_content


# --------------------------------------------------------------------------
# Config error handling
# --------------------------------------------------------------------------

_DEFAULT_ARGS = ["--type", "feat", "--scope", "core", "--message", "test"]


def test_main_config_error(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("[invalid\n")
    with (
        patch("vergil_tooling.bin.vrg_commit.git.current_branch", return_value="feature/42-test"),
        patch("vergil_tooling.bin.vrg_commit.git.repo_root", return_value=tmp_path),
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 1


def test_main_missing_config(tmp_path: Path) -> None:
    with (
        patch("vergil_tooling.bin.vrg_commit.git.current_branch", return_value="feature/42-test"),
        patch("vergil_tooling.bin.vrg_commit.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.is_main_worktree", return_value=False),
        patch("vergil_tooling.bin.vrg_commit.git.has_staged_changes", return_value=True),
        patch("vergil_tooling.bin.vrg_commit.git.run"),
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 0


# --------------------------------------------------------------------------
# No AI co-author trailer (#3200)
# --------------------------------------------------------------------------


def test_stale_vrg_co_author_env_var_is_ignored(tmp_path: Path) -> None:
    """The retired VRG_CO_AUTHOR env var no longer stamps a trailer."""
    commit_file_content = ""

    def capture_run(*args: str) -> None:
        nonlocal commit_file_content
        if args[0] == "commit" and args[1] == "--file":
            commit_file_content = Path(args[2]).read_text(encoding="utf-8")

    with (
        _commit_environment(tmp_path),
        patch.dict(os.environ, {"VRG_CO_AUTHOR": "Claude <noreply@anthropic.com>"}, clear=False),
        patch("vergil_tooling.bin.vrg_commit.git.run", side_effect=capture_run),
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 0
    assert "Co-Authored-By" not in commit_file_content


# --------------------------------------------------------------------------
# Task 1.1 — branch / context validation
# --------------------------------------------------------------------------


# Check 1: detached HEAD


def test_validate_rejects_detached_head(tmp_path: Path) -> None:
    with _commit_environment(tmp_path, branch="HEAD"):
        assert main(_DEFAULT_ARGS) == 1


def test_validate_admits_normal_branch(tmp_path: Path) -> None:
    with _commit_environment(tmp_path, branch="feature/42-test", branching_model="library-release"):
        assert main(_DEFAULT_ARGS) == 0


# Check 2: protected branches


@pytest.mark.parametrize("branch", ["develop", "release", "main"])
def test_validate_rejects_protected_branches(tmp_path: Path, branch: str) -> None:
    with _commit_environment(tmp_path, branch=branch):
        assert main(_DEFAULT_ARGS) == 1


# Check 3: branch prefix against branching_model


def test_validate_rejects_invalid_prefix_for_library_release(tmp_path: Path) -> None:
    with _commit_environment(
        tmp_path, branch="promotion/42-deploy", branching_model="library-release"
    ):
        assert main(_DEFAULT_ARGS) == 1


def test_validate_admits_release_branch_for_library_release(tmp_path: Path) -> None:
    with _commit_environment(tmp_path, branch="release/1.2.3", branching_model="library-release"):
        assert main(_DEFAULT_ARGS) == 0


def test_validate_rejects_hotfix_for_docs_single_branch(tmp_path: Path) -> None:
    with _commit_environment(
        tmp_path, branch="hotfix/42-urgent", branching_model="docs-single-branch"
    ):
        assert main(_DEFAULT_ARGS) == 1


def test_validate_admits_promotion_for_application_promotion(tmp_path: Path) -> None:
    with _commit_environment(
        tmp_path, branch="promotion/42-deploy", branching_model="application-promotion"
    ):
        assert main(_DEFAULT_ARGS) == 0


def test_validate_rejects_unknown_branching_model(tmp_path: Path) -> None:
    mock = "vergil_tooling.bin.vrg_commit.git.current_branch"
    with patch(mock, return_value="feature/42-thing"):
        assert _validate_commit_context(tmp_path, "bogus-model") == 1


def test_validate_falls_back_when_no_config(tmp_path: Path) -> None:
    mock = "vergil_tooling.bin.vrg_commit.git.current_branch"
    with patch(mock, return_value="feature/42-test"):
        assert _validate_commit_context(tmp_path, "") == 0


def test_validate_fallback_rejects_hotfix(tmp_path: Path) -> None:
    mock = "vergil_tooling.bin.vrg_commit.git.current_branch"
    with patch(mock, return_value="hotfix/42-urgent"):
        assert _validate_commit_context(tmp_path, "") == 1


# Check 4: issue number in branch name


def test_validate_rejects_missing_issue_number(tmp_path: Path) -> None:
    with _commit_environment(
        tmp_path, branch="feature/no-number", branching_model="library-release"
    ):
        assert main(_DEFAULT_ARGS) == 1


def test_validate_admits_bugfix_with_issue(tmp_path: Path) -> None:
    with _commit_environment(
        tmp_path, branch="bugfix/99-fix-parsing", branching_model="library-release"
    ):
        assert main(_DEFAULT_ARGS) == 0


def test_validate_admits_chore_with_issue(tmp_path: Path) -> None:
    with _commit_environment(
        tmp_path, branch="chore/5-update-deps", branching_model="library-release"
    ):
        assert main(_DEFAULT_ARGS) == 0


def test_validate_rejects_application_promotion_hotfix_without_issue(tmp_path: Path) -> None:
    with _commit_environment(
        tmp_path,
        branch="hotfix/no-number",
        branching_model="application-promotion",
    ):
        assert main(_DEFAULT_ARGS) == 1


# Check 5: worktree convention — main-tree feature commits forbidden when
# .worktrees/ exists


def test_validate_rejects_main_worktree_feature_commit_when_worktrees_dir(
    tmp_path: Path,
) -> None:
    (tmp_path / ".worktrees").mkdir()
    with _commit_environment(
        tmp_path,
        branch="feature/42-x",
        branching_model="library-release",
        is_main_worktree=True,
    ):
        assert main(_DEFAULT_ARGS) == 1


def test_validate_admits_secondary_worktree_feature_commit(tmp_path: Path) -> None:
    (tmp_path / ".worktrees").mkdir()
    with _commit_environment(
        tmp_path,
        branch="feature/42-x",
        branching_model="library-release",
        is_main_worktree=False,
    ):
        assert main(_DEFAULT_ARGS) == 0


def test_validate_admits_main_worktree_release_commit_when_worktrees_dir(
    tmp_path: Path,
) -> None:
    (tmp_path / ".worktrees").mkdir()
    with _commit_environment(
        tmp_path,
        branch="release/1.2.3",
        branching_model="library-release",
        is_main_worktree=True,
    ):
        assert main(_DEFAULT_ARGS) == 0


def test_validate_admits_main_worktree_feature_commit_without_worktrees_dir(
    tmp_path: Path,
) -> None:
    with _commit_environment(
        tmp_path,
        branch="feature/42-x",
        branching_model="library-release",
        is_main_worktree=True,
    ):
        assert main(_DEFAULT_ARGS) == 0


# --------------------------------------------------------------------------
# Check 6: auto-close keywords in commit body
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Closes #42",
        "closes #42",
        "CLOSES #42",
        "Close #42",
        "Closed #42",
        "Fixes #42",
        "fixes #42",
        "Fix #42",
        "Fixed #42",
        "Resolves #42",
        "resolves #42",
        "Resolve #42",
        "Resolved #42",
        "Fixes: #42",
        "Closes owner/repo#42",
        "Some context.\n\nCloses #99",
    ],
)
def test_validate_rejects_autoclose_keywords_in_body(tmp_path: Path, body: str) -> None:
    with _commit_environment(tmp_path):
        result = main(
            [
                "--type",
                "feat",
                "--scope",
                "core",
                "--message",
                "test",
                "--body",
                body,
            ]
        )
    assert result == 1


def test_autoclose_rejection_message_directs_to_pr_submit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rejection guidance points at PR-submit linkage, not a stale 'Ref #N'."""
    with _commit_environment(tmp_path):
        result = main(
            [
                "--type",
                "feat",
                "--scope",
                "core",
                "--message",
                "test",
                "--body",
                "Closes #42",
            ]
        )
    assert result == 1
    err = capsys.readouterr().err
    # The stale recommendation is gone.
    assert "Ref #N" not in err
    # The new guidance explains linkage is set on the PR at submit time.
    assert "report-ready" in err
    assert "no issue-linkage keyword" in err


@pytest.mark.parametrize(
    "body",
    [
        "Ref #42",
        "This closes the loop on the design.",
        "Fixed the edge case for empty input.",
        "Resolves a long-standing performance issue.",
        "",
    ],
)
def test_validate_admits_safe_body_content(tmp_path: Path, body: str) -> None:
    with _commit_environment(tmp_path):
        result = main(
            [
                "--type",
                "feat",
                "--scope",
                "core",
                "--message",
                "test",
                "--body",
                body,
            ]
        )
    assert result == 0


# --------------------------------------------------------------------------
# Post-report-ready freeze (#2346)
# --------------------------------------------------------------------------


def test_main_refuses_commit_when_frozen(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from vergil_tooling.lib.pr_workflow.freeze import FreezeCheck

    with (
        _commit_environment(tmp_path),
        patch(
            "vergil_tooling.bin.vrg_commit.freeze.check_worktree",
            return_value=FreezeCheck(frozen=True, message="branch is FROZEN; use unfreeze"),
        ),
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 1
    assert "FROZEN" in capsys.readouterr().err


def test_main_warns_but_proceeds_on_freeze_read_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from vergil_tooling.lib.pr_workflow.freeze import FreezeCheck

    with (
        _commit_environment(tmp_path),
        patch(
            "vergil_tooling.bin.vrg_commit.freeze.check_worktree",
            return_value=FreezeCheck(frozen=False, read_error="bad json"),
        ),
    ):
        result = main(["--type", "feat", "--scope", "core", "--message", "add feature"])
    assert result == 0
    assert "could not read PR-workflow state" in capsys.readouterr().err


# --------------------------------------------------------------------------
# Local-settings guard (epic vergil-project/.github#558)
# --------------------------------------------------------------------------


def test_vrg_commit_refuses_for_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = local_settings_violation(tmp_path, monkeypatch)
    monkeypatch.setenv("VRG_IDENTITY_MODE", "user")
    with (
        _commit_environment(tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.run") as mock_run,
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 1
    mock_run.assert_not_called()
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == REFUSAL_TEMPLATE.format(path=path)
    assert lines[1] == AGENT_LINE


def test_vrg_commit_warns_for_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = local_settings_violation(tmp_path, monkeypatch)
    with (
        _commit_environment(tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.run") as mock_run,
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 0
    mock_run.assert_called_once()
    assert WARNING_TEMPLATE.format(path=path) in capsys.readouterr().err.splitlines()
