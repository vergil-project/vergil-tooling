from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.identity import Identity
from vergil_tooling.lib.lima import (
    _limactl,
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
    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima.shell_pipe")
    def test_injects_all_credentials(
        self, mock_pipe: MagicMock, mock_run: MagicMock, tmp_path: Path
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

    def test_exits_if_key_missing(self) -> None:
        identity = Identity(
            vm_instance="vergil-agent",
            app_id="12345",
            private_key_path="/nonexistent/key.pem",
        )
        with pytest.raises(SystemExit):
            inject_credentials("vergil-agent", identity)


class TestInstallTooling:
    @patch("vergil_tooling.lib.lima.shell_run")
    def test_installs_with_tag(self, mock_run: MagicMock) -> None:
        install_tooling("vergil-agent", "v2.0")
        mock_run.assert_called_once()
        args = mock_run.call_args[0]
        cmd_str = " ".join(str(a) for a in args)
        assert "uv tool install" in cmd_str
        assert "v2.0" in cmd_str
