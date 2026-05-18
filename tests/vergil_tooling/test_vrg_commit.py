"""Tests for vergil_tooling.bin.vrg_commit."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_commit import _validate_commit_context, main, parse_args

if TYPE_CHECKING:
    from collections.abc import Iterator


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
) -> Iterator[None]:
    """Set up mocks for `commit.main()`.

    Defaults represent a happy path: secondary worktree, library-release
    config, valid feature/42-test branch, staged changes present.

    When *write_config* is True (default), a ``vergil.toml``
    is written with the given *branching_model*.  Set *write_config*
    to False to test the no-config fallback path.
    """
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
        patch(
            "vergil_tooling.bin.vrg_commit.github.resolve_co_author_trailer",
            return_value="Co-Authored-By: test-agent <test-agent@test.com>",
        ),
    ):
        yield


def test_parse_args_required() -> None:
    args = parse_args(
        ["--type", "feat", "--scope", "core", "--message", "add thing", "--agent", "agent"]
    )
    assert args.commit_type == "feat"
    assert args.scope == "core"
    assert args.message == "add thing"
    assert args.agent == "agent"
    assert args.body == ""


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
            "--agent",
            "agent",
        ]
    )
    assert args.commit_type == "fix"
    assert args.scope == "lint"
    assert args.body == "Fixed edge case"


def test_parse_args_revert_type() -> None:
    args = parse_args(
        [
            "--type",
            "revert",
            "--scope",
            "auth",
            "--message",
            "undo token change",
            "--agent",
            "agent",
        ]
    )
    assert args.commit_type == "revert"


def test_parse_args_invalid_type() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--type", "invalid", "--scope", "core", "--message", "x", "--agent", "agent"])


def test_main_no_staged_changes(tmp_path: Path) -> None:
    with _commit_environment(tmp_path, has_staged=False):
        result = main(
            ["--type", "feat", "--scope", "core", "--message", "test", "--agent", "agent"]
        )
    assert result == 1


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
        result = main(
            ["--type", "feat", "--scope", "core", "--message", "add feature", "--agent", "agent"]
        )
    assert result == 0
    assert commit_file_content.startswith("feat(core): add feature\n")
    assert "Co-Authored-By: test-agent <test-agent@test.com>" in commit_file_content


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
                "--agent",
                "agent",
            ]
        )
    assert result == 0
    assert "fix(lint): correct regex" in commit_file_content
    assert "Fixed edge case" in commit_file_content
    assert "Co-Authored-By: test-agent <test-agent@test.com>" in commit_file_content


# --------------------------------------------------------------------------
# Config error handling
# --------------------------------------------------------------------------


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
        patch(
            "vergil_tooling.bin.vrg_commit.github.resolve_co_author_trailer",
            return_value="Co-Authored-By: test-agent <test-agent@test.com>",
        ),
    ):
        result = main(_DEFAULT_ARGS)
    assert result == 0


# --------------------------------------------------------------------------
# Co-author auto-discovery
# --------------------------------------------------------------------------


def test_main_auto_discovery(tmp_path: Path) -> None:
    commit_file_content = ""

    def capture_run(*args: str) -> None:
        nonlocal commit_file_content
        if args[0] == "commit" and args[1] == "--file":
            commit_file_content = Path(args[2]).read_text(encoding="utf-8")

    with (
        _commit_environment(tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.run", side_effect=capture_run),
        patch(
            "vergil_tooling.bin.vrg_commit.github.resolve_co_author_trailer",
            return_value="Co-Authored-By: jdoe-vergil <12345+jdoe-vergil@users.noreply.github.com>",
        ),
    ):
        result = main(
            ["--type", "feat", "--scope", "core", "--message", "add feature"]
        )
    assert result == 0
    assert commit_file_content.startswith("feat(core): add feature\n")
    assert "Co-Authored-By: jdoe-vergil <12345+jdoe-vergil@users.noreply.github.com>" in commit_file_content


def test_main_agent_flag_prints_deprecation_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        _commit_environment(tmp_path),
        patch("vergil_tooling.bin.vrg_commit.git.run"),
        patch(
            "vergil_tooling.bin.vrg_commit.github.resolve_co_author_trailer",
            return_value="Co-Authored-By: jdoe-vergil <12345+jdoe-vergil@users.noreply.github.com>",
        ),
    ):
        result = main(
            ["--type", "feat", "--scope", "core", "--message", "test", "--agent", "ignored"]
        )
    assert result == 0
    err = capsys.readouterr().err
    assert "deprecated" in err.lower()


# --------------------------------------------------------------------------
# Task 1.1 — branch / context validation
# --------------------------------------------------------------------------
#
# Five checks ported from src/vergil_tooling/bin/pre_commit_hook.py into
# vrg-commit. Each check has a rejection-path and a happy-path test.
# Reference: docs/specs/host-level-tool.md "Migration / vergil-tooling
# itself" step 1; docs/plans/host-level-tool-plan.md Task 1.1.

_DEFAULT_ARGS = ["--type", "feat", "--scope", "core", "--message", "test"]


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
    # promotion branches are allowed and not subject to the issue-number rule.
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
    # release/* is not subject to the worktree-convention check (only
    # feature|bugfix|hotfix|chore are scoped under that rule).
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
        # No .worktrees/ → the rule does not apply.
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
                "--agent",
                "agent",
            ]
        )
    assert result == 1


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
                "--agent",
                "agent",
            ]
        )
    assert result == 0


# --------------------------------------------------------------------------
# Task 1.2 — `git.run` is responsible for setting VRG_COMMIT_CONTEXT=1
# (issue #295 moved the contract from commit.py to lib/git.py). The
# pinning test for that contract lives in tests/vergil_tooling/test_git.py;
# commit.py just calls `git.run("commit", ...)` and trusts the helper.
