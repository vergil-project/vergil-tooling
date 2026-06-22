from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.identity import Identity

if TYPE_CHECKING:
    from pathlib import Path
from vergil_tooling.lib.vm_guest import (
    _inject_host_git_identity,
    _read_host_git_config,
    copy_claude_config,
    get_tooling_version,
    inject_credentials,
    install_tooling,
    link_claude_dirs,
    try_update_tooling,
    update_tooling,
)


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def _transport(stdout: str = "") -> MagicMock:
    """A Transport double whose ``run`` returns a successful CompletedProcess."""
    t = MagicMock()
    t.run.return_value = _ok(stdout)
    t.pipe.return_value = None
    return t


class TestInjectCredentials:
    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_injects_all_credentials(self, _mock_id: MagicMock, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")

        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(key_file),
        )

        transport = _transport()
        inject_credentials(transport, identity)

        assert transport.run.call_count == 3
        mkdir_call = transport.run.call_args_list[0]
        assert "mkdir" in " ".join(str(a) for a in mkdir_call[0])

        mode_bashrc_call = transport.run.call_args_list[1]
        assert "identity-mode" in " ".join(str(a) for a in mode_bashrc_call[0])

        git_call = transport.run.call_args_list[2]
        assert "git" in git_call[0]
        assert "insteadOf" in " ".join(str(a) for a in git_call[0])

        assert transport.pipe.call_count == 3
        pem_call = transport.pipe.call_args_list[0]
        assert "app.pem" in pem_call[0][0]
        assert "fakekey" in pem_call[0][1]

        env_call = transport.pipe.call_args_list[1]
        assert "app.env" in env_call[0][0]
        assert "APP_ID=12345" in env_call[0][1]

        mode_call = transport.pipe.call_args_list[2]
        assert "identity-mode" in mode_call[0][0]
        assert mode_call[0][1] == "user\n"

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_injects_claude_token(self, _mock_id: MagicMock, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        token_file = tmp_path / "claude-oauth-token"
        token_file.write_text("test-oauth-token-abc123\n")

        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(key_file),
            claude_token_path=str(token_file),
        )

        transport = _transport()
        inject_credentials(transport, identity)

        assert transport.run.call_count == 5
        bashrc_call = transport.run.call_args_list[3]
        assert "claude.env" in " ".join(str(a) for a in bashrc_call[0])
        mkdir_call = transport.run.call_args_list[4]
        assert "mkdir" in " ".join(str(a) for a in mkdir_call[0])
        assert ".claude" in " ".join(str(a) for a in mkdir_call[0])

        assert transport.pipe.call_count == 6
        claude_call = transport.pipe.call_args_list[3]
        assert "claude.env" in claude_call[0][0]
        assert "CLAUDE_CODE_OAUTH_TOKEN=test-oauth-token-abc123" in claude_call[0][1]
        creds_call = transport.pipe.call_args_list[4]
        assert ".credentials.json" in creds_call[0][0]
        assert "claudeAiOauth" in creds_call[0][1]
        assert "test-oauth-token-abc123" in creds_call[0][1]
        onboarding_call = transport.pipe.call_args_list[5]
        assert ".claude.json" in onboarding_call[0][0]
        assert "hasCompletedOnboarding" in onboarding_call[0][1]

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_skips_claude_token_when_not_configured(
        self, _mock_id: MagicMock, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")

        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(key_file),
        )

        transport = _transport()
        inject_credentials(transport, identity)

        assert transport.run.call_count == 3
        assert transport.pipe.call_count == 3

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_exits_if_claude_token_missing(self, _mock_id: MagicMock, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        bad_path = "/nonexistent/claude-token"  # noqa: S105

        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(key_file),
            claude_token_path=bad_path,
        )
        with pytest.raises(SystemExit):
            inject_credentials(_transport(), identity)

    def test_exits_if_key_missing(self) -> None:
        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path="/nonexistent/key.pem",
        )
        with pytest.raises(SystemExit):
            inject_credentials(_transport(), identity)

    def test_exits_if_mode_missing(self, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        identity = Identity(
            vm_instance="vergil-agent",
            app_id="12345",
            private_key_path=str(key_file),
        )
        with pytest.raises(SystemExit):
            inject_credentials(_transport(), identity)

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_injects_audit_mode(self, _mock_id: MagicMock, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        identity = Identity(
            vm_instance="vergil-audit",
            mode="audit",
            app_id="12345",
            private_key_path=str(key_file),
        )

        transport = _transport()
        inject_credentials(transport, identity)

        mode_call = transport.pipe.call_args_list[2]
        assert "identity-mode" in mode_call[0][0]
        assert mode_call[0][1] == "audit\n"

        mode_bashrc_call = transport.run.call_args_list[1]
        bashrc_cmd = " ".join(str(a) for a in mode_bashrc_call[0])
        assert "VRG_IDENTITY_MODE" in bashrc_cmd
        assert ".bashrc" in bashrc_cmd

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_calls_inject_host_git_identity(self, mock_id: MagicMock, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(key_file),
        )
        transport = _transport()
        inject_credentials(transport, identity)
        mock_id.assert_called_once_with(transport)

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_skips_all_injection_for_credential_less_identity(self, mock_id: MagicMock) -> None:
        # A credential-less identity has no key and no derivable mode — which
        # would normally abort. With auth_type="none" the whole stage is a
        # clean no-op: no key, no app.env, no mode file, no git identity, no
        # HTTPS rewrite, no Claude token.
        identity = Identity(vm_instance="anonymous", auth_type="none")

        transport = _transport()
        inject_credentials(transport, identity)

        transport.run.assert_not_called()
        transport.pipe.assert_not_called()
        mock_id.assert_not_called()


class TestReadHostGitConfig:
    @patch("vergil_tooling.lib.vm_guest.subprocess.run")
    def test_returns_value(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Phillip Moore\n"
        )
        assert _read_host_git_config("user.name") == "Phillip Moore"
        mock_run.assert_called_once_with(
            ["git", "config", "--global", "user.name"],
            capture_output=True,
            text=True,
            check=True,
        )

    @patch("vergil_tooling.lib.vm_guest.subprocess.run")
    def test_returns_none_on_missing_key(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        assert _read_host_git_config("user.name") is None

    @patch("vergil_tooling.lib.vm_guest.subprocess.run")
    def test_returns_none_when_git_not_found(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("git")
        assert _read_host_git_config("user.name") is None


class TestInjectHostGitIdentity:
    @patch("vergil_tooling.lib.vm_guest._read_host_git_config")
    def test_injects_name_and_email(self, mock_config: MagicMock) -> None:
        values = {
            "user.name": "Test User",
            "user.email": "test@example.com",
        }
        mock_config.side_effect = lambda k: values[k]
        transport = _transport()
        _inject_host_git_identity(transport)
        assert transport.run.call_count == 2
        name_call = transport.run.call_args_list[0]
        assert name_call[0] == (
            "git",
            "config",
            "--global",
            "user.name",
            "Test User",
        )
        email_call = transport.run.call_args_list[1]
        assert email_call[0] == (
            "git",
            "config",
            "--global",
            "user.email",
            "test@example.com",
        )

    @patch("vergil_tooling.lib.vm_guest._read_host_git_config")
    def test_skips_when_not_configured(self, mock_config: MagicMock) -> None:
        mock_config.return_value = None
        transport = _transport()
        _inject_host_git_identity(transport)
        transport.run.assert_not_called()

    @patch("vergil_tooling.lib.vm_guest._read_host_git_config")
    def test_injects_only_name_when_email_missing(self, mock_config: MagicMock) -> None:
        mock_config.side_effect = lambda k: "Test User" if k == "user.name" else None
        transport = _transport()
        _inject_host_git_identity(transport)
        assert transport.run.call_count == 1
        assert "user.name" in transport.run.call_args[0]


class TestInstallTooling:
    def test_installs_with_tag(self) -> None:
        transport = _transport()
        install_tooling(transport, "v2.0")
        assert transport.run.call_count == 2
        install_args = transport.run.call_args_list[0][0]
        cmd_str = " ".join(str(a) for a in install_args)
        assert "uv tool install" in cmd_str
        assert "v2.0" in cmd_str

    def test_creates_tag_dir_before_write(self) -> None:
        transport = _transport()
        install_tooling(transport, "v2.0")
        assert transport.run.call_count == 2
        mkdir_args = transport.run.call_args_list[1][0]
        cmd_str = " ".join(str(a) for a in mkdir_args)
        assert "mkdir -p" in cmd_str

    def test_writes_tag_marker(self) -> None:
        transport = _transport()
        install_tooling(transport, "v2.0")
        transport.pipe.assert_called_once()
        assert "tooling-tag" in transport.pipe.call_args[0][0]
        assert "v2.0" in transport.pipe.call_args[0][1]


class TestUpdateTooling:
    def test_updates_with_explicit_tag(self) -> None:
        transport = _transport()
        update_tooling(transport, "v2.0")
        transport.run.assert_called_once()
        cmd_str = " ".join(str(a) for a in transport.run.call_args[0])
        assert "uv tool install --reinstall" in cmd_str
        assert "v2.0" in cmd_str

    def test_reads_tag_from_marker(self) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok("v2.0\n"), _ok(), _ok()]
        update_tooling(transport)
        assert transport.run.call_count == 3
        cmd_str = " ".join(str(a) for a in transport.run.call_args_list[1][0])
        assert "uv tool install --reinstall" in cmd_str
        assert "v2.0" in cmd_str

    def test_explicit_tag_does_not_persist_marker(self) -> None:
        transport = _transport()
        update_tooling(transport, "v2.1")
        transport.pipe.assert_not_called()

    def test_resolved_tag_persists_marker(self) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok("v2.1\n"), _ok(), _ok()]
        update_tooling(transport)
        transport.pipe.assert_called_once()
        assert "tooling-tag" in transport.pipe.call_args[0][0]
        assert "v2.1" in transport.pipe.call_args[0][1]

    def test_fallback_tag_persists_marker(self) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok(), _ok(), _ok()]
        update_tooling(transport, fallback_tag="v2.1")
        transport.pipe.assert_called_once()
        assert "tooling-tag" in transport.pipe.call_args[0][0]
        assert "v2.1" in transport.pipe.call_args[0][1]

    def test_uses_fallback_when_no_marker(self) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok(), _ok(), _ok()]
        update_tooling(transport, fallback_tag="v2.0")
        assert transport.run.call_count == 3
        cmd_str = " ".join(str(a) for a in transport.run.call_args_list[1][0])
        assert "uv tool install --reinstall" in cmd_str
        assert "v2.0" in cmd_str

    def test_creates_tag_dir_before_write(self) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok("v2.0\n"), _ok(), _ok()]
        update_tooling(transport)
        mkdir_args = transport.run.call_args_list[2][0]
        cmd_str = " ".join(str(a) for a in mkdir_args)
        assert "mkdir -p" in cmd_str

    def test_exits_if_no_tag_and_no_fallback(self) -> None:
        transport = _transport()
        with pytest.raises(SystemExit):
            update_tooling(transport)


