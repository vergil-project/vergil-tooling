from __future__ import annotations

import json
import os
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


@pytest.fixture()
def config_file_model(tmp_path: Path) -> Path:
    p = tmp_path / "identities-model.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "vergil"
        vergil = "v2.0"

        [identities.vergil]
        vm_instance = "vergil-agent"
        projects_dir = "/home/user/projects"
        model = "sonnet"
    """)
    )
    return p


@pytest.fixture()
def config_file_top_model(tmp_path: Path) -> Path:
    p = tmp_path / "identities-top-model.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "vergil"
        vergil = "v2.0"
        model = "opus"

        [identities.vergil]
        vm_instance = "vergil-agent"
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

    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_passes_resource_overrides(
        self,
        _status: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        tmp_path: Path,
    ) -> None:
        p = tmp_path / "identities.toml"
        p.write_text(
            textwrap.dedent("""\
            vergil = "v2.0"

            [identities.vergil]
            vm_instance = "vergil-agent"
            projects_dir = "/home/user/projects"
            cpus = 12
            memory = "32GiB"
            disk = "100GiB"
        """)
        )
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        result = main(["create", "--config", str(p)])
        assert result == 0
        mock_create.assert_called_once_with(
            "vergil-agent",
            template,
            "/home/user/projects",
            cpus=12,
            memory="32GiB",
            disk="100GiB",
        )


class TestStart:
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_and_inject(
        self,
        _status: MagicMock,
        _age: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        mock_update: MagicMock,
        mock_copy: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["start", "--config", str(config_file)])
        assert result == 0
        mock_start.assert_called_once_with("vergil-agent", timeout="30m")
        mock_inject.assert_called_once()
        mock_update.assert_called_once()
        mock_copy.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_custom_timeout(
        self,
        _status: MagicMock,
        _age: MagicMock,
        mock_start: MagicMock,
        _inject: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["start", "--config", str(config_file), "--timeout", "1h"])
        assert result == 0
        mock_start.assert_called_once_with("vergil-agent", timeout="1h")

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_start_fails_if_not_created(self, _status: MagicMock, config_file: Path) -> None:
        result = main(["start", "--config", str(config_file)])
        assert result == 1


class TestStartStaleness:
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=5.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_rejects_stale_vm(
        self,
        _status: MagicMock,
        _age: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = main(["start", "--config", str(config_file)])
        assert result == 1
        captured = capsys.readouterr()
        assert "5 days old" in captured.err
        assert "--allow-stale-vm" in captured.err

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=5.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_allows_stale_with_override(
        self,
        _status: MagicMock,
        _age: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["start", "--config", str(config_file), "--allow-stale-vm"])
        assert result == 0
        mock_start.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_passes_fresh_vm(
        self,
        _status: MagicMock,
        _age: MagicMock,
        mock_start: MagicMock,
        _inject: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["start", "--config", str(config_file)])
        assert result == 0
        mock_start.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_calls_auto_update(
        self,
        _status: MagicMock,
        _age: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        mock_update: MagicMock,
        _copy: MagicMock,
        config_file: Path,
    ) -> None:
        main(["start", "--config", str(config_file)])
        mock_update.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_copies_claude_config(
        self,
        _status: MagicMock,
        _age: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _update: MagicMock,
        mock_copy: MagicMock,
        config_file: Path,
    ) -> None:
        main(["start", "--config", str(config_file)])
        mock_copy.assert_called_once()


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


class TestRebuild:
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_rebuild_destroys_and_creates(
        self,
        _status: MagicMock,
        mock_delete: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        mock_start: MagicMock,
        mock_inject: MagicMock,
        mock_install: MagicMock,
        _copy: MagicMock,
        config_file: Path,
        tmp_path: Path,
    ) -> None:
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        result = main(["rebuild", "--config", str(config_file)])
        assert result == 0
        mock_delete.assert_called_once_with("vergil-agent")
        mock_create.assert_called_once()
        mock_start.assert_called_once_with("vergil-agent", timeout="30m")
        mock_inject.assert_called_once()
        mock_install.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_rebuild_custom_timeout(
        self,
        _status: MagicMock,
        _delete: MagicMock,
        mock_fetch: MagicMock,
        _create: MagicMock,
        mock_start: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        _copy: MagicMock,
        config_file: Path,
        tmp_path: Path,
    ) -> None:
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        result = main(["rebuild", "--config", str(config_file), "--timeout", "45m"])
        assert result == 0
        mock_start.assert_called_once_with("vergil-agent", timeout="45m")

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_rebuild_fails_if_not_created(self, _status: MagicMock, config_file: Path) -> None:
        result = main(["rebuild", "--config", str(config_file)])
        assert result == 1

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_rebuild_fails_without_projects_dir(self, _status: MagicMock, tmp_path: Path) -> None:
        p = tmp_path / "identities.toml"
        p.write_text(
            textwrap.dedent("""\
            vergil = "v2.0"

            [identities.vergil]
            vm_instance = "vergil-agent"
        """)
        )
        result = main(["rebuild", "--config", str(p)])
        assert result == 1

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_rebuild_passes_resource_overrides(
        self,
        _status: MagicMock,
        _delete: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        _copy: MagicMock,
        tmp_path: Path,
    ) -> None:
        p = tmp_path / "identities.toml"
        p.write_text(
            textwrap.dedent("""\
            vergil = "v2.0"

            [identities.vergil]
            vm_instance = "vergil-agent"
            projects_dir = "/home/user/projects"
            cpus = 8
            memory = "24GiB"
        """)
        )
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        result = main(["rebuild", "--config", str(p)])
        assert result == 0
        mock_create.assert_called_once_with(
            "vergil-agent",
            template,
            "/home/user/projects",
            cpus=8,
            memory="24GiB",
            disk=None,
        )


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

    @patch("vergil_tooling.bin.vrg_vm._last_activity", return_value=1700000000.0)
    @patch("vergil_tooling.bin.vrg_vm.name_by_session")
    @patch("vergil_tooling.bin.vrg_vm.shell_run")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_sessions_merges_liveness(
        self,
        mock_list: MagicMock,
        mock_shell: MagicMock,
        mock_names: MagicMock,
        _age: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_list.return_value = [{"name": "vergil-agent", "status": "Running"}]
        mock_shell.return_value = MagicMock(
            stdout=json.dumps(
                [
                    {
                        "identity": "vergil",
                        "slot": 1,
                        "path": "vergil-project/vm",
                        "sessionId": "s1",
                        "state": "active",
                        "lastActive": 1748000000.0,
                    },
                    {
                        "identity": "vergil",
                        "slot": 2,
                        "path": "vergil-project/tooling",
                        "sessionId": "s2",
                        "state": "idle",
                        "lastActive": 1700000000.0,
                    },
                ]
            )
        )
        mock_names.return_value = {
            "s1": "vergil:01:vergil-project/vm",
            "s2": "vergil:02:tooling",
        }
        result = main(["list", "--sessions", "--config", str(config_file)])
        assert result == 0
        out = capsys.readouterr().out
        assert "WORKSPACE" in out
        assert "LAST ACTIVE" in out
        assert "vergil-project/vm" in out
        assert "active" in out
        assert "idle" in out

    @patch("vergil_tooling.bin.vrg_vm._last_activity", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.name_by_session", return_value={})
    @patch("vergil_tooling.bin.vrg_vm.shell_run")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_sessions_names_vm_session_without_host_transcript(
        self,
        mock_list: MagicMock,
        mock_shell: MagicMock,
        _names: MagicMock,
        _age: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A session a running VM reports (named from its roster) but for which
        # the host has no transcript must still be listed. The host learns the
        # name only from the VM's report, never from the (absent) transcript.
        mock_list.return_value = [{"name": "vergil-agent", "status": "Running"}]
        mock_shell.return_value = MagicMock(
            stdout=json.dumps(
                [
                    {
                        "identity": "vergil",
                        "slot": 2,
                        "path": "vergil-project/tooling",
                        "sessionId": "s2",
                        "state": "active",
                        "lastActive": 1748000000.0,
                    }
                ]
            )
        )
        result = main(["list", "--sessions", "--config", str(config_file)])
        assert result == 0
        out = capsys.readouterr().out
        assert "vergil-project/tooling" in out
        assert "active" in out

    @patch("vergil_tooling.bin.vrg_vm._last_activity", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.name_by_session")
    @patch("vergil_tooling.bin.vrg_vm.shell_run")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_sessions_archived_filter(
        self,
        mock_list: MagicMock,
        mock_shell: MagicMock,
        mock_names: MagicMock,
        _age: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_list.return_value = [{"name": "vergil-agent", "status": "Running"}]
        mock_shell.return_value = MagicMock(stdout=json.dumps([]))
        mock_names.return_value = {
            "s1": "vergil:01:vergil-project/vm",
            "a1": "archived@2026-05-01T00:00:00Z@vergil:03:tooling",
        }
        # default view hides archived
        assert main(["list", "--sessions", "--config", str(config_file)]) == 0
        out = capsys.readouterr().out
        assert "vergil-project/vm" in out
        assert "tooling" not in out
        # --archived shows only archived
        assert main(["list", "--sessions", "--archived", "--config", str(config_file)]) == 0
        out = capsys.readouterr().out
        assert "tooling" in out
        assert "archived" in out

    @patch("vergil_tooling.bin.vrg_vm.name_by_session", return_value={})
    @patch("vergil_tooling.bin.vrg_vm.shell_run")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_sessions_skips_stopped_vms(
        self,
        mock_list: MagicMock,
        mock_shell: MagicMock,
        _names: MagicMock,
        config_file: Path,
    ) -> None:
        mock_list.return_value = [{"name": "vergil-agent", "status": "Stopped"}]
        result = main(["list", "--sessions", "--config", str(config_file)])
        assert result == 0
        mock_shell.assert_not_called()


class TestUpdate:
    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_update_default_tag(
        self, _status: MagicMock, mock_update: MagicMock, _ver: MagicMock, config_file: Path
    ) -> None:
        result = main(["update", "--config", str(config_file)])
        assert result == 0
        mock_update.assert_called_once_with("vergil-agent", None, fallback_tag="v2.0")

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_update_explicit_tag(
        self, _status: MagicMock, mock_update: MagicMock, _ver: MagicMock, config_file: Path
    ) -> None:
        result = main(["update", "--config", str(config_file), "--tag", "v2.1"])
        assert result == 0
        mock_update.assert_called_once_with("vergil-agent", "v2.1", fallback_tag="v2.0")

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_update_fails_if_not_running(self, _status: MagicMock, config_file: Path) -> None:
        result = main(["update", "--config", str(config_file)])
        assert result == 1

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_update_fails_if_not_created(self, _status: MagicMock, config_file: Path) -> None:
        result = main(["update", "--config", str(config_file)])
        assert result == 1

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", side_effect=["v2.0.60", "v2.0.63"])
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_shows_version_change(
        self,
        _status: MagicMock,
        _update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["update", "--config", str(config_file)])
        out = capsys.readouterr().out
        assert "v2.0.60 → v2.0.63" in out

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", side_effect=["v2.0.63", "v2.0.63"])
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_shows_already_up_to_date(
        self,
        _status: MagicMock,
        _update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["update", "--config", str(config_file)])
        out = capsys.readouterr().out
        assert "v2.0.63 (already up to date)" in out

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", side_effect=[None, "v2.0.63"])
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_shows_version_when_before_unknown(
        self,
        _status: MagicMock,
        _update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["update", "--config", str(config_file)])
        out = capsys.readouterr().out
        assert "vergil-tooling: v2.0.63" in out
        assert "→" not in out


class TestSessionStaleness:
    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=5.0)
    def test_session_rejects_stale_vm(
        self,
        _age: MagicMock,
        _copy: MagicMock,
        _update: MagicMock,
        _link: MagicMock,
        _exec: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = main(["session", "--config", str(config_file), "."])
        assert result == 1
        captured = capsys.readouterr()
        assert "--allow-stale-vm" in captured.err

    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=5.0)
    def test_session_allows_stale_with_override(
        self,
        _age: MagicMock,
        _copy: MagicMock,
        _update: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "--allow-stale-vm", "."])
        mock_exec.assert_called_once()

    @patch("vergil_tooling.bin.vrg_vm.os.execvp")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    def test_session_passes_fresh_vm(
        self,
        _age: MagicMock,
        _copy: MagicMock,
        _update: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "."])
        mock_exec.assert_called_once()


@patch("vergil_tooling.bin.vrg_vm.os.execvp")
@patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
@patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
@patch("vergil_tooling.bin.vrg_vm.try_update_tooling")
@patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
class TestSession:
    def _inner(self, mock_exec: MagicMock) -> str:
        cmd = mock_exec.call_args[0][1]
        return cmd[cmd.index("-c") + 1]

    def test_session_basic(
        self,
        _age: MagicMock,
        mock_update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "."])
        mock_update.assert_called_once_with("vergil-agent", fallback_tag="v2.0")
        mock_exec.assert_called_once()
        args = mock_exec.call_args[0]
        assert args[0] == "limactl"
        assert "vergil-agent" in args[1]
        assert "--start" in args[1]
        assert "--preserve-env" in args[1]
        assert "--workdir=/home/user/projects" in args[1]

    def test_session_sets_terminal_env_forwarding(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        _exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "."])
        allow = os.environ.get("LIMA_SHELLENV_ALLOW", "")
        for var in ("COLORTERM", "TERM_PROGRAM", "TERM_PROGRAM_VERSION"):
            assert var in allow

    def test_session_default_launches_resolver(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "vergil-tooling"])
        cmd = mock_exec.call_args[0][1]
        assert "--workdir=/home/user/projects/vergil-tooling" in cmd
        inner = self._inner(mock_exec)
        assert "claude.env" in inner
        assert "vrg-vm-resolve-session --identity vergil --path vergil-tooling" in inner
        assert "bash --login" not in inner

    def test_session_explicit_claude_uses_resolver(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "vergil-tooling", "claude"])
        inner = self._inner(mock_exec)
        assert "vrg-vm-resolve-session" in inner

    def test_session_claude_with_flags_passes_extra(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
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
        inner = self._inner(mock_exec)
        assert "vrg-vm-resolve-session" in inner
        assert "-- --model opus" in inner

    def test_session_raw_command_override(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "vergil-tooling", "--", "bash"])
        inner = self._inner(mock_exec)
        assert "exec bash" in inner
        assert "vrg-vm-resolve-session" not in inner

    def test_session_slot_passed_to_resolver(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "--slot", "3", "vergil-tooling"])
        inner = self._inner(mock_exec)
        assert "--slot 3" in inner

    def test_session_fork_passed_to_resolver(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(
            [
                "session",
                "--config",
                str(config_file),
                "--slot",
                "2",
                "--fork",
                "vergil-tooling",
            ]
        )
        inner = self._inner(mock_exec)
        assert "--fork" in inner
        assert "--slot 2" in inner

    def test_session_no_model_no_flag(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "vergil-tooling"])
        assert "--model" not in self._inner(mock_exec)

    def test_session_cli_model_passed(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        main(["session", "--config", str(config_file), "--model", "opus", "vergil-tooling"])
        assert "--model opus" in self._inner(mock_exec)

    def test_session_config_model_default(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file_model: Path,
    ) -> None:
        main(["session", "--config", str(config_file_model), "vergil-tooling"])
        assert "--model sonnet" in self._inner(mock_exec)

    def test_session_cli_model_overrides_config(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file_model: Path,
    ) -> None:
        main(["session", "--config", str(config_file_model), "--model", "opus", "vergil-tooling"])
        inner = self._inner(mock_exec)
        assert "--model opus" in inner
        assert "sonnet" not in inner

    def test_session_top_level_model_default(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file_top_model: Path,
    ) -> None:
        # identity has no model; the top-level config default is used
        main(["session", "--config", str(config_file_top_model), "vergil-tooling"])
        assert "--model opus" in self._inner(mock_exec)


def test_session_inner_strips_leading_double_dash() -> None:
    import argparse

    from vergil_tooling.bin.vrg_vm import _session_inner

    ns = argparse.Namespace(cmd=["--", "bash"], slot=None, fork=False, fresh=False)
    inner = _session_inner(ns, "vergil", "p", "", 7, 14)
    assert "exec bash" in inner
    assert "vrg-vm-resolve-session" not in inner


def test_session_inner_raw_override_ignores_model() -> None:
    import argparse

    from vergil_tooling.bin.vrg_vm import _session_inner

    ns = argparse.Namespace(cmd=["--", "bash"], slot=None, fork=False, fresh=False)
    inner = _session_inner(ns, "vergil", "p", "opus", 7, 14)
    assert "exec bash" in inner
    assert "--model" not in inner


def test_session_inner_includes_thresholds() -> None:
    import argparse

    from vergil_tooling.bin.vrg_vm import _session_inner

    ns = argparse.Namespace(cmd=[], slot=None, fork=False, fresh=False)
    inner = _session_inner(ns, "vergil", "p", "", 5, 30)
    assert "--stale-days 5" in inner
    assert "--archive-days 30" in inner


def test_session_inner_fresh_flag() -> None:
    import argparse

    from vergil_tooling.bin.vrg_vm import _session_inner

    ns = argparse.Namespace(cmd=[], slot=None, fork=False, fresh=True)
    inner = _session_inner(ns, "vergil", "p", "", 7, 14)
    assert "--fresh" in inner


def test_format_age() -> None:
    from vergil_tooling.bin.vrg_vm import _format_age

    now = 100 * 86400.0
    assert _format_age(None, now) == "unknown"
    assert _format_age(now - 2 * 3600.0, now) == "2h"  # < 1 day
    assert _format_age(now - 5 * 86400.0, now) == "5d"  # >= 1 day


def test_selected_states() -> None:
    import argparse

    from vergil_tooling.bin.vrg_vm import _selected_states

    def ns(**kw: bool) -> argparse.Namespace:
        base = {"all": False, "active": False, "idle": False, "archived": False}
        base.update(kw)
        return argparse.Namespace(**base)

    assert _selected_states(ns()) == {"active", "idle"}  # default
    assert _selected_states(ns(all=True)) == {"active", "idle", "archived"}
    assert _selected_states(ns(active=True)) == {"active"}
    assert _selected_states(ns(idle=True, archived=True)) == {"idle", "archived"}
