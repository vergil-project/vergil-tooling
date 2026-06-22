from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.lima import (
    _limactl,
    _nested_virt_unsupported_reason,
    create_vm,
    delete_vm,
    fetch_template,
    list_vms,
    nested_virt_unsupported_reason,
    shell_pipe,
    shell_run,
    start_vm,
    stop_vm,
    update_plugins,
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
    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_constructs_create_command(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        mock.assert_called_once()
        args = mock.call_args[0]
        assert args[0] == "create"
        assert "--name=vergil-agent" in args
        assert "--tty=false" in args
        assert str(tpl) in args
        location_arg = [a for a in args if ".mounts[0].location" in a]
        assert len(location_arg) == 1
        assert "/home/user/projects" in location_arg[0]
        mount_point_arg = [a for a in args if ".mounts[0].mountPoint" in a]
        assert len(mount_point_arg) == 1
        assert "/home/user/projects" in mount_point_arg[0]

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_adds_claude_submounts(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        args = mock.call_args[0]
        claude_projects = str(tmp_path / ".claude" / "projects")
        claude_skills = str(tmp_path / ".claude" / "skills")
        assert f'--set=.mounts[1].location = "{claude_projects}"' in args
        assert f'--set=.mounts[1].mountPoint = "{claude_projects}"' in args
        assert "--set=.mounts[1].writable = true" in args
        assert f'--set=.mounts[2].location = "{claude_skills}"' in args
        assert f'--set=.mounts[2].mountPoint = "{claude_skills}"' in args
        assert "--set=.mounts[2].writable = false" in args
        # The vestigial sessions mount (mounts[3]) is removed; sessions stays VM-local.
        assert not any(".mounts[3]" in a for a in args)

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_creates_host_claude_dirs(
        self, _mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        assert (tmp_path / ".claude" / "projects").is_dir()
        assert (tmp_path / ".claude" / "skills").is_dir()
        # create_vm no longer backs a sessions mount, so it must not create the dir.
        assert not (tmp_path / ".claude" / "sessions").is_dir()

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_passes_cpu_override(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects", cpus=12)
        args = mock.call_args[0]
        cpu_args = [a for a in args if "cpus" in a]
        assert len(cpu_args) == 1
        assert cpu_args[0] == "--set=.cpus = 12"

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_passes_memory_override(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects", memory="32GiB")
        args = mock.call_args[0]
        mem_args = [a for a in args if ".memory" in a]
        assert len(mem_args) == 1
        assert mem_args[0] == '--set=.memory = "32GiB"'

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_passes_disk_override(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects", disk="100GiB")
        args = mock.call_args[0]
        disk_args = [a for a in args if ".disk" in a]
        assert len(disk_args) == 1
        assert disk_args[0] == '--set=.disk = "100GiB"'

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_omits_none_overrides(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        args = mock.call_args[0]
        assert not any(".cpus" in a for a in args)
        assert not any(".memory" in a for a in args)
        assert not any(".disk" in a for a in args)

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_passes_all_overrides(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
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

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_nested_sets_both_lima_halves(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects", nested=True)
        args = mock.call_args[0]
        assert "--set=.nestedVirtualization = true" in args
        assert '--set=.param.NESTED_VIRT = "true"' in args

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_no_nested_flags_by_default(
        self, mock: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        tpl = Path("/tmp/template.yaml")  # noqa: S108
        create_vm("vergil-agent", tpl, "/home/user/projects")
        args = mock.call_args[0]
        assert not any("nestedVirtualization" in a for a in args)
        assert not any("NESTED_VIRT" in a for a in args)


class TestNestedVirtSupport:
    """Host-support preflight for nested virtualization (macOS 15+, M3+)."""

    def test_non_darwin_host_unsupported(self) -> None:
        reason = _nested_virt_unsupported_reason("Linux", "", "")
        assert reason is not None
        assert "macOS 15" in reason
        assert "Linux" in reason

    def test_old_macos_unsupported(self) -> None:
        reason = _nested_virt_unsupported_reason("Darwin", "14.7.1", "Apple M3 Pro")
        assert reason is not None
        assert "14.7.1" in reason

    def test_pre_m3_chip_unsupported(self) -> None:
        reason = _nested_virt_unsupported_reason("Darwin", "15.2", "Apple M2 Max")
        assert reason is not None
        assert "Apple M2 Max" in reason

    def test_unknown_cpu_brand_unsupported(self) -> None:
        reason = _nested_virt_unsupported_reason("Darwin", "15.2", "")
        assert reason is not None

    def test_intel_mac_unsupported(self) -> None:
        reason = _nested_virt_unsupported_reason(
            "Darwin", "15.2", "Intel(R) Core(TM) i9-9980HK CPU @ 2.40GHz"
        )
        assert reason is not None

    def test_m3_on_macos_15_supported(self) -> None:
        assert _nested_virt_unsupported_reason("Darwin", "15.2", "Apple M3 Pro") is None

    def test_m4_on_macos_26_supported(self) -> None:
        assert _nested_virt_unsupported_reason("Darwin", "26.0", "Apple M4") is None

    @patch("vergil_tooling.lib.lima.subprocess.run")
    @patch("vergil_tooling.lib.lima.platform.mac_ver", return_value=("15.2", ("", "", ""), ""))
    @patch("vergil_tooling.lib.lima.platform.system", return_value="Darwin")
    def test_wrapper_gathers_host_facts(
        self, _system: MagicMock, _ver: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="Apple M3 Pro\n")
        assert nested_virt_unsupported_reason() is None
        assert mock_run.call_args.args[0] == ["sysctl", "-n", "machdep.cpu.brand_string"]

    @patch("vergil_tooling.lib.lima.subprocess.run")
    @patch("vergil_tooling.lib.lima.platform.mac_ver", return_value=("15.2", ("", "", ""), ""))
    @patch("vergil_tooling.lib.lima.platform.system", return_value="Darwin")
    def test_wrapper_sysctl_failure_is_unsupported(
        self, _system: MagicMock, _ver: MagicMock, mock_run: MagicMock
    ) -> None:
        # No silent failures: an unreadable CPU brand aborts rather than guessing.
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert nested_virt_unsupported_reason() is not None

    @patch("vergil_tooling.lib.lima.platform.system", return_value="Linux")
    def test_wrapper_non_darwin_short_circuits(self, _system: MagicMock) -> None:
        assert nested_virt_unsupported_reason() is not None


class TestStartStopVm:
    @patch("vergil_tooling.lib.lima._limactl_stream")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Stopped")
    def test_start_streams_limactl_with_default_timeout(
        self, _status: MagicMock, mock: MagicMock
    ) -> None:
        start_vm("vergil-agent")
        mock.assert_called_once_with("start", "--timeout=30m", "vergil-agent")

    @patch("vergil_tooling.lib.lima._limactl_stream")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Stopped")
    def test_start_passes_custom_timeout(self, _status: MagicMock, mock: MagicMock) -> None:
        start_vm("vergil-agent", timeout="1h")
        mock.assert_called_once_with("start", "--timeout=1h", "vergil-agent")

    @patch("vergil_tooling.lib.lima._limactl_stream")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Running")
    def test_start_skips_if_running(self, _status: MagicMock, mock: MagicMock) -> None:
        start_vm("vergil-agent")
        mock.assert_not_called()

    @patch("vergil_tooling.lib.lima._limactl_stream", side_effect=RuntimeError("boom"))
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Stopped")
    def test_start_stops_monitor_on_failure(self, _status: MagicMock, mock: MagicMock) -> None:
        # The monitor thread must be stopped and joined even when limactl fails.
        with pytest.raises(RuntimeError, match="boom"):
            start_vm("vergil-agent")
        mock.assert_called_once()

    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Running")
    def test_stop_calls_limactl(
        self, _status: MagicMock, mock: MagicMock, _mock_shell: MagicMock
    ) -> None:
        stop_vm("vergil-agent")
        mock.assert_called_once_with("stop", "vergil-agent")

    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Stopped")
    def test_stop_skips_if_stopped(self, _status: MagicMock, mock: MagicMock) -> None:
        stop_vm("vergil-agent")
        mock.assert_not_called()

    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Running")
    def test_stop_syncs_guest_before_stopping(
        self, _status: MagicMock, mock_limactl: MagicMock, mock_shell: MagicMock
    ) -> None:
        # Flush the guest page cache before the VM stops so the just-written uv
        # cache/receipt are not truncated by a non-synced shutdown.
        manager = MagicMock()
        manager.attach_mock(mock_shell, "shell_run")
        manager.attach_mock(mock_limactl, "limactl")
        stop_vm("vergil-agent")
        mock_shell.assert_called_once_with("vergil-agent", "sync")
        mock_limactl.assert_called_once_with("stop", "vergil-agent")
        assert [c[0] for c in manager.mock_calls] == ["shell_run", "limactl"]

    @patch(
        "vergil_tooling.lib.lima.shell_run",
        side_effect=subprocess.CalledProcessError(1, "sync"),
    )
    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Running")
    def test_stop_proceeds_when_sync_fails(
        self, _status: MagicMock, mock_limactl: MagicMock, _mock_shell: MagicMock
    ) -> None:
        # The sync is best-effort: a failed flush must never block the stop.
        stop_vm("vergil-agent")
        mock_limactl.assert_called_once_with("stop", "vergil-agent")

    @patch("vergil_tooling.lib.lima.shell_run")
    @patch("vergil_tooling.lib.lima._limactl")
    @patch("vergil_tooling.lib.lima.vm_status", return_value="Stopped")
    def test_stop_skips_sync_if_stopped(
        self, _status: MagicMock, mock_limactl: MagicMock, mock_shell: MagicMock
    ) -> None:
        stop_vm("vergil-agent")
        mock_shell.assert_not_called()
        mock_limactl.assert_not_called()


class TestDeleteVm:
    @patch("vergil_tooling.lib.lima._limactl")
    def test_force_deletes(self, mock: MagicMock) -> None:
        delete_vm("vergil-agent")
        mock.assert_called_once_with("delete", "--force", "vergil-agent")


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


class TestCreateVmProfileParams:
    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_profile_params_passed_via_set(
        self, mock_limactl: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        template = tmp_path / "agent.yaml"
        template.write_text("dummy", encoding="utf-8")
        create_vm(
            "vergil-user.org.repo",
            template,
            "/projects",
            cpus=12,
            memory="64GiB",
            disk="300GiB",
            packages=["qemu-system-x86", "libvirt-clients"],
            apt_repos=[
                {
                    "name": "hashicorp",
                    "key_url": "https://apt.releases.hashicorp.com/gpg",
                    "uri": "https://apt.releases.hashicorp.com",
                    "suite": "noble",
                    "components": "main",
                }
            ],
            vagrant_plugins=["vagrant-libvirt"],
            port_forwards=["3000|10.50.0.2:3000", "8080|10.50.0.2:8080"],
            fingerprint="abc123",
        )
        args = mock_limactl.call_args[0]
        assert '--set=.param.EXTRA_PACKAGES = "qemu-system-x86 libvirt-clients"' in args
        assert (
            '--set=.param.APT_REPOS = "hashicorp|https://apt.releases.hashicorp.com/gpg|'
            'https://apt.releases.hashicorp.com|noble|main"' in args
        )
        assert '--set=.param.VAGRANT_PLUGINS = "vagrant-libvirt"' in args
        # records joined by ";" to match the template's IFS=';' parser
        assert '--set=.param.PORT_FORWARDS = "3000|10.50.0.2:3000;8080|10.50.0.2:8080"' in args
        assert '--set=.param.SPEC_FINGERPRINT = "abc123"' in args
        assert "--set=.cpus = 12" in args
        assert "create" in args

    @patch("vergil_tooling.lib.lima.Path.home")
    @patch("vergil_tooling.lib.lima._limactl")
    def test_base_create_adds_no_profile_params(
        self, mock_limactl: MagicMock, mock_home: MagicMock, tmp_path: Path
    ) -> None:
        mock_home.return_value = tmp_path
        template = tmp_path / "agent.yaml"
        template.write_text("dummy", encoding="utf-8")
        create_vm("vergil-user", template, "/projects")
        args = mock_limactl.call_args[0]
        assert not any("param.EXTRA_PACKAGES" in a for a in args)
        assert not any("param.APT_REPOS" in a for a in args)
        assert not any("param.VAGRANT_PLUGINS" in a for a in args)
        assert not any("param.PORT_FORWARDS" in a for a in args)
        assert not any("param.SPEC_FINGERPRINT" in a for a in args)


class TestLimactlStream:
    @patch("vergil_tooling.lib.lima.progress")
    def test_delegates_to_progress_run(self, m_progress: MagicMock) -> None:
        from vergil_tooling.lib.lima import _limactl_stream

        _limactl_stream("start", "--timeout=30m", "vergil-agent")
        m_progress.run.assert_called_once_with(
            ("limactl", "start", "--timeout=30m", "vergil-agent")
        )


class TestParseDurationSecs:
    def test_minutes(self) -> None:
        from vergil_tooling.lib.lima import _parse_duration_secs

        assert _parse_duration_secs("30m") == 1800

    def test_compound(self) -> None:
        from vergil_tooling.lib.lima import _parse_duration_secs

        assert _parse_duration_secs("1h30m") == 5400

    def test_seconds(self) -> None:
        from vergil_tooling.lib.lima import _parse_duration_secs

        assert _parse_duration_secs("90s") == 90

    def test_invalid(self) -> None:
        from vergil_tooling.lib.lima import _parse_duration_secs

        assert _parse_duration_secs("soon") is None

    def test_empty(self) -> None:
        from vergil_tooling.lib.lima import _parse_duration_secs

        assert _parse_duration_secs("") is None

    def test_trailing_garbage(self) -> None:
        from vergil_tooling.lib.lima import _parse_duration_secs

        assert _parse_duration_secs("30mxx") is None


class TestDrainSerialLogs:
    def test_emits_new_complete_lines(self, tmp_path: Path) -> None:
        from vergil_tooling.lib.lima import _drain_serial_logs

        (tmp_path / "serial.log").write_bytes(b"boot one\nboot two\npartial")
        offsets: dict[Path, int] = {}
        with patch("vergil_tooling.lib.lima.progress.emit") as m_emit:
            _drain_serial_logs(tmp_path, offsets)
        assert [c.args[0] for c in m_emit.call_args_list] == [
            "[guest] boot one",
            "[guest] boot two",
        ]
        # partial line held back; offset stops at the last newline
        assert offsets[tmp_path / "serial.log"] == len(b"boot one\nboot two\n")

    def test_second_drain_emits_only_appended(self, tmp_path: Path) -> None:
        from vergil_tooling.lib.lima import _drain_serial_logs

        log = tmp_path / "serial.log"
        log.write_bytes(b"first\n")
        offsets: dict[Path, int] = {}
        with patch("vergil_tooling.lib.lima.progress.emit") as m_emit:
            _drain_serial_logs(tmp_path, offsets)
            log.write_bytes(b"first\nsecond\n")
            _drain_serial_logs(tmp_path, offsets)
        assert [c.args[0] for c in m_emit.call_args_list] == [
            "[guest] first",
            "[guest] second",
        ]

    def test_no_complete_line_is_silent(self, tmp_path: Path) -> None:
        from vergil_tooling.lib.lima import _drain_serial_logs

        (tmp_path / "serial.log").write_bytes(b"no newline yet")
        with patch("vergil_tooling.lib.lima.progress.emit") as m_emit:
            _drain_serial_logs(tmp_path, {})
        m_emit.assert_not_called()

    def test_missing_dir_is_noop(self, tmp_path: Path) -> None:
        from vergil_tooling.lib.lima import _drain_serial_logs

        with patch("vergil_tooling.lib.lima.progress.emit") as m_emit:
            _drain_serial_logs(tmp_path / "absent", {})
        m_emit.assert_not_called()

    def test_unreadable_file_is_skipped(self, tmp_path: Path) -> None:
        from vergil_tooling.lib.lima import _drain_serial_logs

        (tmp_path / "serial.log").write_bytes(b"line\n")
        with (
            patch("vergil_tooling.lib.lima.progress.emit") as m_emit,
            patch.object(Path, "open", side_effect=OSError),
        ):
            _drain_serial_logs(tmp_path, {})
        m_emit.assert_not_called()

    def test_blank_lines_not_emitted(self, tmp_path: Path) -> None:
        from vergil_tooling.lib.lima import _drain_serial_logs

        (tmp_path / "serial.log").write_bytes(b"\r\n\nreal line\r\n")
        with patch("vergil_tooling.lib.lima.progress.emit") as m_emit:
            _drain_serial_logs(tmp_path, {})
        assert [c.args[0] for c in m_emit.call_args_list] == ["[guest] real line"]


class TestHeartbeat:
    def test_with_budget(self) -> None:
        from vergil_tooling.lib.lima import _heartbeat

        line = _heartbeat(125.0, "30m", 1800.0)
        assert line == "[elapsed] 2m05s of 30m timeout budget"

    def test_without_budget(self) -> None:
        from vergil_tooling.lib.lima import _heartbeat

        assert _heartbeat(3.0, "soon", None) == "[elapsed] 3.0s"


class TestProvisionMonitor:
    def test_tails_and_emits_heartbeat(self, tmp_path: Path) -> None:
        import threading
        import time as _time

        from vergil_tooling.lib.lima import _provision_monitor

        (tmp_path / "serial.log").write_bytes(b"boot line\n")
        stop = threading.Event()
        with patch("vergil_tooling.lib.lima.progress.emit") as m_emit:
            thread = threading.Thread(
                target=_provision_monitor,
                args=(tmp_path, "30m", stop),
                kwargs={"poll_secs": 0.01, "heartbeat_secs": 0.03},
            )
            thread.start()
            _time.sleep(0.2)
            stop.set()
            thread.join()
        emitted = [c.args[0] for c in m_emit.call_args_list]
        assert "[guest] boot line" in emitted
        assert any(e.startswith("[elapsed]") for e in emitted)

    def test_stop_preset_drains_once_and_exits(self, tmp_path: Path) -> None:
        import threading

        from vergil_tooling.lib.lima import _provision_monitor

        (tmp_path / "serial.log").write_bytes(b"final line\n")
        stop = threading.Event()
        stop.set()
        with patch("vergil_tooling.lib.lima.progress.emit") as m_emit:
            _provision_monitor(tmp_path, "30m", stop, poll_secs=0.01, heartbeat_secs=0.03)
        assert [c.args[0] for c in m_emit.call_args_list] == ["[guest] final line"]


class TestUpdatePlugins:
    _LISTING = json.dumps(
        [
            {"id": "paad@paad", "scope": "user", "enabled": True},
            {"id": "frontend-design@official", "scope": "user", "enabled": False},
            {"id": "vergil@vergil-marketplace", "scope": "project", "enabled": True},
        ]
    )

    def _fake_shell(self) -> MagicMock:
        def side_effect(_instance: str, *args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            out = self._LISTING if "plugin list --json" in cmd else ""
            return MagicMock(stdout=out, returncode=0)

        mock = MagicMock(side_effect=side_effect)
        return mock

    @patch("vergil_tooling.lib.lima.shell_run")
    def test_refreshes_marketplaces_then_updates_enabled_plugins(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = self._fake_shell().side_effect
        update_plugins("vergil-agent")
        cmds = [c.args[-1] for c in mock_run.call_args_list]
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
        # resolves regardless of the VM's zsh-configured interactive environment.
        for c in mock_run.call_args_list:
            assert c.args[:3] == ("vergil-agent", "bash", "-c")
            assert "export PATH=" in c.args[-1]

    @patch("vergil_tooling.lib.lima.shell_run")
    def test_raises_after_attempting_all_when_a_plugin_fails(self, mock_run: MagicMock) -> None:
        def side_effect(_instance: str, *args: str, **_kw: object) -> MagicMock:
            cmd = args[-1]
            if "claude plugin update paad@paad" in cmd:
                raise subprocess.CalledProcessError(1, "claude plugin update")
            out = self._LISTING if "plugin list --json" in cmd else ""
            return MagicMock(stdout=out, returncode=0)

        mock_run.side_effect = side_effect
        with pytest.raises(RuntimeError, match="paad@paad"):
            update_plugins("vergil-agent")
        # Best-effort: the other enabled plugin is still attempted before raising.
        cmds = [c.args[-1] for c in mock_run.call_args_list]
        assert any("claude plugin update vergil@vergil-marketplace" in c for c in cmds)
