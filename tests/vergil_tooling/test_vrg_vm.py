from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_vm import main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture()
def config_file(tmp_path: Path) -> Path:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "vergil"
        vergil = "v2.0"

        [identities.vergil]
        vm_instance = "vergil-agent"
        auth_type = "app"
        app_id = 12345
        private_key_path = "~/.config/vergil/keys/vergil-agent.pem"
        projects_dir = "/home/user/projects"
    """)
    )
    return p


class TestNoSubcommand:
    def test_prints_help_and_returns_1(self) -> None:
        assert main([]) == 1


class TestCreate:
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_full_flow(
        self,
        _status: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        mock_install: MagicMock,
        config_file: Path,
        tmp_path: Path,
    ) -> None:
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        result = main(["create", "--config", str(config_file)])
        assert result == 0

        mock_fetch.assert_called_once_with("v2.0")
        mock_create.assert_called_once()
        mock_start.assert_called_once()
        mock_inject.assert_called_once()
        mock_install.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_create_fails_if_exists(self, _status: MagicMock, config_file: Path) -> None:
        result = main(["create", "--config", str(config_file)])
        assert result == 1

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_fails_without_projects_dir(self, _status: MagicMock, tmp_path: Path) -> None:
        p = tmp_path / "identities.toml"
        p.write_text(
            textwrap.dedent("""\
            vergil = "v2.0"

            [identities.vergil]
            vm_instance = "vergil-agent"
        """)
        )
        result = main(["create", "--config", str(p)])
        assert result == 1

    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_custom_tag(
        self,
        _status: MagicMock,
        mock_fetch: MagicMock,
        _create: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        mock_install: MagicMock,
        config_file: Path,
        tmp_path: Path,
    ) -> None:
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        main(["create", "--config", str(config_file), "--tag", "v3.0"])
        mock_fetch.assert_called_once_with("v3.0")
        mock_install.assert_called_once_with("vergil-agent", "v2.0")

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_fails_without_vergil_version(self, _status: MagicMock, tmp_path: Path) -> None:
        p = tmp_path / "identities.toml"
        p.write_text(
            textwrap.dedent("""\
            [identities.vergil]
            vm_instance = "vergil-agent"
            projects_dir = "/home/user/projects"
        """)
        )
        with pytest.raises(SystemExit):
            main(["create", "--config", str(p)])

    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_uses_identity_vergil_override(
        self,
        _status: MagicMock,
        mock_fetch: MagicMock,
        _create: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        mock_install: MagicMock,
        tmp_path: Path,
    ) -> None:
        p = tmp_path / "identities.toml"
        p.write_text(
            textwrap.dedent("""\
            vergil = "v2.0"

            [identities.vergil]
            vm_instance = "vergil-agent"
            projects_dir = "/home/user/projects"
            vergil = "v2.2"
        """)
        )
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        main(["create", "--config", str(p)])
        mock_fetch.assert_called_once_with("v2.2")
        mock_install.assert_called_once_with("vergil-agent", "v2.2")

    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_uses_vergil_vm_for_tag(
        self,
        _status: MagicMock,
        mock_fetch: MagicMock,
        _create: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        mock_install: MagicMock,
        tmp_path: Path,
    ) -> None:
        p = tmp_path / "identities.toml"
        p.write_text(
            textwrap.dedent("""\
            vergil = "v2.0"
            vergil-vm = "v2.1"

            [identities.vergil]
            vm_instance = "vergil-agent"
            projects_dir = "/home/user/projects"
        """)
        )
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        main(["create", "--config", str(p)])
        mock_fetch.assert_called_once_with("v2.1")
        mock_install.assert_called_once_with("vergil-agent", "v2.0")


class TestStart:
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_and_inject(
        self,
        _status: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["start", "--config", str(config_file)])
        assert result == 0
        mock_start.assert_called_once_with("vergil-agent")
        mock_inject.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_start_fails_if_not_created(self, _status: MagicMock, config_file: Path) -> None:
        result = main(["start", "--config", str(config_file)])
        assert result == 1


class TestStop:
    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    def test_stop(self, mock_stop: MagicMock, config_file: Path) -> None:
        result = main(["stop", "--config", str(config_file)])
        assert result == 0
        mock_stop.assert_called_once_with("vergil-agent")


class TestRestart:
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    def test_restart(
        self,
        mock_stop: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["restart", "--config", str(config_file)])
        assert result == 0
        mock_stop.assert_called_once()
        mock_start.assert_called_once()
        mock_inject.assert_called_once()


class TestDestroy:
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_destroy(self, _status: MagicMock, mock_delete: MagicMock, config_file: Path) -> None:
        result = main(["destroy", "--config", str(config_file)])
        assert result == 0
        mock_delete.assert_called_once_with("vergil-agent")

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_destroy_nonexistent(self, _status: MagicMock, config_file: Path) -> None:
        result = main(["destroy", "--config", str(config_file)])
        assert result == 1


class TestList:
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_shows_identities(
        self, mock_list: MagicMock, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
        ]
        result = main(["list", "--config", str(config_file)])
        assert result == 0
        output = capsys.readouterr().out
        assert "vergil" in output
        assert "vergil-agent" in output
        assert "Running" in output

    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_shows_not_created(
        self, mock_list: MagicMock, config_file: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mock_list.return_value = []
        result = main(["list", "--config", str(config_file)])
        assert result == 0
        output = capsys.readouterr().out
        assert "Not Created" in output


class TestSession:
    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    def test_session_basic(self, mock_exec: MagicMock, config_file: Path) -> None:
        main(["session", "--config", str(config_file)])
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == "limactl"
        assert "vergil-agent" in args[1]
        assert "--start" in args[1]

    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    def test_session_with_workspace(self, mock_exec: MagicMock, config_file: Path) -> None:
        main(["session", "--config", str(config_file), "vergil-tooling"])
        cmd = mock_exec.call_args[0][1]
        assert "bash" in cmd
        assert "-c" in cmd
        inner = cmd[cmd.index("-c") + 1]
        assert "cd /projects/vergil-tooling" in inner
        assert "exec bash --login" in inner

    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    def test_session_with_command(self, mock_exec: MagicMock, config_file: Path) -> None:
        main(["session", "--config", str(config_file), "vergil-tooling", "claude"])
        cmd = mock_exec.call_args[0][1]
        assert "bash" in cmd
        assert "-c" in cmd
        inner = cmd[cmd.index("-c") + 1]
        assert "cd /projects/vergil-tooling" in inner
        assert "exec claude" in inner

    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    def test_session_command_with_flags(self, mock_exec: MagicMock, config_file: Path) -> None:
        main(
            [
                "session",
                "--config",
                str(config_file),
                "vergil-tooling",
                "claude",
                "--model",
                "opus",
            ]
        )
        cmd = mock_exec.call_args[0][1]
        inner = cmd[cmd.index("-c") + 1]
        assert "exec claude --model opus" in inner
