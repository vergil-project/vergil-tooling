from __future__ import annotations

import json
import shlex
import subprocess
import tomllib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.conftest import failing_command, shell_transport
from vergil_tooling.lib import vm_packages
from vergil_tooling.lib.atomic_write import atomic_write_cmd
from vergil_tooling.lib.identity import Identity
from vergil_tooling.lib.trusted_authors import TrustedAuthors, parse_trusted_authors
from vergil_tooling.lib.vm_guest import (
    _PLUGIN_PATH_EXPORT,
    _PRUNE_PROBE,
    _READ_GUEST_SETTINGS,
    VmUnreachableError,
    _inject_host_git_identity,
    _read_host_git_config,
    copy_claude_config,
    get_tooling_version,
    inject_credentials,
    inject_trusted_authors,
    install_tooling,
    link_claude_dirs,
    prune_cache_images,
    read_fingerprint,
    update_plugins,
    update_tooling,
    vm_occupancy,
    vm_probe,
    vm_spec_status,
)
from vergil_tooling.lib.vm_transport import TransportTimeoutError

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

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_injects_conan_token(self, _mock_id: MagicMock, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        token_file = tmp_path / "conan-audit-token"
        token_file.write_text("conan-token-xyz789\n")

        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(key_file),
            conan_audit_token_path=str(token_file),
        )

        transport = _transport()
        inject_credentials(transport, identity)

        # base 3 runs + 1 bashrc source line for conan.env
        assert transport.run.call_count == 4
        bashrc_call = transport.run.call_args_list[3]
        assert "conan.env" in " ".join(str(a) for a in bashrc_call[0])

        # base 3 pipes + 1 conan.env write
        assert transport.pipe.call_count == 4
        conan_call = transport.pipe.call_args_list[3]
        assert "conan.env" in conan_call[0][0]
        assert "chmod 600" in conan_call[0][0]
        assert "CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER=conan-token-xyz789" in conan_call[0][1]

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_conan_token_value_is_shell_quoted(self, _mock_id: MagicMock, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        token_file = tmp_path / "conan-audit-token"
        token_file.write_text("tok en$with;special\n")

        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(key_file),
            conan_audit_token_path=str(token_file),
        )

        transport = _transport()
        inject_credentials(transport, identity)

        conan_call = transport.pipe.call_args_list[3]
        quoted = shlex.quote("tok en$with;special")
        assert conan_call[0][1] == f"export CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER={quoted}\n"

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_skips_conan_token_when_not_configured(
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
        for call in transport.pipe.call_args_list:
            assert "conan.env" not in call[0][0]

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_exits_if_conan_token_missing(self, _mock_id: MagicMock, tmp_path: Path) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        bad_path = "/nonexistent/conan-audit-token"  # noqa: S105

        identity = Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(key_file),
            conan_audit_token_path=bad_path,
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

    @staticmethod
    def _full_identity(tmp_path: Path) -> Identity:
        host = tmp_path / "host"
        host.mkdir()
        (host / "app.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nnewkey\n")
        (host / "claude-token").write_text("claude-tok\n")
        (host / "conan-token").write_text("conan-tok\n")
        return Identity(
            vm_instance="vergil-agent",
            mode="user",
            app_id="12345",
            private_key_path=str(host / "app.pem"),
            claude_token_path=str(host / "claude-token"),
            conan_audit_token_path=str(host / "conan-token"),
        )

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_real_shell_writes_every_file_with_its_mode(
        self, _mock_id: MagicMock, tmp_path: Path
    ) -> None:
        # #4035: the real commands in bash, over a guest that already holds a
        # read-only app.pem (a cat > could not even open it). Every file is
        # replaced whole with its final mode, and no temp file is left behind.
        identity = self._full_identity(tmp_path)
        home = tmp_path / "guest"
        config = home / ".config" / "vergil"
        config.mkdir(parents=True)
        (config / "app.pem").write_text("old key\n")
        (config / "app.pem").chmod(0o400)

        transport = shell_transport(home)
        real_run = transport.run.side_effect
        # The git config calls are not under test; keep them off the real git.
        transport.run.side_effect = lambda *a: _ok() if a[0] == "git" else real_run(*a)
        inject_credentials(transport, identity)

        expected = {
            config / "app.pem": ("-----BEGIN RSA PRIVATE KEY-----\nnewkey\n", 0o600),
            config / "app.env": ("APP_ID=12345\n", 0o600),
            config / "identity-mode": ("user\n", 0o600),
            config / "claude.env": ("export CLAUDE_CODE_OAUTH_TOKEN=claude-tok\n", 0o600),
            config / "conan.env": (
                "export CONAN_AUDIT_PROVIDER_TOKEN_CONANCENTER=conan-tok\n",
                0o600,
            ),
            home / ".claude" / ".credentials.json": (
                '{"claudeAiOauth": {"accessToken": "claude-tok"}}\n',
                0o600,
            ),
            home / ".claude.json": ('{"hasCompletedOnboarding": true}\n', 0o644),
        }
        for path, (content, mode) in expected.items():
            assert path.read_text() == content, path
            assert path.stat().st_mode & 0o777 == mode, path
        assert sorted(p.name for p in config.iterdir()) == [
            "app.env",
            "app.pem",
            "claude.env",
            "conan.env",
            "identity-mode",
        ]
        assert [p.name for p in (home / ".claude").iterdir()] == [".credentials.json"]
        assert sorted(p.name for p in home.iterdir()) == [
            ".bashrc",
            ".claude",
            ".claude.json",
            ".config",
        ]

    @patch("vergil_tooling.lib.vm_guest._inject_host_git_identity")
    def test_failed_write_leaves_the_original_and_no_temp_file(
        self, _mock_id: MagicMock, tmp_path: Path
    ) -> None:
        # A write that breaks before the rename fails loudly and keeps the guest
        # file exactly as it was, with no temp file beside it.
        identity = self._full_identity(tmp_path)
        home = tmp_path / "guest"
        config = home / ".config" / "vergil"
        config.mkdir(parents=True)
        (config / "app.pem").write_text("old key\n")
        (config / "app.pem").chmod(0o600)
        fake_bin = tmp_path / "bin"
        failing_command(fake_bin, "mv")

        with pytest.raises(subprocess.CalledProcessError):
            inject_credentials(shell_transport(home, fake_bin), identity)

        assert (config / "app.pem").read_text() == "old key\n"
        assert (config / "app.pem").stat().st_mode & 0o777 == 0o600
        assert [p.name for p in config.iterdir()] == ["app.pem"]


class TestInjectTrustedAuthors:
    def test_inject_trusted_authors_writes_vm_file(self) -> None:
        transport = _transport()
        authors = TrustedAuthors(human=("example-human",), agents=("example-agent",))

        inject_trusted_authors(transport, authors)

        transport.pipe.assert_called_once()
        cmd, content = transport.pipe.call_args.args
        assert cmd == atomic_write_cmd("~/.config/vergil/trusted-authors.toml", 0o600)
        assert parse_trusted_authors(tomllib.loads(content)) == authors
        transport.run.assert_called_once_with("bash", "-c", "mkdir -p ~/.config/vergil")

    def test_inject_trusted_authors_removes_vm_file_when_host_has_none(self) -> None:
        transport = _transport()

        inject_trusted_authors(transport, None)

        transport.pipe.assert_not_called()
        transport.run.assert_called_once_with(
            "bash", "-c", "rm -f ~/.config/vergil/trusted-authors.toml"
        )

    def test_real_shell_replaces_the_vm_file(self, tmp_path: Path) -> None:
        # #4035: written whole with mode 600 over an earlier copy, no temp file.
        home = tmp_path / "guest"
        config = home / ".config" / "vergil"
        config.mkdir(parents=True)
        (config / "trusted-authors.toml").write_text("stale\n")
        authors = TrustedAuthors(human=("example-human",), agents=("example-agent",))

        inject_trusted_authors(shell_transport(home), authors)

        written = config / "trusted-authors.toml"
        assert parse_trusted_authors(tomllib.loads(written.read_text())) == authors
        assert written.stat().st_mode & 0o777 == 0o600
        assert [p.name for p in config.iterdir()] == ["trusted-authors.toml"]


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


@pytest.fixture
def installs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Record the packaged/dev install dispatch instead of running it."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        vm_packages, "packaged_install", lambda _t, ref: calls.append(("packaged", ref))
    )
    monkeypatch.setattr(vm_packages, "dev_install", lambda _t, ref: calls.append(("dev", ref)))
    return calls


