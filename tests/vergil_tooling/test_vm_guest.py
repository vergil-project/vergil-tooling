from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.identity import Identity
from vergil_tooling.lib.vm_guest import (
    VmUnreachableError,
    _inject_host_git_identity,
    _read_host_git_config,
    copy_claude_config,
    get_tooling_version,
    inject_credentials,
    install_tooling,
    link_claude_dirs,
    read_fingerprint,
    update_plugins,
    update_tooling,
    vm_occupancy,
    vm_probe,
    vm_spec_status,
)

if TYPE_CHECKING:
    from pathlib import Path


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


class TestFingerprintHelpers:
    def test_read_fingerprint_returns_stamped_value(self) -> None:
        transport = _transport("abc123\n")
        assert read_fingerprint(transport) == "abc123"

    def test_read_fingerprint_missing_marker_is_none(self) -> None:
        # The in-guest read is masked (`cat ... 2>/dev/null || true`), so an absent
        # marker is empty stdout from a zero exit — never a non-zero exit. That is
        # what distinguishes "marker gone" (drift) from "VM unreachable" (transport).
        transport = _transport("")
        assert read_fingerprint(transport) is None

    def test_read_fingerprint_empty_marker_is_none(self) -> None:
        transport = _transport("\n")
        assert read_fingerprint(transport) is None

    def test_read_fingerprint_transport_failure_raises_unreachable(self) -> None:
        # The shell round-trip itself failing (SSH refused) is a transport failure,
        # NOT an absent marker — it must surface as VmUnreachableError rather than
        # collapse into None (which would be misread as drift).
        transport = _transport()
        transport.run.side_effect = subprocess.CalledProcessError(255, "limactl")
        with pytest.raises(VmUnreachableError):
            read_fingerprint(transport)

    @patch("vergil_tooling.lib.vm_guest.read_fingerprint")
    def test_vm_spec_status_ok_on_match(self, mock_read: MagicMock) -> None:
        mock_read.return_value = "abc123"
        assert vm_spec_status(_transport(), "abc123") == "ok"

    @patch("vergil_tooling.lib.vm_guest.read_fingerprint")
    def test_vm_spec_status_needs_rebuild_on_drift(self, mock_read: MagicMock) -> None:
        mock_read.return_value = "old"
        assert vm_spec_status(_transport(), "new") == "needs-rebuild"

    @patch("vergil_tooling.lib.vm_guest.read_fingerprint")
    def test_vm_spec_status_needs_rebuild_on_missing_marker(self, mock_read: MagicMock) -> None:
        # A reachable VM whose marker is genuinely absent is still drift.
        mock_read.return_value = None
        assert vm_spec_status(_transport(), "abc123") == "needs-rebuild"

    @patch("vergil_tooling.lib.vm_guest.read_fingerprint")
    def test_vm_spec_status_unreachable_on_transport_failure(self, mock_read: MagicMock) -> None:
        # An unreachable VM is not a drifted VM: the third state keeps callers from
        # telling the user to rebuild when the real problem is reachability.
        mock_read.side_effect = VmUnreachableError
        assert vm_spec_status(_transport(), "abc123") == "unreachable"


class TestOccupancy:
    def test_parses_agents_and_humans(self) -> None:
        transport = _transport("agents=2 humans=1\n")
        assert vm_occupancy(transport) == (2, 1)

    def test_zero_when_idle(self) -> None:
        transport = _transport("agents=0 humans=0\n")
        assert vm_occupancy(transport) == (0, 0)

    def test_unparseable_output_is_zeros(self) -> None:
        transport = _transport("garbage\n")
        assert vm_occupancy(transport) == (0, 0)

    def test_exec_failure_is_zeros(self) -> None:
        transport = _transport()
        transport.run.side_effect = subprocess.CalledProcessError(1, "limactl")
        assert vm_occupancy(transport) == (0, 0)


class TestVmProbe:
    def test_occupancy_only_skips_fingerprint(self) -> None:
        transport = _transport("agents=2 humans=1\n")
        assert vm_probe(transport) == (2, 1, None)
        assert transport.run.call_count == 1
        script = transport.run.call_args[0][2]
        assert "vm-spec.fingerprint" not in script

    def test_fingerprint_combined_in_single_invocation(self) -> None:
        transport = _transport("agents=2 humans=1\nfingerprint=abc123\n")
        assert vm_probe(transport, fingerprint=True) == (2, 1, "abc123")
        assert transport.run.call_count == 1
        script = transport.run.call_args[0][2]
        assert "vm-spec.fingerprint" in script

    def test_missing_fingerprint_is_none(self) -> None:
        transport = _transport("agents=0 humans=0\nfingerprint=\n")
        assert vm_probe(transport, fingerprint=True) == (0, 0, None)

    def test_absent_fingerprint_line_is_none(self) -> None:
        transport = _transport("agents=1 humans=0\n")
        assert vm_probe(transport, fingerprint=True) == (1, 0, None)

    def test_unparseable_occupancy_is_zeros(self) -> None:
        transport = _transport("garbage\nfingerprint=abc123\n")
        assert vm_probe(transport, fingerprint=True) == (0, 0, "abc123")

    def test_exec_failure_is_zeros_and_none(self) -> None:
        transport = _transport()
        transport.run.side_effect = subprocess.CalledProcessError(1, "limactl")
        assert vm_probe(transport, fingerprint=True) == (0, 0, None)


