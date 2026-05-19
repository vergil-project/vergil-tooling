"""Tests for vergil_tooling.bin.vrg_git."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_git import main

if TYPE_CHECKING:
    from pathlib import Path


# -- no arguments -------------------------------------------------------------


def test_no_args_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) != 0
    assert "usage" in capsys.readouterr().err.lower()


def test_none_argv_reads_sys_argv(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_git.sys.argv", ["vrg-git"]):
        assert main(None) != 0
    assert "usage" in capsys.readouterr().err.lower()


# -- allowed subcommands ------------------------------------------------------

_ALLOWED_SIMPLE = [
    "status",
    "log",
    "diff",
    "show",
    "branch",
    "ls-remote",
    "rev-parse",
    "add",
    "push",
    "fetch",
    "pull",
    "checkout",
    "switch",
    "stash",
    "merge",
    "cherry-pick",
    "rebase",
]


@pytest.mark.parametrize("subcmd", _ALLOWED_SIMPLE)
def test_allowed_subcommand_passes(subcmd: str) -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main([subcmd])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[0] == "git"
    assert args[1] == subcmd


# -- compound subcommands (worktree) ------------------------------------------

_ALLOWED_WORKTREE = ["add", "list", "remove"]


@pytest.mark.parametrize("sub", _ALLOWED_WORKTREE)
def test_worktree_compound_passes(sub: str) -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["worktree", sub])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[:3] == ["git", "worktree", sub]


def test_worktree_unrecognized_sub(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["worktree", "prune"]) != 0
    err = capsys.readouterr().err
    assert "prune" in err


def test_worktree_no_sub(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["worktree"]) != 0


def test_worktree_denied_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_git._FLAG_DENY", {"worktree": {"--force"}}):
        assert main(["worktree", "add", "--force"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


# -- unrecognized subcommands -------------------------------------------------


def test_unrecognized_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bisect"]) != 0
    err = capsys.readouterr().err
    assert "bisect" in err


# -- denied subcommands -------------------------------------------------------

_DENIED = [
    "commit",
    "reset",
    "clean",
    "config",
    "remote",
    "reflog",
    "gc",
    "prune",
    "filter-branch",
    "replace",
]


@pytest.mark.parametrize("subcmd", _DENIED)
def test_denied_subcommand(subcmd: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main([subcmd]) != 0
    err = capsys.readouterr().err
    assert subcmd in err
    assert "denied" in err.lower()


def test_commit_suggests_vrg_commit(capsys: pytest.CaptureFixture[str]) -> None:
    main(["commit"])
    err = capsys.readouterr().err
    assert "vrg-commit" in err


# -- exact-match allowlist ----------------------------------------------------


def test_exact_match_allows_hooks_path() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["config", "core.hooksPath", ".githooks"])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args == ["git", "config", "core.hooksPath", ".githooks"]


def test_exact_match_denies_different_value(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "core.hooksPath", "/tmp/evil"]) != 0  # noqa: S108
    assert "denied" in capsys.readouterr().err.lower()


def test_exact_match_denies_other_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "user.email", "x@example.com"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


def test_exact_match_denies_bare_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


def test_exact_match_logged(tmp_path: Path) -> None:
    log_file = tmp_path / "vrg-git.log"
    with (
        patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        patch("vergil_tooling.bin.vrg_git._log_path", return_value=log_file),
    ):
        mock_run.return_value.returncode = 0
        main(["config", "core.hooksPath", ".githooks"])
    entries = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["result"] == "allowed"


# -- helper: _is_protected_branch --------------------------------------------


def test_is_protected_branch_develop() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="develop\n",
        )
        from vergil_tooling.bin.vrg_git import _is_protected_branch

        assert _is_protected_branch() is True


def test_is_protected_branch_main() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="main\n",
        )
        from vergil_tooling.bin.vrg_git import _is_protected_branch

        assert _is_protected_branch() is True


def test_is_protected_branch_release() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="release/2.0.22\n",
        )
        from vergil_tooling.bin.vrg_git import _is_protected_branch

        assert _is_protected_branch() is True


def test_is_protected_branch_feature() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="feature/827-force-push\n",
        )
        from vergil_tooling.bin.vrg_git import _is_protected_branch

        assert _is_protected_branch() is False


# -- helper: _is_upstream_gone ------------------------------------------------


def test_is_upstream_gone_true() -> None:
    vv_output = (
        "  develop                  abc1234 [origin/develop] latest commit\n"
        "  feature/123-foo          def5678 [origin/feature/123-foo: gone] old commit\n"
        "* feature/827-force-push   ghi9012 [origin/feature/827-force-push] current\n"
    )
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/123-foo") is True


def test_is_upstream_gone_active_upstream() -> None:
    vv_output = (
        "  feature/123-foo abc1234 [origin/feature/123-foo] some commit\n"
    )
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/123-foo") is False


def test_is_upstream_gone_no_upstream() -> None:
    vv_output = "  feature/123-foo abc1234 some commit with no tracking\n"
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/123-foo") is False


def test_is_upstream_gone_branch_not_found() -> None:
    vv_output = "  develop abc1234 [origin/develop] latest commit\n"
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/nonexistent") is False


# -- flag deny lists ----------------------------------------------------------


def test_branch_force_delete_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["branch", "-D"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


def test_branch_force_flag_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["branch", "--force"]) != 0


def test_branch_safe_delete_allowed() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["branch", "-d", "some-branch"])
    assert rc == 0


def test_push_force_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["push", "--force"]) != 0


def test_push_force_short_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["push", "-f"]) != 0


def test_push_force_with_lease_allowed_on_feature_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        patch(
            "vergil_tooling.bin.vrg_git._is_protected_branch",
            return_value=False,
        ),
    ):
        mock_run.return_value.returncode = 0
        rc = main(["push", "--force-with-lease"])
    assert rc == 0


def test_push_force_with_lease_denied_on_develop(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "vergil_tooling.bin.vrg_git._is_protected_branch",
        return_value=True,
    ):
        rc = main(["push", "--force-with-lease"])
    assert rc != 0
    assert "protected branch" in capsys.readouterr().err.lower()


def test_push_force_with_lease_denied_on_release(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch(
        "vergil_tooling.bin.vrg_git._is_protected_branch",
        return_value=True,
    ):
        rc = main(["push", "--force-with-lease"])
    assert rc != 0
    assert "protected branch" in capsys.readouterr().err.lower()


def test_push_normal_allowed() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["push", "origin", "feature/foo"])
    assert rc == 0


def test_checkout_dot_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["checkout", "--", "."]) != 0


def test_checkout_star_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["checkout", "--", "*"]) != 0


def test_checkout_specific_file_allowed() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["checkout", "--", "src/specific/file.py"])
    assert rc == 0


def test_rebase_interactive_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rebase", "-i"]) != 0


def test_rebase_interactive_long_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rebase", "--interactive"]) != 0


def test_rebase_normal_allowed() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["rebase", "main"])
    assert rc == 0


# -- invocation logging -------------------------------------------------------


def test_allowed_invocation_logged(tmp_path: Path) -> None:
    log_file = tmp_path / "vrg-git.log"
    with (
        patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        patch("vergil_tooling.bin.vrg_git._log_path", return_value=log_file),
    ):
        mock_run.return_value.returncode = 0
        main(["status"])
    entries = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["args"] == ["status"]
    assert entries[0]["result"] == "allowed"
    assert "timestamp" in entries[0]


def test_denied_invocation_logged(tmp_path: Path) -> None:
    log_file = tmp_path / "vrg-git.log"
    with patch("vergil_tooling.bin.vrg_git._log_path", return_value=log_file):
        main(["commit"])
    entries = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]["args"] == ["commit"]
    assert entries[0]["result"] == "denied"


def test_log_directory_created(tmp_path: Path) -> None:
    log_file = tmp_path / "subdir" / "vrg-git.log"
    with (
        patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        patch("vergil_tooling.bin.vrg_git._log_path", return_value=log_file),
    ):
        mock_run.return_value.returncode = 0
        main(["status"])
    assert log_file.exists()


# -- subprocess passthrough ----------------------------------------------------


def test_subprocess_uses_shell_false() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        main(["status"])
    _, kwargs = mock_run.call_args
    assert kwargs.get("shell") is not True


def test_returns_subprocess_exit_code() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 128
        rc = main(["status"])
    assert rc == 128