class TestInstallTooling:
    @pytest.mark.parametrize("tag", ["v2.0", "v2.1.226"])
    def test_release_tag_installs_packaged(self, installs: list[tuple[str, str]], tag: str) -> None:
        install_tooling(_transport(), tag)
        assert installs == [("packaged", tag)]

    def test_dev_ref_installs_with_uv(self, installs: list[tuple[str, str]]) -> None:
        install_tooling(_transport(), "develop")
        assert installs == [("dev", "develop")]

    def test_creates_tag_dir_before_write(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        install_tooling(transport, "v2.0")
        assert transport.run.call_count == 1
        cmd_str = " ".join(str(a) for a in transport.run.call_args_list[0][0])
        assert "mkdir -p" in cmd_str
        assert installs

    @pytest.mark.parametrize("tag", ["v2.0", "develop"])
    def test_writes_tag_marker(self, installs: list[tuple[str, str]], tag: str) -> None:
        transport = _transport()
        install_tooling(transport, tag)
        transport.pipe.assert_called_once()
        assert "tooling-tag" in transport.pipe.call_args[0][0]
        assert transport.pipe.call_args[0][1] == f"{tag}\n"
        assert installs

    def test_failed_install_records_no_tag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fail(_t: object, _ref: str) -> None:
            raise SystemExit(1)

        monkeypatch.setattr(vm_packages, "packaged_install", fail)
        transport = _transport()
        with pytest.raises(SystemExit):
            install_tooling(transport, "v2.2")
        transport.pipe.assert_not_called()

    def test_real_shell_writes_the_tag_marker(
        self, installs: list[tuple[str, str]], tmp_path: Path
    ) -> None:
        # #4035: the marker is replaced whole (mode 644), with no temp file left.
        home = tmp_path / "guest"
        config = home / ".config" / "vergil"
        config.mkdir(parents=True)
        (config / "tooling-tag").write_text("v1.0\n")

        install_tooling(shell_transport(home), "v2.0")

        assert (config / "tooling-tag").read_text() == "v2.0\n"
        assert (config / "tooling-tag").stat().st_mode & 0o777 == 0o644
        assert [p.name for p in config.iterdir()] == ["tooling-tag"]
        assert installs == [("packaged", "v2.0")]


class TestUpdateTooling:
    def test_updates_with_explicit_tag(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        update_tooling(transport, "v2.0")
        assert installs == [("packaged", "v2.0")]
        transport.run.assert_not_called()

    def test_explicit_dev_tag_installs_with_uv(self, installs: list[tuple[str, str]]) -> None:
        update_tooling(_transport(), "feature/1-x")
        assert installs == [("dev", "feature/1-x")]

    def test_reads_tag_from_marker(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok("v2.0\n"), _ok()]
        update_tooling(transport)
        assert installs == [("packaged", "v2.0")]
        assert transport.run.call_count == 2

    def test_explicit_tag_does_not_persist_marker(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        update_tooling(transport, "develop")
        transport.pipe.assert_not_called()
        assert installs == [("dev", "develop")]

    def test_plain_update_after_dev_returns_to_packaged(
        self, installs: list[tuple[str, str]]
    ) -> None:
        # The dev ref was explicit, so never persisted: the marker still holds the
        # identity version, and a plain update goes back to the packaged install.
        transport = _transport()
        transport.run.side_effect = [_ok("v2.1\n"), _ok()]
        update_tooling(transport, fallback_tag="v2.1")
        assert installs == [("packaged", "v2.1")]

    def test_resolved_tag_persists_marker(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok("v2.1\n"), _ok()]
        update_tooling(transport)
        transport.pipe.assert_called_once()
        assert "tooling-tag" in transport.pipe.call_args[0][0]
        assert "v2.1" in transport.pipe.call_args[0][1]
        assert installs

    def test_fallback_tag_persists_marker(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok(), _ok()]
        update_tooling(transport, fallback_tag="v2.1")
        transport.pipe.assert_called_once()
        assert "tooling-tag" in transport.pipe.call_args[0][0]
        assert "v2.1" in transport.pipe.call_args[0][1]
        assert installs == [("packaged", "v2.1")]

    def test_uses_fallback_when_no_marker(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok(), _ok()]
        update_tooling(transport, fallback_tag="v2.0")
        assert installs == [("packaged", "v2.0")]

    def test_creates_tag_dir_before_write(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok("v2.0\n"), _ok()]
        update_tooling(transport)
        cmd_str = " ".join(str(a) for a in transport.run.call_args_list[1][0])
        assert "mkdir -p" in cmd_str
        assert installs

    def test_exits_if_no_tag_and_no_fallback(self, installs: list[tuple[str, str]]) -> None:
        transport = _transport()
        with pytest.raises(SystemExit):
            update_tooling(transport)
        assert installs == []


class TestGetToolingVersion:
    def test_parses_version_from_uv_tool_list(self) -> None:
        uv_output = "vergil-tooling v2.0.63\n    - vrg-commit\n"
        transport = _transport(uv_output)
        assert get_tooling_version(transport) == "v2.0.63"
        assert transport.run.call_count == 1

    def test_falls_back_to_installed_package(self) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok("some-other-tool v1.0\n"), _ok("2.1.240-1")]
        assert get_tooling_version(transport) == "2.1.240-1"
        assert "dpkg-query" in " ".join(transport.run.call_args_list[1][0])

    def test_returns_none_when_not_installed(self) -> None:
        transport = _transport()
        transport.run.side_effect = [_ok("some-other-tool v1.0\n"), _ok("")]
        assert get_tooling_version(transport) is None

    def test_returns_none_on_empty_output(self) -> None:
        transport = _transport("")
        assert get_tooling_version(transport) is None

    def test_uv_failure_falls_back_to_package(self) -> None:
        transport = _transport()
        transport.run.side_effect = [subprocess.CalledProcessError(1, "uv"), _ok("2.1.240-1\n")]
        assert get_tooling_version(transport) == "2.1.240-1"


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

        # #3987: each file is written to a temp file in ~/.claude (so the mv is a
        # rename on one filesystem), given its final mode there, then moved into
        # place; a failure removes the temp file and leaves the original alone.
        # The rename also replaces a read-only CLAUDE.md without reopening it
        # (#2436), since it needs only the directory to be writable.
        assert md_call[0][0] == (
            'tmp=$(mktemp ~/.claude/.CLAUDE.md.XXXXXX) && { cat > "$tmp"'
            ' && chmod 444 "$tmp"'
            ' && mv -f "$tmp" ~/.claude/CLAUDE.md || { rm -f "$tmp"; false; }; }'
        )
        assert settings_call[0][0] == (
            'tmp=$(mktemp ~/.claude/.settings.json.XXXXXX) && { cat > "$tmp"'
            ' && chmod 644 "$tmp"'
            ' && mv -f "$tmp" ~/.claude/settings.json || { rm -f "$tmp"; false; }; }'
        )

    def test_copy_claude_config_locks_claude_md(self, tmp_path: Path) -> None:
        # The guest CLAUDE.md is a read-only copy of the host's on every
        # transport: the copy writes it, then locks it. settings.json is never
        # locked (provisioning and the Claude runtime both write it).
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# prefs\n")
        (claude_dir / "settings.json").write_text("{}\n")

        transport = _transport()
        copy_claude_config(transport, claude_dir)

        piped = [c.args[0] for c in transport.pipe.call_args_list]
        md = next(cmd for cmd in piped if "~/.claude/CLAUDE.md" in cmd)
        # The lock is applied to the temp file, before it is moved into place.
        assert md.index('chmod 444 "$tmp"') < md.index('mv -f "$tmp" ~/.claude/CLAUDE.md')
        others = [cmd for cmd in piped if cmd != md]
        others += [c.args[-1] for c in transport.run.call_args_list]
        assert others
        assert not any("chmod 444" in cmd or "a-w" in cmd for cmd in others)

    def test_real_shell_replaces_both_files_with_their_final_modes(self, tmp_path: Path) -> None:
        # The real commands in bash, over a guest whose CLAUDE.md an earlier copy
        # left read-only (#2436): both files are replaced, CLAUDE.md is locked,
        # settings.json is not, and no temp file is left behind.
        claude_dir = tmp_path / "host" / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text("# new prefs\n")
        (claude_dir / "settings.json").write_text('{"new": true}\n')
        home = tmp_path / "guest"
        guest = home / ".claude"
        guest.mkdir(parents=True)
        (guest / "CLAUDE.md").write_text("# old prefs\n")
        (guest / "CLAUDE.md").chmod(0o444)
        (guest / "settings.json").write_text('{"old": true}\n')

        copy_claude_config(shell_transport(home), claude_dir)

        assert (guest / "CLAUDE.md").read_text() == "# new prefs\n"
        assert (guest / "settings.json").read_text() == '{"new": true}\n'
        assert (guest / "CLAUDE.md").stat().st_mode & 0o777 == 0o444
        assert (guest / "settings.json").stat().st_mode & 0o777 == 0o644
        assert sorted(p.name for p in guest.iterdir()) == ["CLAUDE.md", "settings.json"]

    @pytest.mark.parametrize("failing", ["cat", "chmod", "mv"])
    @pytest.mark.parametrize("filename", ["CLAUDE.md", "settings.json"])
    def test_failed_write_leaves_the_original_and_no_temp_file(
        self, failing: str, filename: str, tmp_path: Path
    ) -> None:
        # A write that breaks part-way (a fake command that writes a fragment,
        # then fails) must fail loudly, keep the guest file exactly as it was,
        # mode included, and remove its temp file.
        claude_dir = tmp_path / "host" / ".claude"
        claude_dir.mkdir(parents=True)
        # The host always has a valid settings.json (it is required, #3988).
        (claude_dir / "settings.json").write_text('{"new": true}\n')
        if filename == "CLAUDE.md":
            (claude_dir / filename).write_text("new content that never lands\n")
        home = tmp_path / "guest"
        guest = home / ".claude"
        guest.mkdir(parents=True)
        original = guest / filename
        original.write_text("original\n")
        original.chmod(0o444)
        fake_bin = tmp_path / "bin"
        failing_command(fake_bin, failing)

        with pytest.raises(subprocess.CalledProcessError):
            copy_claude_config(shell_transport(home, fake_bin), claude_dir)

        assert original.read_text() == "original\n"
        assert original.stat().st_mode & 0o777 == 0o444
        assert [p.name for p in guest.iterdir()] == [filename]

    def test_missing_claude_md_removes_the_guest_copy(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{}\n")

        transport = _transport()
        copy_claude_config(transport, claude_dir)

        # A guest CLAUDE.md the host does not have is removed; settings.json is
        # still copied.
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert cmds == ["mkdir -p ~/.claude", "rm -f ~/.claude/CLAUDE.md"]
        piped = [c.args[0] for c in transport.pipe.call_args_list]
        assert len(piped) == 1
        assert "~/.claude/settings.json" in piped[0]

    def test_missing_settings_fails_naming_the_host_file(self, tmp_path: Path) -> None:
        # #3988: the host settings.json is required on every path (start,
        # rebuild, session, cloud create, update). A missing file is refused
        # before the guest is touched, never skipped over a stale guest copy.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# prefs\n")

        transport = _transport()
        with pytest.raises(RuntimeError) as excinfo:
            copy_claude_config(transport, claude_dir)
        assert f"the host {claude_dir / 'settings.json'} is missing" in str(excinfo.value)
        assert "required" in str(excinfo.value)
        transport.run.assert_not_called()
        transport.pipe.assert_not_called()

    def test_missing_host_claude_dir_fails_naming_the_host_file(self, tmp_path: Path) -> None:
        transport = _transport()
        with pytest.raises(RuntimeError, match=r"\.claude/settings\.json is missing"):
            copy_claude_config(transport, tmp_path / ".claude")
        transport.run.assert_not_called()
        transport.pipe.assert_not_called()

    def test_missing_settings_leaves_the_guest_untouched(self, tmp_path: Path) -> None:
        # The real commands in bash: a refused copy changes nothing in the
        # guest, so no half-updated config is left behind.
        claude_dir = tmp_path / "host" / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text("# new prefs\n")
        home = tmp_path / "guest"
        guest = home / ".claude"
        guest.mkdir(parents=True)
        (guest / "CLAUDE.md").write_text("# old prefs\n")
        (guest / "settings.json").write_text('{"old": true}\n')

        with pytest.raises(RuntimeError, match="settings.json is missing"):
            copy_claude_config(shell_transport(home), claude_dir)

        assert (guest / "CLAUDE.md").read_text() == "# old prefs\n"
        assert (guest / "settings.json").read_text() == '{"old": true}\n'

    def test_copies_a_valid_settings_file(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{}\n")

        transport = _transport()
        copy_claude_config(transport, claude_dir)
        piped = [c.args[0] for c in transport.pipe.call_args_list]
        assert any("~/.claude/settings.json" in cmd for cmd in piped)

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            "[]",
            '{"enabledPlugins": {"x@y": "true"}}',
            # #3995: a plugin enabled from a marketplace the file does not declare.
            '{"enabledPlugins": {"x@y": true}}',
        ],
    )
    def test_rejects_an_unusable_host_settings_file(self, content: str, tmp_path: Path) -> None:
        # The text about to be piped is checked as the refresh will read it, so
        # a host file changed after a pre-flight is never copied into a VM.
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text(content)

        (claude_dir / "CLAUDE.md").write_text("# prefs\n")

        transport = _transport()
        with pytest.raises(RuntimeError, match=r"the host .*settings\.json is unusable"):
            copy_claude_config(transport, claude_dir)
        # Refused before anything is written to the guest: not even CLAUDE.md,
        # which is copied first.
        transport.run.assert_not_called()
        transport.pipe.assert_not_called()

    def test_unusable_settings_leaves_the_guest_untouched(self, tmp_path: Path) -> None:
        # The real commands in bash: a host settings.json that fails the content
        # check leaves the guest's CLAUDE.md and settings.json as they were.
        claude_dir = tmp_path / "host" / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text("# new prefs\n")
        (claude_dir / "settings.json").write_text("{not json")
        home = tmp_path / "guest"
        guest = home / ".claude"
        guest.mkdir(parents=True)
        (guest / "CLAUDE.md").write_text("# old prefs\n")
        (guest / "settings.json").write_text('{"old": true}\n')

        with pytest.raises(RuntimeError, match="unusable"):
            copy_claude_config(shell_transport(home), claude_dir)

        assert (guest / "CLAUDE.md").read_text() == "# old prefs\n"
        assert (guest / "settings.json").read_text() == '{"old": true}\n'

    def test_unreadable_host_file_is_a_named_error(self, tmp_path: Path) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{}\n")
        # A directory where the file should be: read_text raises an OSError.
        (claude_dir / "CLAUDE.md").mkdir()

        transport = _transport()
        with pytest.raises(RuntimeError, match=r"cannot read the host .*CLAUDE\.md"):
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
    # box over limactl or an off-platform box over IAP; only the transport differs.
    # The refresh itself lives in claude_marketplace (test_claude_marketplace.py);
    # these tests cover the guest wiring: the settings read, the PATH export and
    # the RuntimeError naming every failure.
    _SETTINGS = json.dumps(
        {
            "extraKnownMarketplaces": {
                "vergil-marketplace": {
                    "source": {
                        "source": "github",
                        "repo": "vergil-project/vergil-claude-plugin",
                        "ref": "v2.1",
                    }
                }
            },
            "enabledPlugins": {"vergil@vergil-marketplace": True},
        }
    )
    _PLUGIN_LIST = json.dumps([{"id": "vergil@vergil-marketplace", "scope": "user"}])

    @staticmethod
    def _registered(ref: str) -> str:
        return json.dumps(
            [
                {
                    "name": "vergil-marketplace",
                    "source": "github",
                    "repo": "vergil-project/vergil-claude-plugin",
                    "ref": ref,
                }
            ]
        )

    def _fake_transport(
        self,
        *,
        settings: str | Exception | None = None,
        registered_ref: str = "v2.1",
        fail_on: str = "",
    ) -> MagicMock:
        # *settings* is the guest read's raw stdout (status line first) or the
        # exception the transport raises; the default is a readable file.
        settings_out = f"ok\n{self._SETTINGS}" if settings is None else settings

        def side_effect(*args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if fail_on and fail_on in cmd:
                raise subprocess.CalledProcessError(1, "bash")
            if cmd == _READ_GUEST_SETTINGS:
                if isinstance(settings_out, Exception):
                    raise settings_out
                out = settings_out
            elif "marketplace list --json" in cmd:
                out = self._registered(registered_ref)
            elif "plugin list --json" in cmd:
                out = self._PLUGIN_LIST
            else:
                out = ""
            return MagicMock(stdout=out, returncode=0)

        transport = MagicMock()
        transport.run.side_effect = side_effect
        return transport

    def test_refreshes_through_the_shared_sequence(self) -> None:
        transport = self._fake_transport()
        update_plugins(transport)
        claude_cmds = [c.args[-1] for c in transport.run.call_args_list if "claude " in c.args[-1]]
        assert [c.split(" && ", 1)[1] for c in claude_cmds] == [
            "claude plugin marketplace add vergil-project/vergil-claude-plugin@v2.1",
            "claude plugin marketplace update vergil-marketplace",
            "claude plugin list --json",
            "claude plugin update vergil@vergil-marketplace --scope user",
            "claude plugin marketplace list --json",
        ]
        # Every claude call is a non-login bash -c with an explicit PATH export, so
        # claude resolves regardless of the guest's zsh-configured environment.
        for c in transport.run.call_args_list:
            assert c.args[:2] == ("bash", "-c")
        for cmd in claude_cmds:
            assert cmd.startswith(_PLUGIN_PATH_EXPORT + " && ")

    def test_update_plugins_fails_when_verification_fails(self) -> None:
        transport = self._fake_transport(registered_ref="main")
        with pytest.raises(RuntimeError, match="marketplace:vergil-marketplace"):
            update_plugins(transport)

    @pytest.mark.parametrize(
        ("settings", "expected"),
        [
            pytest.param("missing\n", r"settings\.json is missing", id="missing"),
            pytest.param(
                "unreadable\n", r"settings\.json exists but cannot be read", id="unreadable"
            ),
            pytest.param("ok\n{not valid json", r"settings\.json", id="invalid"),
            pytest.param("ok\n[]", r"settings\.json", id="not-an-object"),
            pytest.param("", r"unexpected output", id="no-status-line"),
            pytest.param("garbage\n{}", r"unexpected output", id="unknown-status"),
        ],
    )
    def test_update_plugins_fails_on_missing_unreadable_or_invalid_guest_settings(
        self, settings: str, expected: str
    ) -> None:
        transport = self._fake_transport(settings=settings)
        with pytest.raises(RuntimeError, match=expected) as excinfo:
            update_plugins(transport)
        # A file problem is never reported as an unreachable VM.
        assert not isinstance(excinfo.value, VmUnreachableError)
        assert "~/.claude/settings.json" in str(excinfo.value)
        # Nothing ran against claude before the failure.
        assert not any("claude " in c.args[-1] for c in transport.run.call_args_list)

    @pytest.mark.parametrize("returncode", [255, 1])
    def test_update_plugins_reports_an_unreachable_vm_not_a_missing_file(
        self, returncode: int
    ) -> None:
        # The guest read masks every file condition into a zero exit, so any
        # failed round-trip (ssh's 255 after the connect retries, or limactl
        # failing to reach a stopped instance) is the connection, not the file.
        transport = self._fake_transport(settings=subprocess.CalledProcessError(returncode, "ssh"))
        with pytest.raises(VmUnreachableError) as excinfo:
            update_plugins(transport)
        message = str(excinfo.value)
        assert "could not be reached" in message
        assert f"exit status {returncode}" in message
        assert "~/.claude/settings.json" in message
        assert "missing" not in message
        assert not any("claude " in c.args[-1] for c in transport.run.call_args_list)

    def test_raises_naming_every_failure_after_attempting_all(self) -> None:
        transport = self._fake_transport(fail_on="claude plugin update vergil")
        with pytest.raises(RuntimeError) as excinfo:
            update_plugins(transport)
        assert "vergil@vergil-marketplace (user)" in str(excinfo.value)
        # The verification still ran after the failed update.
        cmds = [c.args[-1] for c in transport.run.call_args_list]
        assert any("marketplace list --json" in c for c in cmds)

    def test_marketplace_add_failure_is_surfaced(self) -> None:
        transport = self._fake_transport(fail_on="marketplace add")
        with pytest.raises(RuntimeError, match="marketplace:vergil-marketplace"):
            update_plugins(transport)


class TestReadGuestSettingsScript:
    """The in-guest read, run for real under bash against a scratch HOME.

    Every file condition must exit 0 with a status line, so a non-zero exit
    can only ever mean the transport failed.
    """

    @staticmethod
    def _run(home: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["bash", "-c", _READ_GUEST_SETTINGS],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )

    def test_readable_file_is_ok_then_content(self, tmp_path: Path) -> None:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text('{"a": 1}\n')
        result = self._run(tmp_path)
        assert result.returncode == 0
        assert result.stdout == 'ok\n{"a": 1}\n'

    def test_missing_file_is_missing(self, tmp_path: Path) -> None:
        result = self._run(tmp_path)
        assert result.returncode == 0
        assert result.stdout == "missing\n"

    def test_unreadable_file_is_unreadable(self, tmp_path: Path) -> None:
        # A directory in the file's place cannot be read even as root, unlike
        # a mode-000 file, so the case holds wherever the tests run.
        (tmp_path / ".claude" / "settings.json").mkdir(parents=True)
        result = self._run(tmp_path)
        assert result.returncode == 0
        assert result.stdout == "unreadable\n"


class TestPruneCacheImages:
    """The box's own vrg-container-cache prunes; a failure is loud but never raised."""

    _VERDICTS = "Repo set: 1 repo(s) under /p\n\n  remove  img--old--abc  (no live branch)\n"

    @staticmethod
    def _cmds(transport: MagicMock) -> list[str]:
        return [c.args[-1] for c in transport.run.call_args_list]

    def test_applies_with_roots_and_prints_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        transport = MagicMock()
        transport.run.side_effect = [_ok("ok\n"), _ok(self._VERDICTS)]
        assert prune_cache_images(transport, ["/home/u/my projects"]) is True
        probe, prune = self._cmds(transport)
        assert probe == _PRUNE_PROBE
        assert prune.endswith(
            "vrg-container-cache prune --apply --root " + shlex.quote("/home/u/my projects")
        )
        out = capsys.readouterr().out
        assert "    remove  img--old--abc  (no live branch)" in out
        assert "    Repo set: 1 repo(s) under /p" in out

    def test_no_roots_keeps_the_box_default(self) -> None:
        transport = MagicMock()
        transport.run.side_effect = [_ok("ok\n"), _ok()]
        assert prune_cache_images(transport) is True
        assert "--root" not in self._cmds(transport)[1]
        assert self._cmds(transport)[1].endswith("vrg-container-cache prune --apply")

    @pytest.mark.parametrize(
        ("token", "reason"),
        [
            ("absent", "vrg-container-cache is not installed in the box"),
            ("no-prune", "has no 'prune' subcommand (its vergil-tooling predates it)"),
            ("garbage", "unexpected probe output 'garbage'"),
        ],
    )
    def test_unavailable_prune_is_skipped_with_a_reason(
        self, token: str, reason: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transport = _transport(f"{token}\n")
        assert prune_cache_images(transport, ["/p"]) is False
        transport.run.assert_called_once()
        err = capsys.readouterr().err
        assert "WARNING: cache prune skipped:" in err
        assert reason in err

    def test_probe_failure_is_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        transport = MagicMock()
        transport.run.side_effect = subprocess.CalledProcessError(255, ["ssh"])
        assert prune_cache_images(transport) is False
        err = capsys.readouterr().err
        assert "ERROR: cache prune skipped: could not check" in err
        assert "exit status 255" in err
        assert "update itself is unaffected" in err

    def test_failed_prune_prints_its_output_and_the_exit(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        transport = MagicMock()
        transport.run.side_effect = [
            _ok("ok\n"),
            subprocess.CalledProcessError(1, ["bash"], output=self._VERDICTS),
        ]
        assert prune_cache_images(transport) is False
        captured = capsys.readouterr()
        assert "    remove  img--old--abc  (no live branch)" in captured.err
        assert "ERROR: cache prune failed in the box (exit status 1)" in captured.err
        assert "update itself is unaffected" in captured.err
        assert "img--old--abc" not in captured.out

    def test_failed_prune_without_output(self, capsys: pytest.CaptureFixture[str]) -> None:
        transport = MagicMock()
        transport.run.side_effect = [_ok("ok\n"), subprocess.CalledProcessError(2, ["bash"])]
        assert prune_cache_images(transport) is False
        assert "(exit status 2)" in capsys.readouterr().err

    def test_timed_out_prune_is_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        transport = MagicMock()
        transport.run.side_effect = [_ok("ok\n"), TransportTimeoutError(["ssh"], 600)]
        assert prune_cache_images(transport) is False
        assert "(timed out after 600s)" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("help_exit", "token"),
        [(0, "ok"), (2, "no-prune")],
    )
    def test_probe_script_tells_old_tooling_apart(
        self, help_exit: int, token: str, tmp_path: Path
    ) -> None:
        # The real probe in bash, against a fake vrg-container-cache in
        # ~/.local/bin (first on the probe's PATH): an old release's argparse
        # rejects 'prune' with exit 2.
        bin_dir = tmp_path / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        fake = bin_dir / "vrg-container-cache"
        fake.write_text(f"#!/bin/sh\nexit {help_exit}\n")
        fake.chmod(0o755)
        result = subprocess.run(  # noqa: S603
            ["bash", "-c", _PRUNE_PROBE],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        )
        assert result.stdout.strip() == token
