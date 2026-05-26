from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.identity import Identity
from vergil_tooling.lib.lima import (
    _inject_host_git_identity,
    _limactl,
    _read_host_git_config,
    copy_claude_config,
    create_vm,
    delete_vm,
    fetch_template,
    inject_credentials,
    install_tooling,
    list_vms,
    shell_pipe,
    shell_run,
    start_vm,
    stop_vm,
    try_update_tooling,
    update_tooling,
    vm_age_days,
    vm_status,
)


class TestLimactl:
    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_limactl_wrapper(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        result = _limactl("list", "--json")
        mock_run.assert_called_once()
        assert result.stdout == "ok"
        args = mock_run.call_args[0][0]
        assert args == ["limactl", "list", "--json"]

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_shell_run_constructs_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        shell_run("vergil-agent", "echo", "hello", workdir="/projects")
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args == [
            "limactl",
            "shell",
            "--workdir",
            "/projects",
            "vergil-agent",
            "--",
            "echo",
            "hello",
        ]

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_shell_run_default_workdir(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        shell_run("vergil-agent", "ls")
        args = mock_run.call_args[0][0]
        assert "--workdir" in args
        idx = args.index("--workdir")
        assert args[idx + 1] == "/tmp"  # noqa: S108

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_shell_pipe_sends_input(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        shell_pipe("vergil-agent", "cat > /tmp/out", "hello")
        mock_run.assert_called_once()
        assert mock_run.call_args[1]["input"] == "hello"
        args = mock_run.call_args[0][0]
        assert "bash" in args
        assert "-c" in args
        assert "cat > /tmp/out" in args

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_limactl_prints_stderr_on_error(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = subprocess.CalledProcessError(1, "limactl")
        err.stderr = "FATA[0000] instance not found\n"
        err.stdout = ""
        mock_run.side_effect = err
        with pytest.raises(subprocess.CalledProcessError):
            _limactl("start", "nonexistent")
        captured = capsys.readouterr()
        assert "instance not found" in captured.err

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_shell_run_prints_stderr_on_error(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = subprocess.CalledProcessError(1, "limactl shell")
        err.stderr = "command failed\n"
        err.stdout = ""
        mock_run.side_effect = err
        with pytest.raises(subprocess.CalledProcessError):
            shell_run("vergil-agent", "false")
        captured = capsys.readouterr()
        assert "command failed" in captured.err

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_shell_pipe_prints_stderr_on_error(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = subprocess.CalledProcessError(1, "limactl shell")
        err.stderr = "pipe error\n"
        err.stdout = ""
        mock_run.side_effect = err
        with pytest.raises(subprocess.CalledProcessError):
            shell_pipe("vergil-agent", "cat > /tmp/out", "data")
        captured = capsys.readouterr()
        assert "pipe error" in captured.err

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_limactl_error_no_stderr(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "limactl")
        err.stderr = ""
        err.stdout = ""
        mock_run.side_effect = err
        with pytest.raises(subprocess.CalledProcessError):
            _limactl("start", "nonexistent")

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_shell_run_error_no_stderr(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "limactl shell")
        err.stderr = ""
        err.stdout = ""
        mock_run.side_effect = err
        with pytest.raises(subprocess.CalledProcessError):
            shell_run("vergil-agent", "false")

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_shell_pipe_error_no_stderr(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "limactl shell")
        err.stderr = ""
        err.stdout = ""
        mock_run.side_effect = err
        with pytest.raises(subprocess.CalledProcessError):
            shell_pipe("vergil-agent", "cat > /tmp/out", "data")


class TestVmStatus:
    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_running(self, mock: MagicMock) -> None:
        mock.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"name": "vergil-agent", "status": "Running"}) + "\n"
        )
        assert vm_status("vergil-agent") == "Running"

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_empty_when_not_found(self, mock: MagicMock) -> None:
        mock.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"name": "other-vm", "status": "Running"}) + "\n"
        )
        assert vm_status("vergil-agent") == ""

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_empty_on_error(self, mock: MagicMock) -> None:
        mock.side_effect = subprocess.CalledProcessError(1, "limactl")
        assert vm_status("vergil-agent") == ""

    @patch("vergil_tooling.lib.lima._limactl")
    def test_multiple_vms(self, mock: MagicMock) -> None:
        lines = "\n".join(
            [
                json.dumps({"name": "default", "status": "Stopped"}),
                json.dumps({"name": "vergil-agent", "status": "Running"}),
            ]
        )
        mock.return_value = subprocess.CompletedProcess([], 0, stdout=lines + "\n")
        assert vm_status("vergil-agent") == "Running"
        assert vm_status("default") == "Stopped"


