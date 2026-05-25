"""Tests for vergil_tooling.bin.vrg_hook_guard."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from vergil_tooling.bin.vrg_hook_guard import _find_vergil_toml, main


def _make_hook_input(command: str, cwd: str = "/repo") -> str:
    return json.dumps(
        {
            "session_id": "test",
            "cwd": cwd,
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
        }
    )


def _run(command: str, *, cwd: str = "/repo", managed: bool = True) -> tuple[int, str]:
    hook_input = _make_hook_input(command, cwd=cwd)
    toml_path = Path(cwd) / "vergil.toml"
    with (
        patch("sys.stdin", StringIO(hook_input)),
        patch("vergil_tooling.bin.vrg_hook_guard.Path.exists", return_value=managed),
        patch(
            "vergil_tooling.bin.vrg_hook_guard._find_vergil_toml",
            return_value=toml_path if managed else None,
        ),
    ):
        buf = StringIO()
        with patch("sys.stdout", buf):
            rc = main()
    return rc, buf.getvalue()


# -- managed-repo gating ------------------------------------------------------


class TestManagedRepoGating:
    def test_unmanaged_repo_allows_everything(self) -> None:
        rc, out = _run("git commit -m 'test'", managed=False)
        assert rc == 0
        assert out == ""

    def test_managed_repo_blocks_raw_git(self) -> None:
        rc, out = _run("git commit -m 'test'")
        assert rc == 0
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- raw git detection --------------------------------------------------------


class TestRawGitDetection:
    def test_direct_git_commit(self) -> None:
        rc, out = _run("git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "vrg-git" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_direct_git_push(self) -> None:
        rc, out = _run("git push origin main")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_direct_git_reset(self) -> None:
        rc, out = _run("git reset --hard HEAD")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_direct_git_status(self) -> None:
        rc, out = _run("git status")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_chained_git_commit(self) -> None:
        rc, out = _run("cd /path && git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_sequenced_git_commit(self) -> None:
        rc, out = _run("git add . ; git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_piped_git_commit(self) -> None:
        rc, out = _run("echo y | git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_or_chained_git(self) -> None:
        rc, out = _run("false || git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_env_var_prefix(self) -> None:
        rc, out = _run("VRG_COMMIT_CONTEXT=1 git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_multiple_env_vars(self) -> None:
        rc, out = _run("FOO=bar BAZ=1 git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_env_command_wrapper(self) -> None:
        rc, out = _run("env git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_command_wrapper(self) -> None:
        rc, out = _run("command git commit -m 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_bash_c_subshell(self) -> None:
        rc, out = _run('bash -c "git commit -m test"')
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_sh_c_subshell(self) -> None:
        rc, out = _run('sh -c "git commit -m test"')
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_parenthesized(self) -> None:
        rc, out = _run("(git reset --hard)")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_dollar_paren(self) -> None:
        rc, out = _run("echo $(git log --oneline)")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_backtick(self) -> None:
        rc, out = _run("echo `git log --oneline`")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- raw gh detection ---------------------------------------------------------


class TestRawGhDetection:
    def test_direct_gh_pr_create(self) -> None:
        rc, out = _run("gh pr create --title 'test'")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "vrg-gh" in result["hookSpecificOutput"]["permissionDecisionReason"]

    def test_direct_gh_api(self) -> None:
        rc, out = _run("gh api repos/foo/bar")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_chained_gh(self) -> None:
        rc, out = _run("cd /repo && gh pr create")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_dollar_paren_gh(self) -> None:
        rc, out = _run("echo $(gh api repos/foo/bar)")
        result = json.loads(out)
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"


# -- vrg-git and vrg-gh must NOT match ----------------------------------------


class TestWrapperExclusion:
    def test_vrg_git_allowed(self) -> None:
        rc, out = _run("vrg-git commit -m 'test'")
        assert rc == 0
        assert out == ""

    def test_vrg_gh_allowed(self) -> None:
        rc, out = _run("vrg-gh pr create --title 'test'")
        assert rc == 0
        assert out == ""

    def test_vrg_git_status_allowed(self) -> None:
        rc, out = _run("vrg-git status")
        assert rc == 0
        assert out == ""

    def test_vrg_git_with_path_allowed(self) -> None:
        rc, out = _run("/usr/local/bin/vrg-git log")
        assert rc == 0
        assert out == ""

    def test_chained_vrg_git_allowed(self) -> None:
        rc, out = _run("cd /path && vrg-git push origin main")
        assert rc == 0
        assert out == ""


# -- non-git/gh commands must be allowed --------------------------------------


class TestNonGitCommands:
    def test_ls(self) -> None:
        rc, out = _run("ls -la")
        assert rc == 0
        assert out == ""

    def test_python(self) -> None:
        rc, out = _run("python script.py")
        assert rc == 0
        assert out == ""

    def test_grep_for_git(self) -> None:
        rc, out = _run("grep -r 'git' src/")
        assert rc == 0
        assert out == ""

    def test_empty_command(self) -> None:
        rc, out = _run("")
        assert rc == 0
        assert out == ""


# -- edge cases and error handling --------------------------------------------


class TestEdgeCases:
    def test_missing_tool_input(self) -> None:
        hook_input = json.dumps({"cwd": "/repo", "hook_event_name": "PreToolUse"})
        toml_path = Path("/repo/vergil.toml")
        with (
            patch("sys.stdin", StringIO(hook_input)),
            patch("vergil_tooling.bin.vrg_hook_guard._find_vergil_toml", return_value=toml_path),
        ):
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main()
        assert rc == 0
        assert buf.getvalue() == ""

    def test_missing_command_in_tool_input(self) -> None:
        hook_input = json.dumps(
            {
                "cwd": "/repo",
                "hook_event_name": "PreToolUse",
                "tool_input": {},
            }
        )
        toml_path = Path("/repo/vergil.toml")
        with (
            patch("sys.stdin", StringIO(hook_input)),
            patch("vergil_tooling.bin.vrg_hook_guard._find_vergil_toml", return_value=toml_path),
        ):
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main()
        assert rc == 0
        assert buf.getvalue() == ""

    def test_invalid_json_exits_zero(self) -> None:
        with patch("sys.stdin", StringIO("not json")):
            buf = StringIO()
            with patch("sys.stdout", buf):
                rc = main()
        assert rc == 0
        assert buf.getvalue() == ""


# -- _find_vergil_toml --------------------------------------------------------


class TestFindVergilToml:
    def test_finds_in_current_dir(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text("[project]\n")
        assert _find_vergil_toml(tmp_path) == tmp_path / "vergil.toml"

    def test_finds_in_parent_dir(self, tmp_path: Path) -> None:
        (tmp_path / "vergil.toml").write_text("[project]\n")
        child = tmp_path / "src" / "pkg"
        child.mkdir(parents=True)
        assert _find_vergil_toml(child) == tmp_path / "vergil.toml"

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        child = tmp_path / "some" / "deep" / "path"
        child.mkdir(parents=True)
        assert _find_vergil_toml(child) is None
