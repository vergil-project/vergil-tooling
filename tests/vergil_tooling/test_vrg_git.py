"""Tests for vergil_tooling.bin.vrg_git."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_git import (
    _noninteractive_rebase_env,
    _parse_branch_target,
    _worktree_convention_active,
    main,
)

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
    "annotate",
    "blame",
    "cat-file",
    "cherry",
    "count-objects",
    "describe",
    "diff-files",
    "diff-index",
    "diff-tree",
    "for-each-ref",
    "grep",
    "ls-files",
    "ls-tree",
    "merge-base",
    "name-rev",
    "rev-list",
    "shortlog",
    "show-branch",
    "show-ref",
    "var",
    "verify-commit",
    "verify-tag",
    "whatchanged",
    "add",
    "mv",
    "rm",
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
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", subcmd], returncode=0, stdout="", stderr=""
        )
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


# -- config is denied (no exact-match exceptions) ----------------------------


def test_config_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


def test_config_with_args_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "user.email", "x@example.com"]) != 0
    assert "denied" in capsys.readouterr().err.lower()


# -- reflog: read-only forms allowed, mutating sub-ops denied -----------------


def test_reflog_bare_allowed() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["reflog"])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args == ["git", "reflog"]


@pytest.mark.parametrize("sub", ["show", "exists"])
def test_reflog_readonly_subcommand_allowed(sub: str) -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["reflog", sub, "HEAD"])
    assert rc == 0
    args = mock_run.call_args[0][0]
    assert args[:3] == ["git", "reflog", sub]


@pytest.mark.parametrize("sub", ["expire", "delete"])
def test_reflog_mutating_subcommand_denied(sub: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["reflog", sub, "--all"]) != 0
    err = capsys.readouterr().err
    assert sub in err
    assert "denied" in err.lower()


def test_reflog_flag_before_subcommand_allowed() -> None:
    """A flag in the first position is read-only (e.g. `git reflog --all`)."""
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        rc = main(["reflog", "--all"])
    assert rc == 0


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
    vv_output = "  feature/123-foo abc1234 [origin/feature/123-foo] some commit\n"
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


def test_is_upstream_gone_skips_empty_lines() -> None:
    vv_output = "\n  feature/123-foo abc1234 [origin/feature/123-foo: gone] old\n\n"
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=vv_output,
        )
        from vergil_tooling.bin.vrg_git import _is_upstream_gone

        assert _is_upstream_gone("feature/123-foo") is True


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


# -- helper: _upstream_is_integration_branch ----------------------------------


def _mock_upstream(upstream: str | None) -> subprocess.CompletedProcess[str]:
    if upstream is None:
        return subprocess.CompletedProcess(args=[], returncode=128, stdout="", stderr="fatal")
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=f"{upstream}\n")


def test_upstream_is_integration_branch_develop() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = _mock_upstream("origin/develop")
        from vergil_tooling.bin.vrg_git import _upstream_is_integration_branch

        assert _upstream_is_integration_branch("chore/417-bump-2.0.17") is True


def test_upstream_is_integration_branch_main() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = _mock_upstream("origin/main")
        from vergil_tooling.bin.vrg_git import _upstream_is_integration_branch

        assert _upstream_is_integration_branch("feature/123-foo") is True


def test_upstream_is_integration_branch_release() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = _mock_upstream("origin/release/2.1")
        from vergil_tooling.bin.vrg_git import _upstream_is_integration_branch

        assert _upstream_is_integration_branch("feature/123-foo") is True


def test_upstream_is_integration_branch_feature_upstream() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = _mock_upstream("origin/feature/123-foo")
        from vergil_tooling.bin.vrg_git import _upstream_is_integration_branch

        assert _upstream_is_integration_branch("feature/123-foo") is False


def test_upstream_is_integration_branch_no_upstream() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = _mock_upstream(None)
        from vergil_tooling.bin.vrg_git import _upstream_is_integration_branch

        assert _upstream_is_integration_branch("feature/123-foo") is False


# -- flag deny lists ----------------------------------------------------------


def test_branch_force_delete_allowed_when_upstream_gone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        patch(
            "vergil_tooling.bin.vrg_git._is_upstream_gone",
            return_value=True,
        ),
    ):
        mock_run.return_value.returncode = 0
        rc = main(["branch", "-D", "feature/123-foo"])
    assert rc == 0


def test_branch_force_delete_denied_when_upstream_active(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_git._is_upstream_gone",
            return_value=False,
        ),
        patch(
            "vergil_tooling.bin.vrg_git._upstream_is_integration_branch",
            return_value=False,
        ),
    ):
        rc = main(["branch", "-D", "feature/123-foo"])
    assert rc != 0
    assert "denied" in capsys.readouterr().err.lower()


def test_branch_force_delete_allowed_when_tracking_integration_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        patch(
            "vergil_tooling.bin.vrg_git._is_upstream_gone",
            return_value=False,
        ),
        patch(
            "vergil_tooling.bin.vrg_git._upstream_is_integration_branch",
            return_value=True,
        ),
    ):
        mock_run.return_value.returncode = 0
        rc = main(["branch", "-D", "chore/417-bump-2.0.17"])
    assert rc == 0


def test_branch_force_delete_denied_for_protected_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_git._is_upstream_gone",
            return_value=True,
        ),
        patch(
            "vergil_tooling.bin.vrg_git._upstream_is_integration_branch",
            return_value=True,
        ),
    ):
        rc = main(["branch", "-D", "develop"])
    assert rc != 0
    assert "protected" in capsys.readouterr().err.lower()


def test_branch_force_delete_denied_for_release_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_git._is_upstream_gone",
            return_value=True,
        ),
        patch(
            "vergil_tooling.bin.vrg_git._upstream_is_integration_branch",
            return_value=True,
        ),
    ):
        rc = main(["branch", "-D", "release/2.1"])
    assert rc != 0
    assert "protected" in capsys.readouterr().err.lower()


def test_branch_force_delete_denied_no_branch_name(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["branch", "-D"])
    assert rc != 0
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
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
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
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "push"], returncode=0, stdout="", stderr=""
        )
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


# -- remote token injection ---------------------------------------------------


class TestRemoteTokenInjection:
    @pytest.mark.parametrize("subcmd", ["push", "pull", "fetch", "ls-remote"])
    def test_injects_token_for_remote_commands(self, subcmd: str) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_git.github.get_installation_token",
                return_value="ghs_token_123",
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", subcmd], returncode=0, stdout="", stderr=""
            )
            rc = main([subcmd, "origin", "main"])
        assert rc == 0
        _, kwargs = mock_run.call_args
        env = kwargs["env"]
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraHeader"
        assert "Authorization: Basic" in env["GIT_CONFIG_VALUE_0"]

    @pytest.mark.parametrize("subcmd", ["status", "log", "diff", "add", "branch"])
    def test_no_injection_for_local_commands(self, subcmd: str) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_git.github.get_installation_token",
                return_value="ghs_token_123",
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            main([subcmd])
        _, kwargs = mock_run.call_args
        assert "env" not in kwargs or kwargs.get("env") is None

    def test_no_injection_when_no_app_token(self) -> None:
        with (
            patch(
                "vergil_tooling.bin.vrg_git.github.get_installation_token",
                return_value=None,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"], returncode=0, stdout="", stderr=""
            )
            main(["push", "origin", "main"])
        _, kwargs = mock_run.call_args
        assert "env" not in kwargs or kwargs.get("env") is None

    def test_token_encodes_as_basic_auth(self) -> None:
        import base64

        with (
            patch(
                "vergil_tooling.bin.vrg_git.github.get_installation_token",
                return_value="ghs_test_token",
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"], returncode=0, stdout="", stderr=""
            )
            main(["push", "origin", "main"])
        _, kwargs = mock_run.call_args
        header_value = kwargs["env"]["GIT_CONFIG_VALUE_0"]
        expected = base64.b64encode(b"x-access-token:ghs_test_token").decode()
        assert expected in header_value


# -- push workflow error detection --------------------------------------------


class TestPushWorkflowErrorDetection:
    """vrg-git push detects workflow permission errors and provides guidance."""

    _WORKFLOW_ERR = (
        "refusing to allow a GitHub App to create or update workflow "
        "`.github/workflows/ci.yml` without `workflows` permission"
    )

    def test_workflow_error_detected_and_guidance_printed(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=1,
                stdout="",
                stderr=self._WORKFLOW_ERR,
            )
            rc = main(["push", "origin", "feature/x"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "workflow" in err.lower()
        assert "escalate" in err.lower()
        assert "human maintainer" in err.lower()

    def test_workflow_error_shows_original_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=1,
                stdout="",
                stderr=self._WORKFLOW_ERR,
            )
            main(["push", "origin", "feature/x"])
        err = capsys.readouterr().err
        assert "refusing to allow" in err

    def test_non_workflow_push_error_passes_through(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=1,
                stdout="",
                stderr="fatal: remote rejected\n",
            )
            rc = main(["push", "origin", "feature/x"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "fatal: remote rejected" in err
        assert "escalate" not in err.lower()

    def test_successful_push_unchanged(self) -> None:
        with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["git", "push"],
                returncode=0,
                stdout="Everything up-to-date\n",
                stderr="",
            )
            rc = main(["push", "origin", "feature/x"])
        assert rc == 0


# -- worktree convention -------------------------------------------------------


class TestParseBranchTarget:
    def test_checkout_with_flag_skips_flag(self) -> None:
        assert _parse_branch_target("checkout", ["-b", "feature/x"]) == "feature/x"

    def test_checkout_no_args(self) -> None:
        assert _parse_branch_target("checkout", []) is None

    def test_switch_with_flag_skips_flag(self) -> None:
        assert _parse_branch_target("switch", ["--detach", "main"]) == "main"

    def test_switch_no_args(self) -> None:
        assert _parse_branch_target("switch", []) is None

    def test_unknown_subcmd_returns_none(self) -> None:
        assert _parse_branch_target("merge", ["feature/x"]) is None


def test_worktree_convention_active_without_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _worktree_convention_active() is False


def test_worktree_convention_active_with_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".worktrees").mkdir()
    monkeypatch.chdir(tmp_path)
    assert _worktree_convention_active() is True


class TestWorktreeConvention:
    """Branch switches in the main worktree are blocked when .worktrees/ exists."""

    def test_checkout_feature_denied_in_main_worktree(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
        ):
            rc = main(["checkout", "feature/123-foo"])
        assert rc != 0
        assert "worktree" in capsys.readouterr().err.lower()

    def test_checkout_develop_allowed_in_main_worktree(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            rc = main(["checkout", "develop"])
        assert rc == 0

    def test_checkout_main_allowed_in_main_worktree(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            rc = main(["checkout", "main"])
        assert rc == 0

    def test_checkout_file_allowed_in_main_worktree(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            rc = main(["checkout", "--", "src/file.py"])
        assert rc == 0

    def test_checkout_feature_allowed_in_secondary_worktree(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=False),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            rc = main(["checkout", "feature/123-foo"])
        assert rc == 0

    def test_checkout_feature_allowed_without_worktrees_dir(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=False,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            rc = main(["checkout", "feature/123-foo"])
        assert rc == 0

    def test_switch_feature_denied_in_main_worktree(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
        ):
            rc = main(["switch", "feature/123-foo"])
        assert rc != 0
        assert "worktree" in capsys.readouterr().err.lower()

    def test_switch_create_denied_in_main_worktree(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
        ):
            rc = main(["switch", "-c", "feature/456-bar"])
        assert rc != 0
        assert "worktree" in capsys.readouterr().err.lower()

    def test_switch_develop_allowed_in_main_worktree(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            rc = main(["switch", "develop"])
        assert rc == 0

    def test_switch_feature_allowed_in_secondary_worktree(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=False),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=True,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            rc = main(["switch", "feature/123-foo"])
        assert rc == 0

    def test_switch_feature_allowed_without_worktrees_dir(self) -> None:
        with (
            patch("vergil_tooling.bin.vrg_git._is_main_worktree", return_value=True),
            patch(
                "vergil_tooling.bin.vrg_git._worktree_convention_active",
                return_value=False,
            ),
            patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run,
        ):
            mock_run.return_value.returncode = 0
            rc = main(["switch", "feature/123-foo"])
        assert rc == 0


# -- non-interactive rebase (#1742) -------------------------------------------


def test_noninteractive_rebase_env_from_none_uses_os_environ() -> None:
    with patch.dict("vergil_tooling.bin.vrg_git.os.environ", {"FOO": "bar"}, clear=True):
        env = _noninteractive_rebase_env(None)
    assert env["FOO"] == "bar"
    assert env["GIT_SEQUENCE_EDITOR"] == "true"
    assert env["GIT_EDITOR"] == "true"


def test_noninteractive_rebase_env_preserves_base_env() -> None:
    base = {"GIT_CONFIG_COUNT": "1", "FOO": "bar"}
    env = _noninteractive_rebase_env(base)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["FOO"] == "bar"
    assert env["GIT_SEQUENCE_EDITOR"] == "true"
    assert env["GIT_EDITOR"] == "true"
    # The caller's dict must not be mutated.
    assert "GIT_SEQUENCE_EDITOR" not in base


def test_rebase_forces_noninteractive_editors() -> None:
    with patch("vergil_tooling.bin.vrg_git.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "rebase"], returncode=0, stdout="", stderr=""
        )
        rc = main(["rebase", "origin/develop"])
    assert rc == 0
    env = mock_run.call_args.kwargs["env"]
    assert env["GIT_SEQUENCE_EDITOR"] == "true"
    assert env["GIT_EDITOR"] == "true"


def test_rebase_interactive_flag_still_denied(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rebase", "-i", "origin/develop"]) != 0
    assert "denied" in capsys.readouterr().err.lower()