class TestGetToolingVersion:
    def test_parses_version_from_uv_tool_list(self) -> None:
        uv_output = "vergil-tooling v2.0.63\n    - vrg-commit\n"
        transport = _transport(uv_output)
        assert get_tooling_version(transport) == "v2.0.63"

    def test_returns_none_when_not_installed(self) -> None:
        transport = _transport("some-other-tool v1.0\n")
        assert get_tooling_version(transport) is None

    def test_returns_none_on_empty_output(self) -> None:
        transport = _transport("")
        assert get_tooling_version(transport) is None

    def test_returns_none_on_command_failure(self) -> None:
        transport = _transport()
        transport.run.side_effect = subprocess.CalledProcessError(1, "uv")
        assert get_tooling_version(transport) is None


class TestCopyClaudeConfig:
    def test_copies_existing_files(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# My prefs\n")
        (claude_dir / "settings.json").write_text('{"key": "val"}\n')

        transport = _transport()
        copy_claude_config(transport, claude_dir)

        assert transport.run.call_count == 1
        mkdir_call = transport.run.call_args_list[0]
        assert "mkdir" in " ".join(str(a) for a in mkdir_call[0])
        assert ".claude" in " ".join(str(a) for a in mkdir_call[0])

        assert transport.pipe.call_count == 2
        md_call = transport.pipe.call_args_list[0]
        assert "CLAUDE.md" in md_call[0][0]
        assert "# My prefs" in md_call[0][1]
        settings_call = transport.pipe.call_args_list[1]
        assert "settings.json" in settings_call[0][0]
        assert '"key"' in settings_call[0][1]

    def test_skips_missing_files(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        transport = _transport()
        copy_claude_config(transport, claude_dir)

        transport.run.assert_called_once()
        transport.pipe.assert_not_called()

    def test_skips_if_claude_dir_missing(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"

        transport = _transport()
        copy_claude_config(transport, claude_dir)

        transport.run.assert_not_called()
        transport.pipe.assert_not_called()


class TestLinkClaudeDirs:
    def test_links_projects_and_skills(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        transport = _transport()
        link_claude_dirs(transport, claude_dir)

        transport.run.assert_called_once()
        args = transport.run.call_args[0]
        assert args[0] == "bash"
        assert args[1] == "-c"
        script = args[2]
        assert "mkdir -p ~/.claude" in script
        for sub in ("projects", "skills"):
            assert f'"$HOME/.claude/{sub}"' in script
            assert str(claude_dir / sub) in script
        assert "ln -sfn" in script

    def test_does_not_link_sessions(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        transport = _transport()
        link_claude_dirs(transport, claude_dir)

        script = transport.run.call_args[0][2]
        # sessions must never be a symlink target (EXDEV breaks the roster write).
        assert f"ln -sfn {claude_dir / 'sessions'}" not in script
        assert str(claude_dir / "sessions") not in script

    def test_removes_existing_sessions_symlink(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        transport = _transport()
        link_claude_dirs(transport, claude_dir)

        script = transport.run.call_args[0][2]
        # A pre-existing sessions symlink (from #1296) is removed so Claude
        # recreates a real VM-local directory.
        assert '"$HOME/.claude/sessions"' in script
        assert 'if [ -L "$HOME/.claude/sessions" ]; then rm -f "$HOME/.claude/sessions"' in script

    def test_skips_if_claude_dir_missing(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"

        transport = _transport()
        link_claude_dirs(transport, claude_dir)

        transport.run.assert_not_called()


class TestTryUpdateTooling:
    @patch("vergil_tooling.lib.vm_guest.update_tooling")
    def test_returns_true_on_success(self, mock_update: MagicMock) -> None:
        transport = _transport()
        result = try_update_tooling(transport, fallback_tag="v2.0")
        assert result is True
        mock_update.assert_called_once_with(transport, None, fallback_tag="v2.0")

    @patch("vergil_tooling.lib.vm_guest.get_tooling_version", return_value="v2.0.63")
    @patch("vergil_tooling.lib.vm_guest.update_tooling")
    def test_returns_false_on_subprocess_error(
        self,
        mock_update: MagicMock,
        _mock_version: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_update.side_effect = subprocess.CalledProcessError(1, "uv")
        result = try_update_tooling(_transport(), fallback_tag="v2.0")
        assert result is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    @patch("vergil_tooling.lib.vm_guest.get_tooling_version", return_value="v2.0.63")
    @patch("vergil_tooling.lib.vm_guest.update_tooling")
    def test_returns_false_on_system_exit(
        self,
        mock_update: MagicMock,
        _mock_version: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_update.side_effect = SystemExit(1)
        result = try_update_tooling(_transport(), fallback_tag="v2.0")
        assert result is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    @patch("vergil_tooling.lib.vm_guest.get_tooling_version", return_value=None)
    @patch("vergil_tooling.lib.vm_guest.update_tooling")
    def test_raises_when_no_tooling_remains_after_failure(
        self,
        mock_update: MagicMock,
        _mock_version: MagicMock,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_update.side_effect = subprocess.CalledProcessError(1, "uv")
        with pytest.raises(SystemExit):
            try_update_tooling(_transport(), fallback_tag="v2.0")
        captured = capsys.readouterr()
        assert "no working tooling" in captured.err.lower()

    @patch("vergil_tooling.lib.vm_guest.update_tooling")
    def test_passes_explicit_tag(self, mock_update: MagicMock) -> None:
        transport = _transport()
        try_update_tooling(transport, tag="v2.1", fallback_tag="v2.0")
        mock_update.assert_called_once_with(transport, "v2.1", fallback_tag="v2.0")


class TestToolingInstallSelfHeal:
    """A corrupt uv cache must not permanently brick the install: on failure
    the VM's uv cache is cleared and the install is retried once."""

    @staticmethod
    def _cmds(transport: MagicMock) -> list[str]:
        return [" ".join(str(a) for a in call[0]) for call in transport.run.call_args_list]

    def test_update_clears_cache_and_retries_on_failure(self) -> None:
        transport = _transport()
        transport.run.side_effect = [subprocess.CalledProcessError(1, "uv"), _ok(), _ok()]
        update_tooling(transport, "v2.0")
        cmds = self._cmds(transport)
        assert any("uv cache clean" in c for c in cmds)
        assert sum("uv tool install" in c for c in cmds) == 2

    def test_retry_forces_over_orphaned_executables(self) -> None:
        # A poisoned cache + invalid receipt leaves orphaned `vrg-*` executables
        # in ~/.local/bin. Clearing the cache fixes the wheel, but the retry then
        # dies on "Executable already exists" unless it forces. So the retry must
        # escalate to --force; the first attempt must not (a healthy version bump
        # should not force-replace entry points).
        transport = _transport()
        transport.run.side_effect = [subprocess.CalledProcessError(1, "uv"), _ok(), _ok()]
        update_tooling(transport, "v2.0")
        installs = [c for c in self._cmds(transport) if "uv tool install" in c]
        assert len(installs) == 2
        first_attempt, retry = installs
        assert "--force" not in first_attempt
        assert "--force" in retry

    def test_update_propagates_when_retry_also_fails(self) -> None:
        transport = _transport()
        transport.run.side_effect = [
            subprocess.CalledProcessError(1, "uv"),
            _ok(),
            subprocess.CalledProcessError(1, "uv"),
        ]
        with pytest.raises(subprocess.CalledProcessError):
            update_tooling(transport, "v2.0")

    def test_install_clears_cache_and_retries_on_failure(self) -> None:
        transport = _transport()
        transport.run.side_effect = [
            subprocess.CalledProcessError(1, "uv"),
            _ok(),
            _ok(),
            _ok(),
        ]
        install_tooling(transport, "v2.0")
        cmds = self._cmds(transport)
        assert any("uv cache clean" in c for c in cmds)
        assert sum("uv tool install" in c for c in cmds) == 2
