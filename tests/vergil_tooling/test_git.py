"""Tests for standard_tooling.lib.git."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from standard_tooling.lib import git


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_run_delegates_to_subprocess() -> None:
    """Non-commit commands run with env=None (inherit parent env)."""
    with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        git.run("status")
    mock_run.assert_called_once_with(("git", "status"), check=True, env=None)


def test_run_sets_st_commit_context_for_commit() -> None:
    """`git commit` calls must set ST_COMMIT_CONTEXT=1 in the subprocess
    env so the repo's pre-commit gate (.githooks/pre-commit) admits the
    commit. This is the contract that lets every internal `st-*` tool
    pass through the gate without touching its own commit-time
    plumbing. Issue #295.
    """
    with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        git.run("commit", "-m", "msg")
    args, kwargs = mock_run.call_args
    assert args == (("git", "commit", "-m", "msg"),)
    assert kwargs["check"] is True
    assert kwargs["env"] is not None
    assert kwargs["env"]["ST_COMMIT_CONTEXT"] == "1"


def test_run_does_not_mutate_parent_env_for_commit() -> None:
    """The env-var contract is propagated via subprocess env, not by
    mutating `os.environ`. Verify the parent process is unaffected.
    """
    import os as _os

    parent_value = _os.environ.get("ST_COMMIT_CONTEXT")
    try:
        _os.environ.pop("ST_COMMIT_CONTEXT", None)
        with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
            mock_run.return_value = _completed()
            git.run("commit", "-m", "msg")
        assert "ST_COMMIT_CONTEXT" not in _os.environ
    finally:
        if parent_value is not None:
            _os.environ["ST_COMMIT_CONTEXT"] = parent_value


def test_run_does_not_set_st_commit_context_for_non_commit() -> None:
    """`git status`, `git push`, etc. should not get ST_COMMIT_CONTEXT
    set — only `git commit` triggers the gate-admit path.
    """
    with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        git.run("push", "origin", "main")
    _args, kwargs = mock_run.call_args
    assert kwargs["env"] is None


def test_read_output_returns_stripped_stdout() -> None:
    with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(stdout="  hello world  \n")
        assert git.read_output("log") == "hello world"
    mock_run.assert_called_once_with(("git", "log"), check=True, text=True, capture_output=True)


def test_repo_root_returns_path() -> None:
    with patch("standard_tooling.lib.git.read_output", return_value="/var/repo"):  # noqa: S108
        result = git.repo_root()
    assert result == Path("/var/repo")


def test_current_branch_returns_name() -> None:
    with patch("standard_tooling.lib.git.read_output", return_value="feature/test"):
        result = git.current_branch()
    assert result == "feature/test"


def test_is_main_worktree_true() -> None:
    with patch(
        "standard_tooling.lib.git.read_output",
        side_effect=["/repo/.git", "/repo/.git"],
    ):
        assert git.is_main_worktree() is True


def test_is_main_worktree_false() -> None:
    with patch(
        "standard_tooling.lib.git.read_output",
        side_effect=["/repo/.git/worktrees/feature-x", "/repo/.git"],
    ):
        assert git.is_main_worktree() is False


def test_main_worktree_root_from_main() -> None:
    with patch(
        "standard_tooling.lib.git.read_output",
        return_value="/repo/.git",
    ):
        assert git.main_worktree_root() == Path("/repo")


def test_main_worktree_root_from_secondary() -> None:
    with patch(
        "standard_tooling.lib.git.read_output",
        return_value="/repo/.git",
    ):
        assert git.main_worktree_root() == Path("/repo")


def test_has_staged_changes_true() -> None:
    with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=1)
        assert git.has_staged_changes() is True


def test_has_staged_changes_false() -> None:
    with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        assert git.has_staged_changes() is False


def test_ref_exists_true() -> None:
    with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        assert git.ref_exists("main") is True


def test_ref_exists_false() -> None:
    with patch("standard_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=1)
        assert git.ref_exists("nonexistent") is False


def test_merged_branches_returns_list() -> None:
    with patch("standard_tooling.lib.git.read_output", return_value="feature/a\nfeature/b"):
        result = git.merged_branches("develop")
    assert result == ["feature/a", "feature/b"]


def test_merged_branches_empty() -> None:
    with patch("standard_tooling.lib.git.read_output", return_value=""):
        result = git.merged_branches("develop")
    assert result == []


def test_working_tree_status_returns_porcelain_output() -> None:
    with patch("standard_tooling.lib.git.read_output", return_value="?? orphan.md") as mock:
        result = git.working_tree_status()
    assert result == "?? orphan.md"
    mock.assert_called_once_with("status", "--porcelain")


def test_working_tree_status_returns_empty_when_clean() -> None:
    with patch("standard_tooling.lib.git.read_output", return_value=""):
        assert git.working_tree_status() == ""
