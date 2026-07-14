import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import vm_transport
from vergil_tooling.lib.vm_transport import (
    _CONNECT_RETRIES,
    IapTransport,
    LimaTransport,
    SshTransport,
    control_socket_path,
    ssh_mux_options,
)


@pytest.fixture(autouse=True)
def _isolate_control_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep multiplexing hermetic: point HOME at tmp so control sockets/dirs land
    under the test's tmp_path, never the developer's real home. Setting HOME (rather
    than patching _control_dir) exercises the real _control_dir() path logic too."""
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture
def _disable_mux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(vm_transport._MUX_DISABLE_ENV, "1")


class TestLimaTransport:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_constructs_limactl_shell(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        t = LimaTransport("vm-x")
        result = t.run("echo", "hi", workdir="/work")
        assert result.stdout == "ok"
        args = mock_run.call_args[0][0]
        assert args == ["limactl", "shell", "--workdir", "/work", "vm-x", "--", "echo", "hi"]

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_sends_input(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        LimaTransport("vm-x").pipe("cat > f", "payload", workdir="/work")
        assert mock_run.call_args[1]["input"] == "payload"
        args = mock_run.call_args[0][0]
        assert args == [
            "limactl",
            "shell",
            "--workdir",
            "/work",
            "vm-x",
            "--",
            "bash",
            "-c",
            "cat > f",
        ]

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_default_workdir_is_tmp(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        LimaTransport("vm-x").run("ls")
        assert mock_run.call_args[0][0][:5] == [
            "limactl",
            "shell",
            "--workdir",
            "/tmp",  # noqa: S108
            "vm-x",
        ]

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_prints_stderr_on_error(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "limactl")
        err.stderr = "boom"
        mock_run.side_effect = err
        try:
            LimaTransport("vm-x").run("false")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_prints_stderr_on_error(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "limactl")
        err.stderr = "boom"
        mock_run.side_effect = err
        try:
            LimaTransport("vm-x").pipe("cat > f", "data")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_error_without_stderr_is_silent(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "limactl")
        err.stderr = ""
        mock_run.side_effect = err
        try:
            LimaTransport("vm-x").run("false")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_error_without_stderr_is_silent(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "limactl")
        err.stderr = ""
        mock_run.side_effect = err
        try:
            LimaTransport("vm-x").pipe("cat > f", "data")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.os.execvp")
    def test_exec_session_execs_limactl_start(self, mock_execvp: MagicMock) -> None:
        LimaTransport("vm-x").exec_session("/work", "exec bash")
        cmd = mock_execvp.call_args[0][1]
        assert cmd[:4] == ["limactl", "shell", "--start", "--preserve-env"]
        assert "--workdir=/work" in cmd
        assert cmd[-3:] == ["bash", "-c", "exec bash"]

    @patch("vergil_tooling.lib.vm_transport.subprocess.Popen")
    def test_popen_streams_via_limactl_shell(self, mock_popen: MagicMock) -> None:
        LimaTransport("vm-x").popen("tail", "-f", "/log", workdir="/work")
        args = mock_popen.call_args[0][0]
        assert args == [
            "limactl",
            "shell",
            "--workdir",
            "/work",
            "vm-x",
            "--",
            "tail",
            "-f",
            "/log",
        ]
        assert mock_popen.call_args[1]["stdout"] == subprocess.PIPE
        assert mock_popen.call_args[1]["stderr"] == subprocess.STDOUT


class TestIapTransport:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_builds_iap_command(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        t = IapTransport("inst", "us-central1-b", "proj", "vergil")
        result = t.run("echo", "hi", workdir="/work")
        assert result.stdout == "ok"
        args = mock_run.call_args[0][0]
        assert args[:6] == [
            "gcloud",
            "compute",
            "ssh",
            "vergil@inst",
            "--tunnel-through-iap",
            "--zone=us-central1-b",
        ]
        assert "--project=proj" in args
        assert args[-1] == "--command=cd /work && echo hi"

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_sends_input(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").pipe("cat > f", "payload", workdir="/work")
        assert mock_run.call_args[1]["input"] == "payload"
        assert mock_run.call_args[0][0][-1] == "--command=cd /work && cat > f"

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_prints_stderr_on_error(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "gcloud")
        err.stderr = "boom"
        mock_run.side_effect = err
        try:
            IapTransport("inst", "z", "p", "vergil").run("false")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_error_without_stderr_is_silent(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "gcloud")
        err.stderr = ""
        mock_run.side_effect = err
        try:
            IapTransport("inst", "z", "p", "vergil").run("false")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_prints_stderr_on_error(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "gcloud")
        err.stderr = "boom"
        mock_run.side_effect = err
        try:
            IapTransport("inst", "z", "p", "vergil").pipe("cat > f", "data")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_error_without_stderr_is_silent(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "gcloud")
        err.stderr = ""
        mock_run.side_effect = err
        try:
            IapTransport("inst", "z", "p", "vergil").pipe("cat > f", "data")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_quiet_suppresses_stderr_on_error(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # quiet=True is for probe callers where a connect failure is expected
        # and the raw error (e.g. IAP 4003) would be misleading noise.
        err = subprocess.CalledProcessError(255, "gcloud")
        err.stderr = "ERROR: 4003: failed to connect to port 22"
        mock_run.side_effect = err
        try:
            IapTransport("inst", "z", "p", "vergil").run("true", quiet=True)
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")
        assert capsys.readouterr().err == ""

    @patch("vergil_tooling.lib.vm_transport.time.sleep")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_retries_transient_connect_failure_then_succeeds(
        self, mock_run: MagicMock, mock_sleep: MagicMock
    ) -> None:
        # A transient IAP tunnel blip (exit 255 / "4003: failed to connect") is
        # retried rather than aborting a multi-minute rebuild (#1992).
        blip = subprocess.CalledProcessError(255, "gcloud")
        blip.stderr = "ERROR: 4003: failed to connect to port 22"
        mock_run.side_effect = [
            blip,
            subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
        ]
        result = IapTransport("inst", "z", "p", "vergil").run("true")
        assert result.stdout == "ok"
        assert mock_run.call_count == 2
        assert mock_sleep.called

    @patch("vergil_tooling.lib.vm_transport.time.sleep")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_does_not_retry_real_command_failure(
        self, mock_run: MagicMock, mock_sleep: MagicMock
    ) -> None:
        # A remote command that ran and returned nonzero (exit 1) is a real
        # failure — retrying would mask it (no-silent-failures).
        err = subprocess.CalledProcessError(1, "gcloud")
        err.stderr = "test: /x: No such file or directory"
        mock_run.side_effect = err
        with pytest.raises(subprocess.CalledProcessError):
            IapTransport("inst", "z", "p", "vergil").run("test", "-d", "/x")
        assert mock_run.call_count == 1
        assert not mock_sleep.called

    @patch("vergil_tooling.lib.vm_transport.time.sleep")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_gives_up_after_bounded_retries(
        self, mock_run: MagicMock, mock_sleep: MagicMock
    ) -> None:
        # A persistently unreachable box still fails the stage — the retries are
        # bounded, not infinite.
        blip = subprocess.CalledProcessError(255, "gcloud")
        blip.stderr = "ERROR: 4003: failed to connect to port 22"
        mock_run.side_effect = blip
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            IapTransport("inst", "z", "p", "vergil").run("true")
        assert excinfo.value.returncode == 255
        assert mock_run.call_count == _CONNECT_RETRIES + 1

    @patch("vergil_tooling.lib.vm_transport.time.sleep")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_quiet_probe_is_not_retried(
        self, mock_run: MagicMock, mock_sleep: MagicMock
    ) -> None:
        # quiet=True callers (the readiness gate) own their own poll loop, so a
        # connect failure passes straight back — an internal retry here would
        # blow out their cadence.
        blip = subprocess.CalledProcessError(255, "gcloud")
        blip.stderr = "ERROR: 4003"
        mock_run.side_effect = blip
        with pytest.raises(subprocess.CalledProcessError):
            IapTransport("inst", "z", "p", "vergil").run("true", quiet=True)
        assert mock_run.call_count == 1
        assert not mock_sleep.called

    @patch("vergil_tooling.lib.vm_transport.time.sleep")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_retries_transient_connect_failure(
        self, mock_run: MagicMock, mock_sleep: MagicMock
    ) -> None:
        blip = subprocess.CalledProcessError(255, "gcloud")
        blip.stderr = "ERROR: 4003"
        mock_run.side_effect = [
            blip,
            subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        ]
        IapTransport("inst", "z", "p", "vergil").pipe("cat > f", "data")
        assert mock_run.call_count == 2

    @patch("vergil_tooling.lib.vm_transport.os.execvp")
    def test_exec_session_tunnels_interactively(self, mock_execvp: MagicMock) -> None:
        IapTransport("inst", "z", "p", "vergil").exec_session("/work", "exec bash")
        cmd = mock_execvp.call_args[0][1]
        assert cmd[:4] == ["gcloud", "compute", "ssh", "vergil@inst"]
        assert "--tunnel-through-iap" in cmd
        assert cmd[-3:] == ["--", "-t", "cd /work && exec bash"]

    @patch("vergil_tooling.lib.vm_transport.os.execvp")
    def test_exec_session_stays_off_shared_mux_socket(self, mock_execvp: MagicMock) -> None:
        # #2345: the interactive PTY must NOT ride the shared control socket, or a
        # concurrent session's reap-on-start tears it down ("Shared connection closed").
        # It keeps its own connection but retains keepalive bounding.
        IapTransport("inst", "z", "p", "vergil").exec_session("/work", "exec bash")
        cmd = mock_execvp.call_args[0][1]
        assert not any("Control" in a for a in cmd)
        assert "--ssh-flag=-oServerAliveInterval=15" in cmd
        assert "--ssh-flag=-oConnectTimeout=30" in cmd

    @patch("vergil_tooling.lib.vm_transport.subprocess.Popen")
    def test_popen_streams_over_iap_tunnel(self, mock_popen: MagicMock) -> None:
        IapTransport("inst", "z", "p", "vergil").popen(
            "sudo", "tail", "-f", "/var/log/cloud-init-output.log", workdir="/work"
        )
        args = mock_popen.call_args[0][0]
        assert args[:4] == ["gcloud", "compute", "ssh", "vergil@inst"]
        assert args[-1] == "--command=cd /work && sudo tail -f /var/log/cloud-init-output.log"
        assert mock_popen.call_args[1]["stdout"] == subprocess.PIPE
        assert mock_popen.call_args[1]["stderr"] == subprocess.STDOUT


class TestSshTransport:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_base_command_structure(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="ok", stderr="")
        t = SshTransport(host="20.1.2.3", ssh_user="ubuntu", key_path="/k/id_ed25519")
        result = t.run("echo", "hi", workdir="/vergil")
        assert result.stdout == "ok"
        argv = mock_run.call_args[0][0]
        assert argv[0] == "ssh"
        assert "ubuntu@20.1.2.3" in argv
        assert "/k/id_ed25519" in argv  # -i <key>
        assert any("cd /vergil && echo hi" in a for a in argv)
        assert "StrictHostKeyChecking=accept-new" in " ".join(argv)

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_default_workdir_is_tmp(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").run("ls")
        argv = mock_run.call_args[0][0]
        assert any("cd /tmp" in a for a in argv)  # noqa: S108

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_key_passed_with_dash_i(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        t = SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/keys/my_key")
        t.run("true")
        argv = mock_run.call_args[0][0]
        i_idx = argv.index("-i")
        assert argv[i_idx + 1] == "/keys/my_key"

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_sends_input(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").pipe(
            "cat > f", "payload", workdir="/work"
        )
        assert mock_run.call_args[1]["input"] == "payload"
        argv = mock_run.call_args[0][0]
        assert any("cd /work && cat > f" in a for a in argv)

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_prints_stderr_on_error(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = subprocess.CalledProcessError(1, "ssh")
        err.stderr = "boom"
        mock_run.side_effect = err
        try:
            SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").pipe(
                "cat > f", "data"
            )
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")
        assert "boom" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_error_without_stderr_is_silent(self, mock_run: MagicMock) -> None:
        err = subprocess.CalledProcessError(1, "ssh")
        err.stderr = ""
        mock_run.side_effect = err
        try:
            SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").pipe(
                "cat > f", "data"
            )
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_prints_stderr_on_error(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = subprocess.CalledProcessError(1, "ssh")
        err.stderr = "boom"
        mock_run.side_effect = err
        try:
            SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").run("false")
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")
        assert "boom" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_quiet_suppresses_stderr_on_error(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = subprocess.CalledProcessError(255, "ssh")
        err.stderr = "ssh: connect to host 1.2.3.4 port 22: Connection refused"
        mock_run.side_effect = err
        try:
            SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").run(
                "true", quiet=True
            )
        except subprocess.CalledProcessError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected CalledProcessError")
        assert capsys.readouterr().err == ""

    @patch("vergil_tooling.lib.vm_transport.time.sleep")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_retries_transient_connect_failure_then_succeeds(
        self, mock_run: MagicMock, mock_sleep: MagicMock
    ) -> None:
        # plain ssh also exits 255 when the transport fails to connect
        # ("Connection refused" / "Connection closed") — same retriable class as
        # the IAP tunnel (#1992).
        blip = subprocess.CalledProcessError(255, "ssh")
        blip.stderr = "ssh: connect to host 1.2.3.4 port 22: Connection refused"
        mock_run.side_effect = [
            blip,
            subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
        ]
        result = SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").run("true")
        assert result.stdout == "ok"
        assert mock_run.call_count == 2
        assert mock_sleep.called

    @patch("vergil_tooling.lib.vm_transport.time.sleep")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_does_not_retry_real_command_failure(
        self, mock_run: MagicMock, mock_sleep: MagicMock
    ) -> None:
        err = subprocess.CalledProcessError(1, "ssh")
        err.stderr = "test: /x: No such file or directory"
        mock_run.side_effect = err
        transport = SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key")
        with pytest.raises(subprocess.CalledProcessError):
            transport.run("test", "-d", "/x")
        assert mock_run.call_count == 1
        assert not mock_sleep.called

    @patch("vergil_tooling.lib.vm_transport.subprocess.Popen")
    def test_popen_streams_via_ssh(self, mock_popen: MagicMock) -> None:
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").popen(
            "tail", "-f", "/log", workdir="/work"
        )
        argv = mock_popen.call_args[0][0]
        assert argv[0] == "ssh"
        assert any("cd /work && tail -f /log" in a for a in argv)
        assert mock_popen.call_args[1]["stdout"] == subprocess.PIPE
        assert mock_popen.call_args[1]["stderr"] == subprocess.STDOUT

    @patch("vergil_tooling.lib.vm_transport.os.execvp")
    def test_exec_session_execs_ssh_with_pty(self, mock_execvp: MagicMock) -> None:
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").exec_session(
            "/work", "exec bash"
        )
        cmd = mock_execvp.call_args[0][1]
        assert cmd[0] == "ssh"
        assert "-t" in cmd
        # -t must come BEFORE user@host so ssh treats it as an option, not as
        # the remote command (the bug: -t after user@host → no PTY allocated).
        assert cmd.index("-t") < cmd.index("ubuntu@1.2.3.4")
        assert any("cd /work && exec bash" in a for a in cmd)

    @patch("vergil_tooling.lib.vm_transport.os.execvp")
    def test_exec_session_stays_off_shared_mux_socket(self, mock_execvp: MagicMock) -> None:
        # #2345: interactive PTY off the shared control socket, keepalive retained.
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").exec_session(
            "/work", "exec bash"
        )
        cmd = mock_execvp.call_args[0][1]
        assert not any("Control" in a for a in cmd)
        assert "ServerAliveInterval=15" in cmd
        assert "ConnectTimeout=30" in cmd


_HEX16 = 16


class TestControlSocketPath:
    def test_deterministic_for_same_host_and_workdir(self) -> None:
        a = control_socket_path("inst-abc", "/w/tree")
        b = control_socket_path("inst-abc", "/w/tree")
        assert a == b

    def test_unique_per_host(self) -> None:
        assert control_socket_path("inst-a", "/w") != control_socket_path("inst-b", "/w")

    def test_unique_per_workdir(self) -> None:
        # Two worktrees reaching the same box must not share a master socket.
        assert control_socket_path("inst", "/w/one") != control_socket_path("inst", "/w/two")

    def test_filename_is_short_hex(self) -> None:
        name = control_socket_path("inst", "/w").name
        assert len(name) == _HEX16
        assert all(c in "0123456789abcdef" for c in name)

    def test_full_path_stays_under_socket_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # sun_path cap is 104 (macOS) / 108 (Linux); a realistically long home must
        # still fit. Point _control_dir at a long-username home and check the margin.
        long_home = Path("/Users/a-rather-long-developer-username/.config/vergil/cm")
        monkeypatch.setattr(vm_transport, "_control_dir", lambda: long_home)
        assert len(str(control_socket_path("inst", "/some/deep/worktree/path"))) < 104  # noqa: PLR2004


class TestSshMuxOptions:
    def test_enabled_returns_three_options_and_creates_dir(self, tmp_path: Path) -> None:
        opts = ssh_mux_options("inst", "/w")
        keys = [k for k, _ in opts]
        assert keys == ["ControlMaster", "ControlPath", "ControlPersist"]
        assert dict(opts)["ControlMaster"] == "auto"
        assert dict(opts)["ControlPersist"] == vm_transport._CONTROL_PERSIST
        # side effect: the socket's parent dir is created (HOME points at tmp_path)
        assert (tmp_path / ".config" / "vergil" / "cm").is_dir()

    @pytest.mark.usefixtures("_disable_mux")
    def test_disabled_returns_empty(self, tmp_path: Path) -> None:
        assert ssh_mux_options("inst", "/w") == []
        assert not (tmp_path / ".config" / "vergil" / "cm").exists()  # nothing created


class TestMultiplexInjection:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_iap_injects_glued_ssh_flags(self, mock_run: MagicMock) -> None:
        # gcloud splits --ssh-flag on spaces, so the glued -oKey=Val form is required.
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("true")
        args = mock_run.call_args[0][0]
        assert "--ssh-flag=-oControlMaster=auto" in args
        assert "--ssh-flag=-oControlPersist=60s" in args
        paths = [a for a in args if a.startswith("--ssh-flag=-oControlPath=")]
        assert len(paths) == 1
        socket = Path(paths[0].split("=", 2)[2])
        assert len(socket.name) == _HEX16

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_ssh_injects_split_o_flags(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").run("true")
        argv = mock_run.call_args[0][0]
        assert "ControlMaster=auto" in argv
        # each option is a -o followed by Key=Val, inserted before the destination.
        assert argv[argv.index("ControlMaster=auto") - 1] == "-o"
        assert any(a.startswith("ControlPath=") for a in argv)
        assert argv.index("ControlMaster=auto") < argv.index("ubuntu@1.2.3.4")

    @pytest.mark.usefixtures("_disable_mux")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_kill_switch_removes_all_injection(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("true")
        args = mock_run.call_args[0][0]
        assert not any("Control" in a for a in args)


class TestClose:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_iap_close_exits_master_and_unlinks_socket(self, mock_run: MagicMock) -> None:
        transport = IapTransport("inst", "z", "p", "vergil")
        socket = control_socket_path("inst", str(Path.cwd()))
        socket.parent.mkdir(parents=True, exist_ok=True)
        socket.write_text("")  # stand in for the live control socket
        transport.close()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ssh"
        assert "-O" in cmd
        assert "exit" in cmd
        assert f"ControlPath={socket}" in cmd
        assert "vergil@inst" in cmd
        assert not socket.exists()  # removed after teardown

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_ssh_close_exits_master(self, mock_run: MagicMock) -> None:
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").close()
        cmd = mock_run.call_args[0][0]
        assert cmd[:1] == ["ssh"]
        assert cmd[-3:] == ["-O", "exit", "ubuntu@1.2.3.4"]

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_close_swallows_teardown_errors(self, mock_run: MagicMock) -> None:
        # A missing socket makes `ssh -O exit` fail; teardown is best-effort and
        # must never raise (the pipeline is already exiting).
        mock_run.side_effect = OSError("ssh not found")
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").close()  # no raise

    @pytest.mark.usefixtures("_disable_mux")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_close_is_noop_when_disabled(self, mock_run: MagicMock) -> None:
        # Both off-platform transports short-circuit teardown under the kill-switch
        # (there is no master to close when injection was disabled).
        IapTransport("inst", "z", "p", "vergil").close()
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").close()
        mock_run.assert_not_called()

    def test_lima_close_is_noop(self) -> None:
        LimaTransport("vm-x").close()  # no raise, no connection to tear down


class TestGuestCommandTimeout:
    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(vm_transport._GUEST_CMD_TIMEOUT_ENV, raising=False)
        assert vm_transport._guest_cmd_timeout() == vm_transport._GUEST_CMD_TIMEOUT_DEFAULT_SECS

    def test_custom_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(vm_transport._GUEST_CMD_TIMEOUT_ENV, "45")
        assert vm_transport._guest_cmd_timeout() == 45.0  # noqa: PLR2004

    def test_invalid_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(vm_transport._GUEST_CMD_TIMEOUT_ENV, "not-a-number")
        assert vm_transport._guest_cmd_timeout() == vm_transport._GUEST_CMD_TIMEOUT_DEFAULT_SECS

    @pytest.mark.parametrize("val", ["0", "-1"])
    def test_non_positive_disables(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv(vm_transport._GUEST_CMD_TIMEOUT_ENV, val)
        assert vm_transport._guest_cmd_timeout() is None

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_default_timeout_passed_to_subprocess(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("true")
        assert mock_run.call_args[1]["timeout"] == vm_transport._GUEST_CMD_TIMEOUT_DEFAULT_SECS

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_disabled_timeout_passes_none(
        self, mock_run: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(vm_transport._GUEST_CMD_TIMEOUT_ENV, "0")
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").run("true")
        assert mock_run.call_args[1]["timeout"] is None


class TestTransportTimeoutError:
    @patch("vergil_tooling.lib.vm_transport.time.sleep")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_timeout_raises_and_is_not_retried(
        self, mock_run: MagicMock, mock_sleep: MagicMock
    ) -> None:
        # A wedged connection must fail loudly, not hang; and a timeout already spent
        # its budget, so it is NOT retried (unlike a transient exit-255 connect blip).
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gcloud"], timeout=600.0)
        with pytest.raises(vm_transport.TransportTimeoutError) as excinfo:
            IapTransport("inst", "z", "p", "vergil").run("true")
        assert mock_run.call_count == 1
        assert not mock_sleep.called
        msg = str(excinfo.value)
        assert "VERGIL_VM_DISABLE_SSH_MUX=1" in msg  # actionable recovery hint
        assert "600s" in msg

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_timeout_carries_real_cmd_and_value(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ssh"], timeout=42.0)
        with pytest.raises(vm_transport.TransportTimeoutError) as excinfo:
            SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").run("true")
        assert excinfo.value.timeout == 42.0  # noqa: PLR2004
        assert excinfo.value.cmd[0] == "ssh"  # the full built argv, not the stub

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_timeout_raises(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["gcloud"], timeout=600.0)
        with pytest.raises(vm_transport.TransportTimeoutError):
            IapTransport("inst", "z", "p", "vergil").pipe("cat > f", "data")


class TestKeepaliveBounding:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_iap_base_carries_keepalive_ssh_flags(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("true")
        args = mock_run.call_args[0][0]
        assert "--ssh-flag=-oConnectTimeout=30" in args
        assert "--ssh-flag=-oServerAliveInterval=15" in args
        assert "--ssh-flag=-oServerAliveCountMax=4" in args

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_ssh_base_carries_keepalive_o_flags(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        SshTransport(host="1.2.3.4", ssh_user="ubuntu", key_path="/k/key").run("true")
        argv = mock_run.call_args[0][0]
        assert "ConnectTimeout=30" in argv
        assert argv[argv.index("ConnectTimeout=30") - 1] == "-o"
        assert "ServerAliveInterval=15" in argv
        # bounding options must precede the destination to take effect
        assert argv.index("ConnectTimeout=30") < argv.index("ubuntu@1.2.3.4")

    @pytest.mark.usefixtures("_disable_mux")
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_keepalive_survives_mux_kill_switch(self, mock_run: MagicMock) -> None:
        # Disabling multiplexing must NOT disable connection bounding — they are
        # independent guards.
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("true")
        args = mock_run.call_args[0][0]
        assert "--ssh-flag=-oConnectTimeout=30" in args
        assert not any("Control" in a for a in args)


class TestDebugTracing:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_debug_logs_command_when_enabled(
        self,
        mock_run: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(vm_transport._DEBUG_ENV, "1")
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("echo", "hi")
        err = capsys.readouterr().err
        assert "[vm-transport]" in err
        assert "echo hi" in err

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_silent_by_default(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("true")
        assert "[vm-transport]" not in capsys.readouterr().err


class TestAnnounce:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_run_announces_by_default(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # #2202: a guest command must never run silently — announce it (host + what +
        # the bound) BEFORE the potentially-hanging subprocess, with no opt-in flag.
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("systemctl", "is-active", "tlshd")
        err = capsys.readouterr().err
        assert "inst: systemctl is-active tlshd" in err
        assert "<=600s" in err  # the default bound is shown

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_pipe_announces_by_default(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").pipe("cat > f", "data")
        assert "inst: cat > f" in capsys.readouterr().err

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_quiet_suppresses_announce(
        self, mock_run: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Probe callers (readiness gate) run quiet and own their cadence — no chatter.
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("true", quiet=True)
        assert capsys.readouterr().err == ""

    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_announce_shows_unbounded_when_timeout_disabled(
        self,
        mock_run: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv(vm_transport._GUEST_CMD_TIMEOUT_ENV, "0")
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        IapTransport("inst", "z", "p", "vergil").run("true")
        assert "unbounded" in capsys.readouterr().err


class TestReapIsBounded:
    @patch("vergil_tooling.lib.vm_transport.subprocess.run")
    def test_close_bounds_the_reap(self, mock_run: MagicMock) -> None:
        # #2202: reaping a wedged master must never itself hang.
        transport = IapTransport("inst", "z", "p", "vergil")
        socket = control_socket_path("inst", str(Path.cwd()))
        socket.parent.mkdir(parents=True, exist_ok=True)
        socket.write_text("")
        transport.close()
        assert mock_run.call_args[1]["timeout"] == vm_transport._REAP_TIMEOUT_SECS
