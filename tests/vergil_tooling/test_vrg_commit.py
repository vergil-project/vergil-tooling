"""Tests for vergil_tooling.bin.vrg_commit."""

from __future__ import annotations

import contextlib
import os
import subprocess
from dataclasses import dataclass
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


# --------------------------------------------------------------------------
# --fixup <commit> (epic vergil-project/.github#1019)
# --------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@dataclass(frozen=True)
class _FixupRepo:
    repo: Path
    base_sha: str
    branch_sha: str


@pytest.fixture
def fixup_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FixupRepo:
    """A repo whose `origin/develop` holds a base commit, on a feature branch with one commit.

    The working directory is the repo, with one change staged for a fixup.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.name", "Me")
    _git(repo, "config", "user.email", "me@here.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-q", "-m", "chore(core): base on develop")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/develop", base_sha)

    _git(repo, "switch", "-q", "-c", "feature/42-x")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "feat(core): add the a file (#42)")
    branch_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "a.txt").write_text("a, fixed\n")
    _git(repo, "add", "a.txt")
    monkeypatch.chdir(repo)
    return _FixupRepo(repo=repo, base_sha=base_sha, branch_sha=branch_sha)


def test_fixup_creates_fixup_commit(fixup_repo: _FixupRepo) -> None:
    assert main(["--fixup", fixup_repo.branch_sha]) == 0
    subject = _git(fixup_repo.repo, "log", "-1", "--format=%s")
    assert subject == "fixup! feat(core): add the a file (#42)"
    assert _git(fixup_repo.repo, "rev-parse", "HEAD~1") == fixup_repo.branch_sha
    assert _git(fixup_repo.repo, "status", "--porcelain") == ""


def test_fixup_accepts_a_ref_to_the_target(fixup_repo: _FixupRepo) -> None:
    assert main(["--fixup", "HEAD"]) == 0
    subject = _git(fixup_repo.repo, "log", "-1", "--format=%s")
    assert subject == "fixup! feat(core): add the a file (#42)"


def _assert_fixup_refused(
    fixup_repo: _FixupRepo, target: str, capsys: pytest.CaptureFixture[str]
) -> str:
    head_before = _git(fixup_repo.repo, "rev-parse", "HEAD")
    assert main(["--fixup", target]) == 1
    assert _git(fixup_repo.repo, "rev-parse", "HEAD") == head_before
    err = capsys.readouterr().err
    assert f"'{target}'" in err
    return err


def test_fixup_refuses_commit_not_on_branch(
    fixup_repo: _FixupRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    # A commit on another branch: not reachable from HEAD.
    repo = fixup_repo.repo
    _git(repo, "stash", "-q")
    _git(repo, "switch", "-q", "-c", "feature/43-other", fixup_repo.base_sha)
    (repo / "other.txt").write_text("other\n")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-q", "-m", "feat(core): other branch (#43)")
    other_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "feature/42-x")
    _git(repo, "stash", "pop", "-q")

    err = _assert_fixup_refused(fixup_repo, other_sha, capsys)
    assert "not on the current branch" in err


def test_fixup_refuses_commit_already_on_base(
    fixup_repo: _FixupRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _assert_fixup_refused(fixup_repo, fixup_repo.base_sha, capsys)
    assert "already part of 'origin/develop'" in err


def test_fixup_refuses_unknown_commit(
    fixup_repo: _FixupRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _assert_fixup_refused(fixup_repo, "no-such-commit", capsys)
    assert "does not resolve to a commit" in err


def test_fixup_refuses_without_base_ref(
    fixup_repo: _FixupRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(fixup_repo.repo, "update-ref", "-d", "refs/remotes/origin/develop")
    err = _assert_fixup_refused(fixup_repo, fixup_repo.branch_sha, capsys)
    assert "cannot resolve 'origin/develop'" in err


def test_fixup_refuses_with_no_staged_changes(fixup_repo: _FixupRepo) -> None:
    _git(fixup_repo.repo, "restore", "--staged", "a.txt")
    assert main(["--fixup", fixup_repo.branch_sha]) == 1
    assert _git(fixup_repo.repo, "rev-parse", "HEAD") == fixup_repo.branch_sha


@pytest.mark.parametrize(
    ("extra", "named"),
    [
        (["--type", "feat"], "--type"),
        (["--scope", "core"], "--scope"),
        (["--message", "x"], "--message"),
        (["--body", "more"], "--body"),
        (["--type", "feat", "--scope", "core", "--message", "x"], "--type, --scope, --message"),
    ],
)
def test_fixup_refuses_with_type_or_message(
    extra: list[str], named: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["--fixup", "abc123", *extra])
    assert exc.value.code == 2
    assert f"--fixup cannot be used with {named}" in capsys.readouterr().err


def test_parse_args_fixup_alone() -> None:
    args = parse_args(["--fixup", "abc123"])
    assert args.fixup == "abc123"
    assert args.commit_type is None
    assert args.body is None


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        (["--scope", "core", "--message", "x"], "--type"),
        (["--type", "feat", "--message", "x"], "--scope"),
        (["--type", "feat", "--scope", "core"], "--message"),
        ([], "--type, --scope, --message"),
    ],
)
def test_parse_args_requires_message_flags_without_fixup(
    argv: list[str], named: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(argv)
    assert exc.value.code == 2
    assert f"the following arguments are required: {named}" in capsys.readouterr().err


def test_fixup_refused_on_frozen_branch(
    fixup_repo: _FixupRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    from vergil_tooling.lib.pr_workflow.freeze import FreezeCheck

    with patch(
        "vergil_tooling.bin.vrg_commit.freeze.check_worktree",
        return_value=FreezeCheck(frozen=True, message="branch is FROZEN; use unfreeze"),
    ) as check:
        result = main(["--fixup", fixup_repo.branch_sha])
    assert result == 1
    check.assert_called_once_with(fixup_repo.repo.resolve(), action="add a commit")
    assert "FROZEN" in capsys.readouterr().err
    assert _git(fixup_repo.repo, "rev-parse", "HEAD") == fixup_repo.branch_sha
