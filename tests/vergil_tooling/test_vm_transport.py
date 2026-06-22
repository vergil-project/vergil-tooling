import subprocess
from unittest.mock import MagicMock, patch

from vergil_tooling.lib.vm_transport import IapTransport, LimaTransport


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

    @patch("vergil_tooling.lib.vm_transport.os.execvp")
    def test_exec_session_tunnels_interactively(self, mock_execvp: MagicMock) -> None:
        IapTransport("inst", "z", "p", "vergil").exec_session("/work", "exec bash")
        cmd = mock_execvp.call_args[0][1]
        assert cmd[:4] == ["gcloud", "compute", "ssh", "vergil@inst"]
        assert "--tunnel-through-iap" in cmd
        assert cmd[-3:] == ["--", "-t", "cd /work && exec bash"]