class TestListVms:
    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_vm_list(self, mock: MagicMock) -> None:
        lines = "\n".join(
            [
                json.dumps({"name": "default", "status": "Stopped"}),
                json.dumps({"name": "vergil-agent", "status": "Running"}),
            ]
        )
        mock.return_value = subprocess.CompletedProcess([], 0, stdout=lines + "\n")
        result = list_vms()
        assert len(result) == 2
        assert result[0] == {"name": "default", "status": "Stopped"}
        assert result[1] == {"name": "vergil-agent", "status": "Running"}

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_empty_on_error(self, mock: MagicMock) -> None:
        mock.side_effect = subprocess.CalledProcessError(1, "limactl")
        assert list_vms() == []


class TestFetchTemplate:
    @patch("vergil_tooling.lib.lima.urllib.request.urlopen")
    def test_downloads_and_writes_tempfile(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"cpus: 4\nmemory: 2GiB\n"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = fetch_template("v2.0")
        assert result.exists()
        assert result.read_text() == "cpus: 4\nmemory: 2GiB\n"
        assert result.suffix == ".yaml"
        result.unlink()

        url = mock_urlopen.call_args[0][0]
        assert "v2.0" in url
        assert "vergil-vm" in url

    @patch("vergil_tooling.lib.lima.urllib.request.urlopen")
    def test_exits_on_network_error(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("network error")
        with pytest.raises(SystemExit):
            fetch_template("v2.0")

    def test_rejects_invalid_tag(self) -> None:
        with pytest.raises(SystemExit):
            fetch_template("../../etc/passwd")

    def test_rejects_arbitrary_string(self) -> None:
        with pytest.raises(SystemExit):
            fetch_template("main")

    @patch("vergil_tooling.lib.lima.urllib.request.urlopen")
    def test_accepts_patch_version(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"cpus: 4\n"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = fetch_template("v2.0.1")
        assert result.exists()
        result.unlink()


class TestCreateVm:
    @patch("vergil_tooling.lib.lima._limactl")
    def test_constructs_create_command(self, mock: MagicMock) -> None:
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        mock.assert_called_once()
        args = mock.call_args[0]
        assert args[0] == "create"
        assert "--name=vergil-agent" in args
        assert "--tty=false" in args
        assert str(tpl) in args
        mount_arg = [a for a in args if ".mounts[0].location" in a]
        assert len(mount_arg) == 1
        assert "/home/user/projects" in mount_arg[0]

    @patch("vergil_tooling.lib.lima._limactl")
    def test_passes_cpu_override(self, mock: MagicMock) -> None:
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects", cpus=12)
        args = mock.call_args[0]
        cpu_args = [a for a in args if "cpus" in a]
        assert len(cpu_args) == 1
        assert cpu_args[0] == "--set=.cpus = 12"

    @patch("vergil_tooling.lib.lima._limactl")
    def test_passes_memory_override(self, mock: MagicMock) -> None:
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects", memory="32GiB")
        args = mock.call_args[0]
        mem_args = [a for a in args if "memory" in a]
        assert len(mem_args) == 1
        assert mem_args[0] == '--set=.memory = "32GiB"'

    @patch("vergil_tooling.lib.lima._limactl")
    def test_passes_disk_override(self, mock: MagicMock) -> None:
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects", disk="100GiB")
        args = mock.call_args[0]
        disk_args = [a for a in args if "disk" in a]
        assert len(disk_args) == 1
        assert disk_args[0] == '--set=.disk = "100GiB"'

    @patch("vergil_tooling.lib.lima._limactl")
    def test_omits_none_overrides(self, mock: MagicMock) -> None:
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        args = mock.call_args[0]
        assert not any("cpus" in a for a in args)
        assert not any("memory" in a for a in args)
        assert not any("disk" in a for a in args)

    @patch("vergil_tooling.lib.lima._limactl")
    def test_passes_all_overrides(self, mock: MagicMock) -> None:
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm(
            "vergil-agent",
            tpl,
            "/home/user/projects",
            cpus=8,
            memory="24GiB",
            disk="100GiB",
        )
        args = mock.call_args[0]
        assert "--set=.cpus = 8" in args
        assert '--set=.memory = "24GiB"' in args
        assert '--set=.disk = "100GiB"' in args
        assert str(tpl) == args[-1]


class TestStartStopVm:
    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Stopped")
    def test_start_calls_limactl(self, _status: MagicMock, mock: MagicMock) -> None:
        start_vm("vergil-agent")
        mock.assert_called_once_with("start", "vergil-agent")

    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Running")
    def test_start_skips_if_running(self, _status: MagicMock, mock: MagicMock) -> None:
        start_vm("vergil-agent")
        mock.assert_not_called()

    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Running")
    def test_stop_calls_limactl(self, _status: MagicMock, mock: MagicMock) -> None:
        stop_vm("vergil-agent")
        mock.assert_called_once_with("stop", "vergil-agent")

    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Stopped")
    def test_stop_skips_if_stopped(self, _status: MagicMock, mock: MagicMock) -> None:
        stop_vm("vergil-agent")
        mock.assert_not_called()


class TestDeleteVm:
    @patch("vergil_tooling.lib.lima._limactl")
    def test_force_deletes(self, mock: MagicMock) -> None:
        delete_vm("vergil-agent")
        mock.assert_called_once_with("delete", "--force", "vergil-agent")


class TestInjectCredentials:
    @patch("vergil_tooling.lib.lima._inject_host_git_identity")
    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima.shell_pipe")
    def test_injects_all_credentials(
        self, mock_pipe: MagicMock, mock_run: MagicMock, _mock_id: MagicMock, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")

        identity = Identity(
            vm_instance="vergil-agent",
            app_id="12345",
            private_key_path=str(key_file),
        )

        inject_credentials("vergil-agent", identity)

        assert mock_run.call_count == 2
        mkdir_call = mock_run.call_args_list[0]
        assert "mkdir" in " ".join(str(a) for a in mkdir_call[0])

        git_call = mock_run.call_args_list[1]
        assert "git" in git_call[0]
        assert "insteadOf" in " ".join(str(a) for a in git_call[0])

        assert mock_pipe.call_count == 2
        pem_call = mock_pipe.call_args_list[0]
        assert "app.pem" in pem_call[0][1]
        assert "fakekey" in pem_call[0][2]

        env_call = mock_pipe.call_args_list[1]
        assert "app.env" in env_call[0][1]
        assert "APP_ID=12345" in env_call[0][2]

    @patch("vergil_tooling.lib.lima._inject_host_git_identity")
    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima.shell_pipe")
    def test_injects_claude_token(
        self, mock_pipe: MagicMock, mock_run: MagicMock, _mock_id: MagicMock, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        token_file = tmp_path / "claude-oauth-token"
        token_file.write_text("test-oauth-token-abc123\n")

        identity = Identity(
            vm_instance="vergil-agent",
            app_id="12345",
            private_key_path=str(key_file),
            claude_token_path=str(token_file),
        )

        inject_credentials("vergil-agent", identity)

        assert mock_run.call_count == 4
        bashrc_call = mock_run.call_args_list[2]
        assert "claude.env" in " ".join(str(a) for a in bashrc_call[0])
        mkdir_call = mock_run.call_args_list[3]
        assert "mkdir" in " ".join(str(a) for a in mkdir_call[0])
        assert ".claude" in " ".join(str(a) for a in mkdir_call[0])

        assert mock_pipe.call_count == 5
        claude_call = mock_pipe.call_args_list[2]
        assert "claude.env" in claude_call[0][1]
        assert "CLAUDE_CODE_OAUTH_TOKEN=test-oauth-token-abc123" in claude_call[0][2]
        creds_call = mock_pipe.call_args_list[3]
        assert ".credentials.json" in creds_call[0][1]
        assert "claudeAiOauth" in creds_call[0][2]
        assert "test-oauth-token-abc123" in creds_call[0][2]
        onboarding_call = mock_pipe.call_args_list[4]
        assert ".claude.json" in onboarding_call[0][1]
        assert "hasCompletedOnboarding" in onboarding_call[0][2]

    @patch("vergil_tooling.lib.lima._inject_host_git_identity")
    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima.shell_pipe")
    def test_skips_claude_token_when_not_configured(
        self, mock_pipe: MagicMock, mock_run: MagicMock, _mock_id: MagicMock, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")

        identity = Identity(
            vm_instance="vergil-agent",
            app_id="12345",
            private_key_path=str(key_file),
        )

        inject_credentials("vergil-agent", identity)

        assert mock_run.call_count == 2
        assert mock_pipe.call_count == 2

    @patch("vergil_tooling.lib.lima._inject_host_git_identity")
    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima.shell_pipe")
    def test_exits_if_claude_token_missing(
        self, _mock_pipe: MagicMock, _mock_run: MagicMock, _mock_id: MagicMock, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        bad_path = "/nonexistent/claude-token"  # noqa: S105

        identity = Identity(
            vm_instance="vergil-agent",
            app_id="12345",
            private_key_path=str(key_file),
            claude_token_path=bad_path,
        )
        with pytest.raises(SystemExit):
            inject_credentials("vergil-agent", identity)

    def test_exits_if_key_missing(self) -> None:
        identity = Identity(
            vm_instance="vergil-agent",
            app_id="12345",
            private_key_path="/nonexistent/key.pem",
        )
        with pytest.raises(SystemExit):
            inject_credentials("vergil-agent", identity)

    @patch("vergil_tooling.lib.lima._inject_host_git_identity")
    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima.shell_pipe")
    def test_calls_inject_host_git_identity(
        self, _mock_pipe: MagicMock, _mock_run: MagicMock, mock_id: MagicMock, tmp_path: Path
    ) -> None:
        key_file = tmp_path / "app.pem"
        key_file.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        identity = Identity(
            vm_instance="vergil-agent",
            app_id="12345",
            private_key_path=str(key_file),
        )
        inject_credentials("vergil-agent", identity)
        mock_id.assert_called_once_with("vergil-agent")


class TestReadHostGitConfig:
    @patch("vergil_tooling.lib.lima.subprocess.run")
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

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_returns_none_on_missing_key(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(1, "git")
        assert _read_host_git_config("user.name") is None

    @patch("vergil_tooling.lib.lima.subprocess.run")
    def test_returns_none_when_git_not_found(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError("git")
        assert _read_host_git_config("user.name") is None


class TestInjectHostGitIdentity:
    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima._read_host_git_config")
    def test_injects_name_and_email(self, mock_config: MagicMock, mock_run: MagicMock) -> None:
        values = {
            "user.name": "Test User",
            "user.email": "test@example.com",
        }
        mock_config.side_effect = lambda k: values[k]
        _inject_host_git_identity("vergil-agent")
        assert mock_run.call_count == 2
        name_call = mock_run.call_args_list[0]
        assert name_call[0] == (
            "vergil-agent",
            "git",
            "config",
            "--global",
            "user.name",
            "Test User",
        )
        email_call = mock_run.call_args_list[1]
        assert email_call[0] == (
            "vergil-agent",
            "git",
            "config",
            "--global",
            "user.email",
            "test@example.com",
        )

    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima._read_host_git_config")
    def test_skips_when_not_configured(self, mock_config: MagicMock, mock_run: MagicMock) -> None:
        mock_config.return_value = None
        _inject_host_git_identity("vergil-agent")
        mock_run.assert_not_called()

    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima._read_host_git_config")
    def test_injects_only_name_when_email_missing(
        self, mock_config: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_config.side_effect = lambda k: "Test User" if k == "user.name" else None
        _inject_host_git_identity("vergil-agent")
        assert mock_run.call_count == 1
        assert "user.name" in mock_run.call_args[0]


class TestInstallTooling:
    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_installs_with_tag(self, mock_run: MagicMock, mock_pipe: MagicMock) -> None:
        install_tooling("vergil-agent", "v2.0")
        mock_run.assert_called_once()
        args = mock_run.call_args[0]
        cmd_str = " ".join(str(a) for a in args)
        assert "uv tool install" in cmd_str
        assert "v2.0" in cmd_str

    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_writes_tag_marker(self, _mock_run: MagicMock, mock_pipe: MagicMock) -> None:
        install_tooling("vergil-agent", "v2.0")
        mock_pipe.assert_called_once()
        assert "tooling-tag" in mock_pipe.call_args[0][1]
        assert "v2.0" in mock_pipe.call_args[0][2]


class TestUpdateTooling:
    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_updates_with_explicit_tag(self, mock_run: MagicMock, mock_pipe: MagicMock) -> None:
        update_tooling("vergil-agent", "v2.0")
        mock_run.assert_called_once()
        cmd_str = " ".join(str(a) for a in mock_run.call_args[0])
        assert "uv tool install --reinstall" in cmd_str
        assert "v2.0" in cmd_str

    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_reads_tag_from_marker(self, mock_run: MagicMock, mock_pipe: MagicMock) -> None:
        mock_run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="v2.0\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        update_tooling("vergil-agent")
        assert mock_run.call_count == 2
        cmd_str = " ".join(str(a) for a in mock_run.call_args_list[1][0])
        assert "uv tool install --reinstall" in cmd_str
        assert "v2.0" in cmd_str

    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_writes_tag_marker(self, _mock_run: MagicMock, mock_pipe: MagicMock) -> None:
        update_tooling("vergil-agent", "v2.1")
        mock_pipe.assert_called_once()
        assert "tooling-tag" in mock_pipe.call_args[0][1]
        assert "v2.1" in mock_pipe.call_args[0][2]

    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_uses_fallback_when_no_marker(self, mock_run: MagicMock, mock_pipe: MagicMock) -> None:
        mock_run.side_effect = [
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        update_tooling("vergil-agent", fallback_tag="v2.0")
        assert mock_run.call_count == 2
        cmd_str = " ".join(str(a) for a in mock_run.call_args_list[1][0])
        assert "uv tool install --reinstall" in cmd_str
        assert "v2.0" in cmd_str

    @patch("vergil_tooling.lib.lima.shell_run")
    def test_exits_if_no_tag_and_no_fallback(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with pytest.raises(SystemExit):
            update_tooling("vergil-agent")


class TestVmAgeDays:
    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_age_in_days(self, mock: MagicMock, tmp_path: Path) -> None:
        vm_dir = tmp_path / "vergil-agent"
        vm_dir.mkdir()

        mock.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"name": "vergil-agent", "dir": str(vm_dir)}) + "\n"
        )

        age = vm_age_days("vergil-agent")
        assert age is not None
        assert age >= 0

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_none_when_vm_not_found(self, mock: MagicMock) -> None:
        mock.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"name": "other-vm", "dir": "/tmp/other"}) + "\n",  # noqa: S108
        )
        assert vm_age_days("vergil-agent") is None

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_none_on_error(self, mock: MagicMock) -> None:
        mock.side_effect = subprocess.CalledProcessError(1, "limactl")
        assert vm_age_days("vergil-agent") is None

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_none_when_dir_empty(self, mock: MagicMock) -> None:
        mock.return_value = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"name": "vergil-agent", "dir": ""}) + "\n"
        )
        assert vm_age_days("vergil-agent") is None

    @patch("vergil_tooling.lib.lima._limactl")
    def test_returns_none_when_dir_not_exists(self, mock: MagicMock) -> None:
        mock.return_value = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"name": "vergil-agent", "dir": "/nonexistent/path"}) + "\n",
        )
        assert vm_age_days("vergil-agent") is None


