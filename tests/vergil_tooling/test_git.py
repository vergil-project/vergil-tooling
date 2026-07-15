"""Tests for vergil_tooling.lib.git."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib import git


def _completed(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


def test_run_delegates_to_progress_run() -> None:
    with patch("vergil_tooling.lib.git.progress") as m_progress:
        git.run("status")
    _args, kwargs = m_progress.run.call_args
    assert _args[0] == ("git", "status")
    # Local commands carry no auth header but still disable the prompt (#1830).
    env = kwargs["env"]
    assert "GIT_CONFIG_KEY_0" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_run_commit_no_env_var_gate() -> None:
    """Commit calls no longer set VRG_COMMIT_CONTEXT — the git hook
    has been replaced by a Claude Code PreToolUse hook (#1135).
    """
    with patch("vergil_tooling.lib.git.progress") as m_progress:
        git.run("commit", "-m", "msg")
    _args, kwargs = m_progress.run.call_args
    assert "GIT_CONFIG_KEY_0" not in kwargs["env"]


def test_run_raises_on_failure() -> None:
    err = subprocess.CalledProcessError(1, ("git", "status"), output="so", stderr="se")
    with (
        patch("vergil_tooling.lib.git.progress") as m_progress,
        pytest.raises(subprocess.CalledProcessError) as excinfo,
    ):
        m_progress.run.side_effect = err
        git.run("status")
    assert excinfo.value.output == "so"
    assert excinfo.value.stderr == "se"


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
    _args, kwargs = mock_run.call_args
    assert _args[0] == ("git", "log")
    assert kwargs["check"] is True
    assert kwargs["text"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "GIT_CONFIG_KEY_0" not in kwargs["env"]


def test_read_output_stdin_feeds_stdin_and_returns_stripped_stdout() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(stdout="  blobsha  \n")
        result = git.read_output_stdin("payload", "hash-object", "-w", "--stdin")
    assert result == "blobsha"
    _args, kwargs = mock_run.call_args
    assert _args[0] == ("git", "hash-object", "-w", "--stdin")
    assert kwargs["input"] == "payload"
    assert kwargs["check"] is True
    assert kwargs["capture_output"] is True
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_read_output_stdin_prints_stderr_on_error(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, "git mktree", stderr="fatal: bad tree\n")
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.git.subprocess.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        git.read_output_stdin("spec", "mktree")
    assert "bad tree" in capsys.readouterr().err


def test_read_output_stdin_error_no_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(1, "git mktree")
    err.stderr = ""
    err.stdout = ""
    with (
        patch("vergil_tooling.lib.git.subprocess.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        git.read_output_stdin("spec", "mktree")
    assert capsys.readouterr().err == ""


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
    # Even this local op disables the prompt so it can never hang (#1830).
    _, kwargs = mock_run.call_args
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_has_staged_changes_false() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        assert git.has_staged_changes() is False


def test_ref_exists_true() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=0)
        assert git.ref_exists("main") is True
    # Even this local op disables the prompt so it can never hang (#1830).
    _, kwargs = mock_run.call_args
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_ref_exists_false() -> None:
    with patch("vergil_tooling.lib.git.subprocess.run") as mock_run:
        mock_run.return_value = _completed(returncode=1)
        assert git.ref_exists("nonexistent") is False


def test_commit_sha_resolves_ref() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="abc123") as mock:
        assert git.commit_sha("develop") == "abc123"
    mock.assert_called_once_with("rev-parse", "develop")


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


# -- remote credential injection -----------------------------------------------


class TestRunRemoteCredentialInjection:
    """git.run() injects credentials for remote-capable subcommands."""

    # "remote" is included (#1830): `git remote prune` ls-remotes the origin,
    # so it must be token-injected just like fetch/pull.
    @pytest.mark.parametrize("subcmd", ["push", "pull", "fetch", "ls-remote", "remote"])
    def test_injects_token_for_remote_commands(self, subcmd: str) -> None:
        with (
            patch(
                "vergil_tooling.lib.git.github.get_installation_token",
                return_value="ghs_test_token",
            ),
            patch("vergil_tooling.lib.git.progress") as mock_progress,
        ):
            git.run(subcmd, "origin", "main")
        _, kwargs = mock_progress.run.call_args
        env = kwargs.get("env")
        assert env is not None
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
        assert "Authorization: Basic" in env["GIT_CONFIG_VALUE_0"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    @pytest.mark.parametrize("subcmd", ["status", "log", "diff", "add", "branch", "commit"])
    def test_no_injection_for_local_commands(self, subcmd: str) -> None:
        with (
            patch(
                "vergil_tooling.lib.git.github.get_installation_token",
                return_value="ghs_test_token",
            ),
            patch("vergil_tooling.lib.git.progress") as mock_progress,
        ):
            git.run(subcmd)
        _, kwargs = mock_progress.run.call_args
        env = kwargs["env"]
        # No auth header for local commands, but the prompt is still disabled.
        assert "GIT_CONFIG_KEY_0" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_no_injection_when_no_token(self) -> None:
        with (
            patch(
                "vergil_tooling.lib.git.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.lib.git.progress") as mock_progress,
        ):
            git.run("push", "origin", "main")
        _, kwargs = mock_progress.run.call_args
        env = kwargs["env"]
        # Without a token the remote op carries no auth header — but the prompt
        # stays disabled so it fails fast instead of hanging (#1830).
        assert "GIT_CONFIG_KEY_0" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_token_encodes_as_basic_auth(self) -> None:
        import base64

        with (
            patch(
                "vergil_tooling.lib.git.github.get_installation_token",
                return_value="ghs_test_token",
            ),
            patch("vergil_tooling.lib.git.progress") as mock_progress,
        ):
            git.run("push", "origin", "main")
        _, kwargs = mock_progress.run.call_args
        header_value = kwargs["env"]["GIT_CONFIG_VALUE_0"]
        expected = base64.b64encode(b"x-access-token:ghs_test_token").decode()
        assert expected in header_value


class TestReadOutputRemoteCredentialInjection:
    """git.read_output() injects credentials for remote-capable subcommands."""

    @pytest.mark.parametrize("subcmd", ["ls-remote", "fetch"])
    def test_injects_token_for_remote_commands(self, subcmd: str) -> None:
        with (
            patch(
                "vergil_tooling.lib.git.github.get_installation_token",
                return_value="ghs_test_token",
            ),
            patch("vergil_tooling.lib.git.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout="output\n")
            git.read_output(subcmd, "origin")
        _, kwargs = mock_run.call_args
        env = kwargs.get("env")
        assert env is not None
        assert "Authorization: Basic" in env["GIT_CONFIG_VALUE_0"]

    @pytest.mark.parametrize("subcmd", ["log", "rev-parse", "status"])
    def test_no_injection_for_local_commands(self, subcmd: str) -> None:
        with (
            patch(
                "vergil_tooling.lib.git.github.get_installation_token",
                return_value="ghs_test_token",
            ),
            patch("vergil_tooling.lib.git.subprocess.run") as mock_run,
        ):
            mock_run.return_value = _completed(stdout="output\n")
            git.read_output(subcmd)
        _, kwargs = mock_run.call_args
        env = kwargs["env"]
        assert "GIT_CONFIG_KEY_0" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_commits_ahead_parses_rev_list_count() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="3") as ro:
        assert git.commits_ahead("develop", "feature/x") == 3
    ro.assert_called_once_with("rev-list", "--count", "develop..feature/x")


def test_committer_timestamp_returns_epoch_int() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="1700000000"):
        assert git.committer_timestamp("/repo/.worktrees/issue-1-x") == 1700000000


def test_committer_timestamp_invokes_log_with_dash_c() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="1700000000") as mock_ro:
        git.committer_timestamp("/wt")
    mock_ro.assert_called_once_with("-C", "/wt", "log", "-1", "--format=%ct", "HEAD")