class TestUpdatePlugins:
    # update_plugins is transport-generic (#1812): the same refresh drives a Lima
    # box over limactl or an off-platform box over IAP — only the transport differs.
    _LISTING = json.dumps(
        [
            {"id": "paad@paad", "scope": "user", "enabled": True},
            {"id": "frontend-design@official", "scope": "user", "enabled": False},
            {"id": "vergil@vergil-marketplace", "scope": "project", "enabled": True},
        ]
    )

    def _fake_transport(self) -> MagicMock:
        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            out = self._LISTING if "plugin list --json" in cmd else ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        return transport

    def test_refreshes_marketplaces_then_updates_enabled_plugins(self) -> None:
        transport = self._fake_transport()
        update_plugins(transport)
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        # Marketplace metadata refreshed first, then the installed list is read.
        assert any("claude plugin marketplace update" in c for c in cmds)
        assert any("claude plugin list --json" in c for c in cmds)
        # Each ENABLED plugin updated with its own scope; no bulk update exists.
        assert any("claude plugin update paad@paad --scope user" in c for c in cmds)
        assert any(
            "claude plugin update vergil@vergil-marketplace --scope project" in c for c in cmds
        )
        # Disabled plugins are left alone.
        assert not any("frontend-design" in c for c in cmds)
        # Every call is a non-login bash -c with an explicit PATH export, so claude
        # resolves regardless of the guest's zsh-configured interactive environment.
        for c in transport.run.call_args_list:
            assert c.args[:2] == ("bash", "-c")
            assert "export PATH=" in c.args[-1]

    def test_raises_after_attempting_all_when_a_plugin_fails(self) -> None:
        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "claude plugin update paad@paad" in cmd:
                raise subprocess.CalledProcessError(1, "claude plugin update")
            out = self._LISTING if "plugin list --json" in cmd else ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        with pytest.raises(RuntimeError, match="paad@paad"):
            update_plugins(transport)
        # Best-effort: the other enabled plugin is still attempted before raising.
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert any("claude plugin update vergil@vergil-marketplace" in c for c in cmds)

    def test_installs_enabled_plugins_not_yet_installed(self) -> None:
        # #2006 (Fix C): a plugin enabled in the guest settings.json but absent from
        # `claude plugin list` (never installed) must be INSTALLED, not skipped.
        # Post-v2.1.195 `enabledPlugins` no longer auto-installs, so an update-only
        # pass leaves a fresh box with zero plugins.
        settings = json.dumps({"enabledPlugins": {"superpowers@official": True, "paad@paad": True}})
        # superpowers is enabled-but-not-installed; paad is already installed.
        listing = json.dumps([{"id": "paad@paad", "scope": "user", "enabled": True}])

        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd:
                out = settings
            elif "plugin list --json" in cmd:
                out = listing
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        # The enabled-but-uninstalled plugin is installed...
        assert any("claude plugin install superpowers@official --scope user" in c for c in cmds)
        # ...and the already-installed enabled plugin is updated, not reinstalled.
        assert any("claude plugin update paad@paad --scope user" in c for c in cmds)
        assert not any("claude plugin install paad@paad" in c for c in cmds)

    def test_unreadable_settings_still_refreshes_installed(self) -> None:
        # #2006: if the guest settings.json cannot be read (cat fails), the desired
        # set is empty but anything already installed-and-enabled is still refreshed.
        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd:
                raise subprocess.CalledProcessError(1, "cat")
            out = self._LISTING if "plugin list --json" in cmd else ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)  # no raise
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert any("claude plugin update paad@paad --scope user" in c for c in cmds)

    def test_malformed_settings_is_tolerated(self) -> None:
        # #2006: garbage in settings.json must not crash the reconcile; the
        # installed-enabled set is still refreshed.
        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd:
                out = "{not valid json"
            elif "plugin list --json" in cmd:
                out = self._LISTING
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)  # no raise
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert any(
            "claude plugin update vergil@vergil-marketplace --scope project" in c for c in cmds
        )

    def test_registers_declared_marketplaces_before_install(self) -> None:
        # #2021 (Fix C v2): headless `marketplace update`/`list` do NOT register the
        # marketplaces declared in settings.json extraKnownMarketplaces — only
        # `marketplace add <source>` does. Without it, install fails "not found in
        # marketplace, local copy out of date" on a fresh box.
        settings = json.dumps(
            {
                "extraKnownMarketplaces": {
                    "vergil-marketplace": {
                        "source": {
                            "source": "github",
                            "repo": "vergil-project/vergil-claude-plugin",
                        }
                    },
                    "paad": {"source": {"source": "github", "repo": "Ovid/paad"}},
                },
                "enabledPlugins": {"vergil@vergil-marketplace": True},
            }
        )

        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd:
                out = settings
            elif "plugin list --json" in cmd:
                out = "[]"
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)
        cmds = [c.args[-1] for c in transport.run.call_args_list]

        def find(sub: str) -> int:
            return next((i for i, c in enumerate(cmds) if sub in c), -1)

        # Each declared marketplace is registered by source...
        assert find("marketplace add vergil-project/vergil-claude-plugin") >= 0
        assert find("marketplace add Ovid/paad") >= 0
        # ...before the enabled plugin is installed.
        assert find("plugin install vergil@vergil-marketplace") >= 0
        assert find("marketplace add vergil-project/vergil-claude-plugin") < find(
            "plugin install vergil@vergil-marketplace"
        )

    def test_marketplace_sources_handles_url_and_path_and_skips_sourceless(self) -> None:
        # #2021: non-github sources (url, path) are registered; entries with no
        # usable source are skipped, not crashed on.
        settings = json.dumps(
            {
                "extraKnownMarketplaces": {
                    "u": {"source": {"source": "git", "url": "https://example.com/y.git"}},
                    "p": {"source": {"path": "./local-mp"}},
                    "empty": {"source": {}},
                    "nosrc": {},
                },
                "enabledPlugins": {},
            }
        )

        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd:
                out = settings
            elif "plugin list --json" in cmd:
                out = "[]"
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)  # no raise (nothing enabled)
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert any("marketplace add https://example.com/y.git" in c for c in cmds)
        assert any("marketplace add ./local-mp" in c for c in cmds)
        # sourceless entries produced no add command
        assert sum("marketplace add" in c for c in cmds) == 2

    def test_marketplace_add_failure_is_surfaced(self) -> None:
        # #2021: a marketplace that fails to register is collected and surfaced,
        # never silently skipped.
        settings = json.dumps(
            {
                "extraKnownMarketplaces": {
                    "bad": {"source": {"source": "github", "repo": "x/bad"}}
                },
                "enabledPlugins": {},
            }
        )

        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "marketplace add x/bad" in cmd:
                raise subprocess.CalledProcessError(1, "claude plugin marketplace add")
            if "cat ~/.claude/settings.json" in cmd:
                out = settings
            elif "plugin list --json" in cmd:
                out = "[]"
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        with pytest.raises(RuntimeError, match="marketplace:x/bad"):
            update_plugins(transport)

    def test_derives_official_marketplace_when_undeclared(self) -> None:
        # #2029: an enabled plugin can reference a marketplace not in
        # extraKnownMarketplaces — notably claude-plugins-official, which isn't
        # always declared. Derive its source so add+install still work.
        settings = json.dumps(
            {
                "extraKnownMarketplaces": {},
                "enabledPlugins": {"superpowers@claude-plugins-official": True},
            }
        )

        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd:
                out = settings
            elif "plugin list --json" in cmd:
                out = "[]"
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert any("marketplace add anthropics/claude-plugins-official" in c for c in cmds)
        assert any(
            "plugin install superpowers@claude-plugins-official --scope user" in c for c in cmds
        )

    def test_undecidable_undeclared_marketplace_is_not_added(self) -> None:
        # #2029: a plugin against an unknown third-party marketplace has no
        # derivable source — we do not invent one; the install surfaces the error.
        settings = json.dumps(
            {
                "extraKnownMarketplaces": {},
                "enabledPlugins": {"thing@some-third-party": True},
            }
        )

        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd:
                out = settings
            elif "plugin list --json" in cmd:
                out = "[]"
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert not any("marketplace add" in c for c in cmds)

    def test_non_object_settings_is_tolerated(self) -> None:
        # #2029: settings.json that isn't a JSON object (e.g. an array) is treated
        # as empty, not crashed on.
        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd or "plugin list --json" in cmd:
                out = "[]"
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)  # no raise
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert not any("marketplace add" in c for c in cmds)

    def test_enabled_id_without_marketplace_suffix_is_skipped(self) -> None:
        # #2029: an enabled id with no @marketplace can't map to a marketplace, so
        # it yields no add command (its install surfaces any problem).
        settings = json.dumps({"extraKnownMarketplaces": {}, "enabledPlugins": {"bare-id": True}})

        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "cat ~/.claude/settings.json" in cmd:
                out = settings
            elif "plugin list --json" in cmd:
                out = "[]"
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        update_plugins(transport)
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert not any("marketplace add" in c for c in cmds)
