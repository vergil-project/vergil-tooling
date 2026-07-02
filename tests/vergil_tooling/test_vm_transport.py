import subprocess
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.vm_transport import (
    _CONNECT_RETRIES,
    IapTransport,
    LimaTransport,
    SshTransport,
)


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