class TestCopyClaudeConfig:
    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_copies_existing_files(
        self, mock_run: MagicMock, mock_pipe: MagicMock, tmp_path: Path
    ) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "CLAUDE.md").write_text("# My prefs\n")
        (claude_dir / "settings.json").write_text('{"key": "val"}\n')

        copy_claude_config("vergil-agent", claude_dir)

        assert mock_run.call_count == 1
        mkdir_call = mock_run.call_args_list[0]
        assert "mkdir" in " ".join(str(a) for a in mkdir_call[0])
        assert ".claude" in " ".join(str(a) for a in mkdir_call[0])

        assert mock_pipe.call_count == 2
        md_call = mock_pipe.call_args_list[0]
        assert "CLAUDE.md" in md_call[0][1]
        assert "# My prefs" in md_call[0][2]
        settings_call = mock_pipe.call_args_list[1]
        assert "settings.json" in settings_call[0][1]
        assert '"key"' in settings_call[0][2]

    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_skips_missing_files(
        self, mock_run: MagicMock, mock_pipe: MagicMock, tmp_path: Path
    ) -> None:
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()

        copy_claude_config("vergil-agent", claude_dir)

        mock_run.assert_called_once()
        mock_pipe.assert_not_called()

    @patch("vergil_tooling.lib.lima.shell_pipe")
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_skips_if_claude_dir_missing(
        self, mock_run: MagicMock, mock_pipe: MagicMock, tmp_path: Path
    ) -> None:
        claude_dir = tmp_path / ".claude"

        copy_claude_config("vergil-agent", claude_dir)

        mock_run.assert_not_called()
        mock_pipe.assert_not_called()


class TestTryUpdateTooling:
    @patch("vergil_tooling.lib.lima.update_tooling")
    def test_returns_true_on_success(self, mock_update: MagicMock) -> None:
        result = try_update_tooling("vergil-agent", fallback_tag="v2.0")
        assert result is True
        mock_update.assert_called_once_with("vergil-agent", None, fallback_tag="v2.0")

    @patch("vergil_tooling.lib.lima.update_tooling")
    def test_returns_false_on_subprocess_error(
        self, mock_update: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_update.side_effect = subprocess.CalledProcessError(1, "uv")
        result = try_update_tooling("vergil-agent", fallback_tag="v2.0")
        assert result is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    @patch("vergil_tooling.lib.lima.update_tooling")
    def test_returns_false_on_system_exit(
        self, mock_update: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_update.side_effect = SystemExit(1)
        result = try_update_tooling("vergil-agent", fallback_tag="v2.0")
        assert result is False
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    @patch("vergil_tooling.lib.lima.update_tooling")
    def test_passes_explicit_tag(self, mock_update: MagicMock) -> None:
        try_update_tooling("vergil-agent", tag="v2.1", fallback_tag="v2.0")
        mock_update.assert_called_once_with("vergil-agent", "v2.1", fallback_tag="v2.0")
