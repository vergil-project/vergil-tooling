"""Tests for vergil_tooling.lib.git."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib import git


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_run_delegates_to_subprocess() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        git.run("status")
    mock_run.assert_called_once_with(("git", "status"), check=True, capture_output=True, text=True)


def test_run_commit_no_env_var_gate() -> None:
    """Commit calls no longer set VRG_COMMIT_CONTEXT — the git hook
    has been replaced by a Claude Code PreToolUse hook (#1135).
    """
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        git.run("commit", "-m", "msg")
    _args, kwargs = mock_run.call_args
    assert "env" not in kwargs or kwargs.get("env") is None


def test_run_prints_captured_output(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        result = _completed(stdout="branch created\n")
        result.stderr = "warning: refname\n"
        mock_run.return_value = result
        git.run("checkout", "-b", "test")
    captured = capsys.readouterr()
    assert "branch created" in captured.out
    assert "warning: refname" in captured.err


def test_run_error_carries_output() -> None:
    err = subprocess.CalledProcessError(1, "git push", output="out", stderr="err")
    with (
        patch("vergil_tooling.lib.git.subprocess.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError) as exc_info,
    ):
        git.run("push", "origin", "main")
    assert exc_info.value.stdout == "out"
    assert exc_info.value.stderr == "err"


def test_run_prints_stderr_on_error(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, "git push", output="partial\n", stderr="fatal: error\n")
    with (
        patch("vergil_tooling.lib.git.subprocess.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        git.run("push", "origin", "main")
    captured = capsys.readouterr()
    assert "partial" in captured.out
    assert "fatal: error" in captured.err


def test_run_error_no_output(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, "git status")
    err.stderr = ""
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.git.subprocess.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        git.run("status")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_read_output_prints_stderr_on_error(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, "git log", stderr="fatal: not a repo\n")
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.git.subprocess.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        git.read_output("log")
    captured = capsys.readouterr()
    assert "not a repo" in captured.err


def test_read_output_error_no_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, "git log")
    err.stderr = ""
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.git.subprocess.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        git.read_output("log")
    captured = capsys.readouterr()
    assert captured.err == ""


def test_read_output_returns_stripped_stdout() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(stdout="  hello world  \n")
        assert git.read_output("log") == "hello world"
    mock_run.assert_called_once_with(("git", "log"), check=True, text=True, capture_output=True)


def test_repo_root_returns_path() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="/var/repo"):  # noqa: S108
        result = git.repo_root()
    assert result == Path("/var/repo")


def test_current_branch_returns_name() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="feature/test"):
        result = git.current_branch()
    assert result == "feature/test"


def test_is_main_worktree_true() -> None:
    with patch(
        "vergil_tooling.lib.git.read_output",
        side_effect=["/repo/.git", "/repo/.git"],
    ):
        assert git.is_main_worktree() is True


def test_is_main_worktree_false() -> None:
    with patch(
        "vergil_tooling.lib.git.read_output",
        side_effect=["/repo/.git/worktrees/feature-x", "/repo/.git"],
    ):
        assert git.is_main_worktree() is False


def test_main_worktree_root_from_main() -> None:
    with patch(
        "vergil_tooling.lib.git.read_output",
        return_value="/repo/.git",
    ):
        assert git.main_worktree_root() == Path("/repo")


def test_main_worktree_root_from_secondary() -> None:
    with patch(
        "vergil_tooling.lib.git.read_output",
        return_value="/repo/.git",
    ):
        assert git.main_worktree_root() == Path("/repo")


def test_has_staged_changes_true() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=1)
        assert git.has_staged_changes() is True


def test_has_staged_changes_false() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        assert git.has_staged_changes() is False


def test_ref_exists_true() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        assert git.ref_exists("main") is True


def test_ref_exists_false() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=1)
        assert git.ref_exists("nonexistent") is False


def test_merged_branches_returns_list() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="feature/a\nfeature/b"):
        result = git.merged_branches("develop")
    assert result == ["feature/a", "feature/b"]


def test_merged_branches_empty() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value=""):
        result = git.merged_branches("develop")
    assert result == []


def test_working_tree_status_returns_porcelain_output() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="?? orphan.md") as mock:
        result = git.working_tree_status()
    assert result == "?? orphan.md"
    mock.assert_called_once_with("status", "--porcelain")


def test_working_tree_status_returns_empty_when_clean() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value=""):
        assert git.working_tree_status() == ""
