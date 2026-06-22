from __future__ import annotations

import argparse
import json
import os
import subprocess
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from vergil_tooling.bin.vrg_vm import (
    BorrowError,
    DedicatedRow,
    Target,
    _cloud_backend,
    _CloudState,
    _cs_credentials,
    _cs_tofu_volume,
    _list_rows,
    _log_root,
    _preflight_target,
    _probe_running,
    _read_repo_vm,
    _resolve,
    _resolve_target,
    _target_ref,
    _warn_under,
    discover_dedicated,
    main,
    resolve_borrow,
)
from vergil_tooling.lib.identity import Identity, IdentityConfig
from vergil_tooling.lib.vm_backend import select_backend
from vergil_tooling.lib.vm_spec import ComposedSpec
from vergil_tooling.lib.vm_transport import LimaTransport


def _assert_transport(mock: MagicMock, instance: str) -> None:
    """Assert the mock's first positional arg was a LimaTransport for ``instance``.

    The guest helpers (inject_credentials, update_tooling, vm_probe, …) now take a
    Transport first instead of an instance string; this verifies the routed
    transport addresses the expected VM without coupling to its identity.
    """
    transport = mock.call_args.args[0]
    assert isinstance(transport, LimaTransport)
    assert transport.instance == instance


if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

# Bound at import time, before the autouse _vm_log_root fixture patches the
# module attribute — TestLogRoot exercises the real implementation.
_REAL_LOG_ROOT = _log_root


@pytest.fixture(autouse=True)
def _vm_log_root(tmp_path: Path) -> Iterator[None]:
    """Keep pipeline run logs out of the real repo's .vergil directory."""
    with patch("vergil_tooling.bin.vrg_vm._log_root", return_value=tmp_path):
        yield


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
def config_file_two(tmp_path: Path) -> Path:
    """Two identities so identity-selection tests can assert a non-default choice."""
    p = tmp_path / "identities-two.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "vergil"
        vergil = "v2.0"

        [identities.vergil]
        vm_instance = "vergil-agent"
        projects_dir = "/home/user/projects"

        [identities.audit]
        vm_instance = "audit-agent"
        projects_dir = "/home/user/projects"
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
    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
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
        mock_stop: MagicMock,
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
        mock_inject.assert_called_once()
        mock_install.assert_called_once()
        # The provisioning start plus the post-provision SSH cycle (#1463).
        assert mock_start.call_count == 2
        mock_stop.assert_called_once_with("vergil-agent")

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

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
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
        _stop: MagicMock,
        config_file: Path,
        tmp_path: Path,
    ) -> None:
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        main(["create", "--config", str(config_file), "--tag", "v3.0"])
        mock_fetch.assert_called_once_with("v3.0")
        mock_install.assert_called_once_with(ANY, "v2.0")
        _assert_transport(mock_install, "vergil-agent")

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

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
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
        _stop: MagicMock,
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
        mock_install.assert_called_once_with(ANY, "v2.2")
        _assert_transport(mock_install, "vergil-agent")

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
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
        _stop: MagicMock,
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
        mock_install.assert_called_once_with(ANY, "v2.0")
        _assert_transport(mock_install, "vergil-agent")

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
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
        _stop: MagicMock,
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
    # The start pipeline includes a warn-mode update-plugins stage; mock it so
    # the pipeline tests don't reach the real claude/limactl call.
    @pytest.fixture(autouse=True)
    def _mock_update_plugins(self) -> Iterator[MagicMock]:
        with patch("vergil_tooling.bin.vrg_vm.update_plugins") as m:
            yield m

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
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
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
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
    @pytest.fixture(autouse=True)
    def _mock_update_plugins(self) -> Iterator[MagicMock]:
        with patch("vergil_tooling.bin.vrg_vm.update_plugins") as m:
            yield m

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
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
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
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
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
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
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
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
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
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

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    def test_global_identity_and_config_before_subcommand(
        self, mock_stop: MagicMock, config_file_two: Path
    ) -> None:
        # --identity/--config are accepted globally (before the subcommand) for
        # every verb, not just session — and the global value is honored, not
        # clobbered by the subparser's default.
        result = main(["--identity", "audit", "--config", str(config_file_two), "stop"])
        assert result == 0
        mock_stop.assert_called_once_with("audit-agent")


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
    # The rebuild pipeline includes a warn-mode update-plugins stage; mock it.
    @pytest.fixture(autouse=True)
    def _mock_update_plugins(self) -> Iterator[MagicMock]:
        with patch("vergil_tooling.bin.vrg_vm.update_plugins") as m:
            yield m

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
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
        mock_stop: MagicMock,
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
        mock_inject.assert_called_once()
        mock_install.assert_called_once()
        # The provisioning start plus the post-provision SSH cycle (#1463).
        assert mock_start.call_args_list == [
            call("vergil-agent", timeout="30m"),
            call("vergil-agent", timeout="30m"),
        ]
        mock_stop.assert_called_once_with("vergil-agent")

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
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
        _stop: MagicMock,
        config_file: Path,
        tmp_path: Path,
    ) -> None:
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        result = main(["rebuild", "--config", str(config_file), "--timeout", "45m"])
        assert result == 0
        assert mock_start.call_args_list == [
            call("vergil-agent", timeout="45m"),
            call("vergil-agent", timeout="45m"),
        ]

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_rebuild_creates_if_not_created(
        self,
        _status: MagicMock,
        mock_delete: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        _start: MagicMock,
        mock_inject: MagicMock,
        mock_install: MagicMock,
        _stop: MagicMock,
        config_file: Path,
        tmp_path: Path,
    ) -> None:
        """Rebuild is idempotent: with no VM present it creates one instead of
        aborting, and never runs the destroy stage (#1631)."""
        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        mock_fetch.return_value = template

        result = main(["rebuild", "--config", str(config_file)])
        assert result == 0
        mock_create.assert_called_once()
        mock_delete.assert_not_called()
        mock_inject.assert_called_once()
        mock_install.assert_called_once()

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

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
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
        _stop: MagicMock,
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
    @patch("vergil_tooling.bin.vrg_vm.vm_probe", return_value=(0, 0, None))
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_shows_identities(
        self,
        mock_list: MagicMock,
        _probe: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
        ]
        result = main(["list", "--config", str(config_file)])
        assert result == 0
        output = capsys.readouterr().out
        assert "vergil" in output  # IDENTITY column
        assert "base" in output  # SCOPE column
        assert "Running" in output  # STATUS column
        assert "AGENTS" in output  # new observability header

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

    @patch("vergil_tooling.bin.vrg_vm._last_activity", return_value=1700000000.0)
    @patch("vergil_tooling.bin.vrg_vm.name_by_session")
    @patch("vergil_tooling.bin.vrg_vm.shell_run")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_sessions_workspace_column_fits_long_paths(
        self,
        mock_list: MagicMock,
        mock_shell: MagicMock,
        mock_names: MagicMock,
        _age: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # A workspace longer than the historical 36-char column must render in
        # full and must not shove STATE / LAST ACTIVE out of alignment.
        long_path = "logical-minds-foundry/mq-cluster-tooling"  # 40 chars
        assert len(long_path) > 36
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
                        "identity": "vergil-user",
                        "slot": 1,
                        "path": long_path,
                        "sessionId": "s2",
                        "state": "idle",
                        "lastActive": 1748000000.0,
                    },
                ]
            )
        )
        mock_names.return_value = {
            "s1": "vergil:01:vergil-project/vm",
            "s2": "vergil-user:01:" + long_path,
        }
        assert main(["list", "--sessions", "--config", str(config_file)]) == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        header = lines[0]
        state_col = header.index("STATE")
        # The full long path is rendered, never truncated.
        assert long_path in out
        # STATE begins at the same offset on every data row (header + divider
        # are lines[0] and lines[1]; data rows follow).
        for row in lines[2:]:
            assert row[state_col:].startswith(("active", "idle"))

    @patch("vergil_tooling.bin.vrg_vm._last_activity", return_value=1700000000.0)
    @patch("vergil_tooling.bin.vrg_vm.name_by_session")
    @patch("vergil_tooling.bin.vrg_vm.shell_run")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_list_sessions_column_order_and_sort(
        self,
        mock_list: MagicMock,
        mock_shell: MagicMock,
        mock_names: MagicMock,
        _age: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Columns render IDENTITY -> WORKSPACE -> SLOT, and rows sort by
        # workspace first then slot (identity is only the final tiebreaker).
        mock_list.return_value = [{"name": "vergil-agent", "status": "Running"}]
        mock_shell.return_value = MagicMock(
            stdout=json.dumps(
                [
                    {
                        "identity": "vergil-user",
                        "slot": 2,
                        "path": "alpha/repo",
                        "sessionId": "s1",
                        "state": "idle",
                        "lastActive": 1700000000.0,
                    },
                    {
                        "identity": "vergil",
                        "slot": 1,
                        "path": "beta/repo",
                        "sessionId": "s2",
                        "state": "idle",
                        "lastActive": 1700000000.0,
                    },
                    {
                        "identity": "vergil-user",
                        "slot": 1,
                        "path": "alpha/repo",
                        "sessionId": "s3",
                        "state": "idle",
                        "lastActive": 1700000000.0,
                    },
                ]
            )
        )
        mock_names.return_value = {
            "s1": "vergil-user:02:alpha/repo",
            "s2": "vergil:01:beta/repo",
            "s3": "vergil-user:01:alpha/repo",
        }
        assert main(["list", "--sessions", "--config", str(config_file)]) == 0
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if line.strip()]
        header = lines[0]
        # Column order: IDENTITY, then WORKSPACE, then SLOT, then STATE.
        assert (
            header.index("IDENTITY")
            < header.index("WORKSPACE")
            < header.index("SLOT")
            < header.index("STATE")
        )
        # Data rows (after header + divider) sort by workspace then slot:
        # both alpha/repo rows precede beta/repo, slot 01 before slot 02.
        data = lines[2:]
        assert "alpha/repo" in data[0] and "01" in data[0]
        assert "alpha/repo" in data[1] and "02" in data[1]
        assert "beta/repo" in data[2]


class TestUpdate:
    # vrg-vm update refreshes plugins too (via _update_instance); mock it so the
    # command tests don't reach the real claude/limactl call.
    @pytest.fixture(autouse=True)
    def _mock_update_plugins(self) -> Iterator[MagicMock]:
        with patch("vergil_tooling.bin.vrg_vm.update_plugins") as m:
            yield m

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_update_default_tag(
        self, _status: MagicMock, mock_update: MagicMock, _ver: MagicMock, config_file: Path
    ) -> None:
        result = main(["update", "--config", str(config_file)])
        assert result == 0
        mock_update.assert_called_once_with(ANY, None, fallback_tag="v2.0")
        _assert_transport(mock_update, "vergil-agent")

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_update_refreshes_plugins(
        self,
        _status: MagicMock,
        _update: MagicMock,
        _ver: MagicMock,
        _mock_update_plugins: MagicMock,
        config_file: Path,
    ) -> None:
        result = main(["update", "--config", str(config_file)])
        assert result == 0
        _mock_update_plugins.assert_called_once_with("vergil-agent")

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_update_explicit_tag(
        self, _status: MagicMock, mock_update: MagicMock, _ver: MagicMock, config_file: Path
    ) -> None:
        result = main(["update", "--config", str(config_file), "--tag", "v2.1"])
        assert result == 0
        mock_update.assert_called_once_with(ANY, "v2.1", fallback_tag="v2.0")
        _assert_transport(mock_update, "vergil-agent")

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


@pytest.fixture()
def config_file_multi(tmp_path: Path) -> Path:
    p = tmp_path / "identities-multi.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "vergil"
        vergil = "v2.0"

        [identities.vergil]
        vm_instance = "vergil-agent"
        projects_dir = "/home/user/projects"

        [identities.audit]
        vm_instance = "audit-agent"
        vergil = "v2.5"
        projects_dir = "/home/user/projects"
    """)
    )
    return p


class TestUpdateAll:
    # vrg-vm update --all refreshes plugins per VM (via _update_instance); mock it.
    @pytest.fixture(autouse=True)
    def _mock_update_plugins(self) -> Iterator[MagicMock]:
        with patch("vergil_tooling.bin.vrg_vm.update_plugins") as m:
            yield m

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_updates_every_running_vm(
        self,
        mock_list: MagicMock,
        mock_update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
            {"name": "vergil.acme.widgets", "status": "Running"},
        ]
        result = main(["update", "--all", "--config", str(config_file)])
        assert result == 0
        assert mock_update.call_args_list == [
            call(ANY, None, fallback_tag="v2.0"),
            call(ANY, None, fallback_tag="v2.0"),
        ]
        instances = [c.args[0].instance for c in mock_update.call_args_list]
        assert instances == ["vergil-agent", "vergil.acme.widgets"]
        assert all(isinstance(c.args[0], LimaTransport) for c in mock_update.call_args_list)

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_continues_after_failure_and_exits_nonzero(
        self,
        mock_list: MagicMock,
        mock_update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
            {"name": "vergil.acme.widgets", "status": "Running"},
            {"name": "vergil.acme.gadgets", "status": "Running"},
        ]
        mock_update.side_effect = [
            subprocess.CalledProcessError(1, "uv tool install"),
            None,
            None,
        ]
        result = main(["update", "--all", "--config", str(config_file)])
        assert result == 1
        assert mock_update.call_count == 3
        err = capsys.readouterr().err
        assert "failed to update 1 of 3" in err
        assert "vergil-agent" in err

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_fail_deferred_catches_system_exit(
        self,
        mock_list: MagicMock,
        mock_update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
            {"name": "vergil.acme.widgets", "status": "Running"},
        ]
        mock_update.side_effect = [SystemExit(1), None]
        result = main(["update", "--all", "--config", str(config_file)])
        assert result == 1
        assert mock_update.call_count == 2

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_skips_non_running_vms(
        self,
        mock_list: MagicMock,
        mock_update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
            {"name": "vergil.acme.widgets", "status": "Stopped"},
        ]
        result = main(["update", "--all", "--config", str(config_file)])
        assert result == 0
        mock_update.assert_called_once_with(ANY, None, fallback_tag="v2.0")
        _assert_transport(mock_update, "vergil-agent")
        out = capsys.readouterr().out
        assert "Skipping VM 'vergil.acme.widgets' (status: Stopped)" in out
        assert "1 skipped" in out

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_ignores_vms_of_unconfigured_identities(
        self,
        mock_list: MagicMock,
        mock_update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
            {"name": "random-box", "status": "Running"},
            {"name": "other.acme.widgets", "status": "Running"},
            {"name": "two.tiers", "status": "Running"},  # unparseable instance name
        ]
        result = main(["update", "--all", "--config", str(config_file)])
        assert result == 0
        mock_update.assert_called_once_with(ANY, None, fallback_tag="v2.0")
        _assert_transport(mock_update, "vergil-agent")

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_resolves_fallback_tag_per_identity(
        self,
        mock_list: MagicMock,
        mock_update: MagicMock,
        _ver: MagicMock,
        config_file_multi: Path,
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
            {"name": "audit-agent", "status": "Running"},
        ]
        result = main(["update", "--all", "--config", str(config_file_multi)])
        assert result == 0
        assert mock_update.call_args_list == [
            call(ANY, None, fallback_tag="v2.0"),
            call(ANY, None, fallback_tag="v2.5"),
        ]
        instances = [c.args[0].instance for c in mock_update.call_args_list]
        assert instances == ["vergil-agent", "audit-agent"]
        assert all(isinstance(c.args[0], LimaTransport) for c in mock_update.call_args_list)

    @patch("vergil_tooling.bin.vrg_vm.get_tooling_version", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_explicit_tag_applies_to_every_vm(
        self,
        mock_list: MagicMock,
        mock_update: MagicMock,
        _ver: MagicMock,
        config_file: Path,
    ) -> None:
        mock_list.return_value = [
            {"name": "vergil-agent", "status": "Running"},
            {"name": "vergil.acme.widgets", "status": "Running"},
        ]
        result = main(["update", "--all", "--tag", "v2.1", "--config", str(config_file)])
        assert result == 0
        assert mock_update.call_args_list == [
            call(ANY, "v2.1", fallback_tag="v2.0"),
            call(ANY, "v2.1", fallback_tag="v2.0"),
        ]
        instances = [c.args[0].instance for c in mock_update.call_args_list]
        assert instances == ["vergil-agent", "vergil.acme.widgets"]
        assert all(isinstance(c.args[0], LimaTransport) for c in mock_update.call_args_list)

    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_rejects_workspace_argument(
        self,
        _list: MagicMock,
        mock_update: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = main(["update", "--all", "acme/widgets", "--config", str(config_file)])
        assert result == 2
        mock_update.assert_not_called()
        assert "--all" in capsys.readouterr().err

    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_rejects_identity_flag(
        self,
        _list: MagicMock,
        mock_update: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        result = main(["update", "--all", "--identity", "vergil", "--config", str(config_file)])
        assert result == 2
        mock_update.assert_not_called()
        assert "--all" in capsys.readouterr().err

    @patch("vergil_tooling.bin.vrg_vm.update_tooling")
    @patch("vergil_tooling.bin.vrg_vm.list_vms")
    def test_no_vms_found(
        self,
        mock_list: MagicMock,
        mock_update: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        mock_list.return_value = []
        result = main(["update", "--all", "--config", str(config_file)])
        assert result == 0
        mock_update.assert_not_called()
        assert "No VMs found" in capsys.readouterr().out


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
        mock_update.assert_called_once_with(ANY, fallback_tag="v2.0")
        _assert_transport(mock_update, "vergil-agent")
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

    def test_session_identity_after_workspace(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file_two: Path,
    ) -> None:
        # Regression: --identity placed AFTER the workspace positional used to be
        # swallowed by the REMAINDER `cmd` and handed to the guest shell as a raw
        # `exec --identity ...`. It must now parse as the option.
        main(["session", "--config", str(config_file_two), "vergil-tooling", "--identity", "audit"])
        cmd = mock_exec.call_args[0][1]
        assert "audit-agent" in cmd
        inner = self._inner(mock_exec)
        assert "vrg-vm-resolve-session --identity audit" in inner
        assert "exec --identity" not in inner

    def test_session_identity_before_subcommand(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file_two: Path,
    ) -> None:
        # --identity placed BEFORE the subcommand (global) is honored.
        main(["--identity", "audit", "session", "--config", str(config_file_two), "vergil-tooling"])
        assert "audit-agent" in mock_exec.call_args[0][1]

    def test_session_identity_between_subcommand_and_workspace(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file_two: Path,
    ) -> None:
        # The historically-only-accepted slot must keep working.
        main(["session", "--identity", "audit", "--config", str(config_file_two), "vergil-tooling"])
        assert "audit-agent" in mock_exec.call_args[0][1]

    def test_session_passthrough_unknown_flag_after_dashdash(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        mock_exec: MagicMock,
        config_file: Path,
    ) -> None:
        # Arbitrary claude flags pass through when placed after '--', regardless
        # of leading dashes (nargs="*" captures everything past the separator).
        main(
            [
                "session",
                "--config",
                str(config_file),
                "vergil-tooling",
                "--",
                "claude",
                "--dangerously-skip-permissions",
            ]
        )
        inner = self._inner(mock_exec)
        assert "vrg-vm-resolve-session" in inner
        assert "-- --dangerously-skip-permissions" in inner

    def test_session_unknown_flag_without_dashdash_errors(
        self,
        _age: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _link: MagicMock,
        _exec: MagicMock,
        config_file: Path,
    ) -> None:
        # Without '--', an unknown flag is a clear argparse error rather than being
        # silently swallowed and mis-executed (the old REMAINDER failure mode).
        with pytest.raises(SystemExit):
            main(
                [
                    "session",
                    "--config",
                    str(config_file),
                    "vergil-tooling",
                    "claude",
                    "--dangerously-skip-permissions",
                ]
            )

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


# -- _resolve_target (issue #99) ----------------------------------------------

_REPO_TOML_HEAD = """\
[project]
repository-type = "tooling"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


def _identities(tmp_path: Path, projects: Path) -> Path:
    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent(f"""\
        default_identity = "vergil-user"
        vergil = "v2.0"

        [identities.vergil-user]
        vm_instance = "vergil-user"
        projects_dir = "{projects}"
    """)
    )
    return p


def _make_repo(projects: Path, org: str, repo: str, vm_section: str = "") -> Path:
    repo_dir = projects / org / repo
    repo_dir.mkdir(parents=True)
    (repo_dir / "vergil.toml").write_text(_REPO_TOML_HEAD + vm_section)
    return repo_dir


def _args(config: Path, workspace: str | None) -> argparse.Namespace:
    return argparse.Namespace(config=config, identity=None, workspace=workspace)


_MQ_VM_SECTION = """
[vm]
packages = ["qemu-system-x86"]

[[vm.apt_repos]]
name = "hashicorp"
key_url = "https://apt.releases.hashicorp.com/gpg"
uri = "https://apt.releases.hashicorp.com"
suite = "noble"
components = "main"

[vm.vergil-user]
cpus = 12
memory = "64GiB"
disk = "300GiB"
vagrant_plugins = ["vagrant-libvirt"]
port_forwards = ["3000|10.50.0.2:3000"]
"""


class TestResolveTarget:
    def test_no_workspace_is_base(self, tmp_path: Path) -> None:
        cfg = _identities(tmp_path, tmp_path / "projects")
        target = _resolve_target(_args(cfg, None))
        assert isinstance(target, Target)
        assert target.org is None
        assert target.repo is None
        assert target.instance == "vergil-user"
        assert target.spec.dedicated is False
        assert target.fingerprint == ""

    def test_repo_without_vergil_toml_is_base(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        (projects / "org" / "plain").mkdir(parents=True)  # no vergil.toml
        cfg = _identities(tmp_path, projects)
        target = _resolve_target(_args(cfg, "org/plain"))
        assert target.spec.dedicated is False
        assert target.instance == "vergil-user"

    def test_plain_repo_with_no_vm_section_is_base(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "org", "plain")
        cfg = _identities(tmp_path, projects)
        target = _resolve_target(_args(cfg, "org/plain"))
        assert target.spec.dedicated is False
        assert target.instance == "vergil-user"

    def test_spec_repo_is_dedicated(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION)
        cfg = _identities(tmp_path, projects)
        target = _resolve_target(_args(cfg, "lmf/mq"))
        assert target.org == "lmf"
        assert target.repo == "mq"
        assert target.instance == "vergil-user.lmf.mq"
        assert target.spec.dedicated is True
        assert target.spec.cpus == 12
        assert target.spec.vagrant_plugins == ("vagrant-libvirt",)
        assert target.spec.port_forwards == ("3000|10.50.0.2:3000",)
        assert len(target.spec.apt_repos) == 1
        assert target.fingerprint != ""

    def test_packages_only_repo_is_dedicated(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "org", "pkgonly", '\n[vm]\npackages = ["qemu-system-x86"]\n')
        cfg = _identities(tmp_path, projects)
        target = _resolve_target(_args(cfg, "org/pkgonly"))
        assert target.spec.dedicated is True
        assert target.spec.apt_repos == ()
        assert target.spec.vagrant_plugins == ()
        assert target.spec.port_forwards == ()
        assert target.fingerprint != ""

    def test_one_level_workspace_is_base(self, tmp_path: Path) -> None:
        # The pre-existing 1-level session convention (a bare repo name) stays on base.
        cfg = _identities(tmp_path, tmp_path / "projects")
        target = _resolve_target(_args(cfg, "just-a-repo"))
        assert target.spec.dedicated is False
        assert target.instance == "vergil-user"

    def test_absolute_workspace_is_base(self, tmp_path: Path) -> None:
        cfg = _identities(tmp_path, tmp_path / "projects")
        target = _resolve_target(_args(cfg, "/abs/path"))
        assert target.spec.dedicated is False
        assert target.instance == "vergil-user"

    def test_borrow_redirects_instance_and_spec(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _MQ_VM_SECTION)
        _make_repo(projects, "lmf", "tooling", '\n[vm]\nshared_from = "lmf/lab"\n')
        cfg = _identities(tmp_path, projects)
        target = _resolve_target(_args(cfg, "lmf/tooling"), borrow_allowed=True)
        # Instance + spec resolve to the LENDER, not the borrower.
        assert target.org == "lmf"
        assert target.repo == "lab"
        assert target.instance == "vergil-user.lmf.lab"
        assert target.spec.dedicated is True
        assert target.spec.cpus == 12

    def test_borrow_fingerprint_matches_lender(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _MQ_VM_SECTION)
        _make_repo(projects, "lmf", "tooling", '\n[vm]\nshared_from = "lmf/lab"\n')
        cfg = _identities(tmp_path, projects)
        lender = _resolve_target(_args(cfg, "lmf/lab"))
        borrower = _resolve_target(_args(cfg, "lmf/tooling"), borrow_allowed=True)
        assert borrower.fingerprint == lender.fingerprint
        assert borrower.instance == lender.instance


_LENDER_VM = '\n[vm]\npackages = ["qemu-system-x86"]\ncpus = 12\n'
_BORROW_VM = '\n[vm]\nshared_from = "lmf/lab"\n'


class TestResolveBorrow:
    def test_no_shared_from_returns_none(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _LENDER_VM)
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "lab")
        assert resolve_borrow(identity, "lmf", "lab", requested_vm) is None

    def test_borrow_resolves_to_lender(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _LENDER_VM)
        _make_repo(projects, "lmf", "tooling", _BORROW_VM)
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        borrow = resolve_borrow(identity, "lmf", "tooling", requested_vm)
        assert borrow is not None
        assert (borrow.org, borrow.repo) == ("lmf", "lab")
        assert borrow.stanza.cpus == 12

    def test_self_reference_raises(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "tooling", '\n[vm]\nshared_from = "lmf/tooling"\n')
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        with pytest.raises(BorrowError, match="its own VM"):
            resolve_borrow(identity, "lmf", "tooling", requested_vm)

    def test_missing_lender_raises(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "tooling", _BORROW_VM)  # lmf/lab does not exist
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        with pytest.raises(BorrowError, match="declares no \\[vm\\] stanza"):
            resolve_borrow(identity, "lmf", "tooling", requested_vm)

    def test_lender_without_vm_raises(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab")  # vergil.toml but no [vm]
        _make_repo(projects, "lmf", "tooling", _BORROW_VM)
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        with pytest.raises(BorrowError, match="declares no \\[vm\\] stanza"):
            resolve_borrow(identity, "lmf", "tooling", requested_vm)

    def test_chain_raises(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", '\n[vm]\nshared_from = "lmf/base"\n')
        _make_repo(projects, "lmf", "base", _LENDER_VM)
        _make_repo(projects, "lmf", "tooling", _BORROW_VM)
        cfg = _identities(tmp_path, projects)
        _name, identity, _config = _resolve(_args(cfg, None))
        requested_vm = _read_repo_vm(identity, "lmf", "tooling")
        with pytest.raises(BorrowError, match="chains are not allowed"):
            resolve_borrow(identity, "lmf", "tooling", requested_vm)


class TestBorrowBlocks:
    def _setup(self, tmp_path: Path) -> Path:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "lab", _MQ_VM_SECTION)
        _make_repo(projects, "lmf", "tooling", '\n[vm]\nshared_from = "lmf/lab"\n')
        return _identities(tmp_path, projects)

    @pytest.mark.parametrize("command", ["create", "stop", "restart", "destroy", "rebuild"])
    def test_manage_command_blocked_on_borrower(
        self, command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = self._setup(tmp_path)
        result = main([command, "lmf/tooling", "--config", str(cfg)])
        assert result == 1
        err = capsys.readouterr().err
        assert "borrows the VM of lmf/lab" in err
        assert f"vrg-vm {command} lmf/lab" in err

    def test_update_blocked_on_borrower(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = self._setup(tmp_path)
        result = main(["update", "lmf/tooling", "--config", str(cfg)])
        assert result == 1
        assert "borrows the VM of lmf/lab" in capsys.readouterr().err


_OFF_PLATFORM_VM = """
[vm]
backend = "off-platform"
provider = "gcp"
region = "us-central1"
instance = "n2-standard-8"
volume = "300GiB"
"""


class TestOffPlatformDispatch:
    def test_resolve_target_selects_off_platform_backend(self, tmp_path: Path) -> None:
        # An off-platform repo now resolves to a real OffPlatformBackend carrying the
        # cloud spec; the cloud lifecycle stages themselves land in a later task.
        from vergil_tooling.lib.vm_cloud import OffPlatformBackend

        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "cloud", _OFF_PLATFORM_VM)
        cfg = _identities(tmp_path, projects)
        target = _resolve_target(_args(cfg, "lmf/cloud"))
        assert isinstance(target.backend, OffPlatformBackend)
        assert target.backend.provider_label == "gcp"
        assert target.spec.off_platform


class TestCreateDedicated:
    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_dedicated_create_passes_spec(
        self,
        _status: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        _start: MagicMock,
        _link: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        _stop: MagicMock,
        tmp_path: Path,
    ) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION)
        cfg = _identities(tmp_path, projects)
        template = tmp_path / "tpl.yaml"
        template.write_text("x")
        mock_fetch.return_value = template

        assert main(["create", "lmf/mq", "--config", str(cfg)]) == 0
        assert mock_create.call_args.args[0] == "vergil-user.lmf.mq"
        kwargs = mock_create.call_args.kwargs
        assert kwargs["cpus"] == 12
        assert kwargs["memory"] == "64GiB"
        assert kwargs["packages"] == ["qemu-system-x86"]
        assert kwargs["fingerprint"] != ""
        assert len(kwargs["apt_repos"]) == 1
        assert kwargs["vagrant_plugins"] == ["vagrant-libvirt"]
        assert kwargs["port_forwards"] == ["3000|10.50.0.2:3000"]

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_dedicated_create_packages_only(
        self,
        _status: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        _start: MagicMock,
        _link: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        _stop: MagicMock,
        tmp_path: Path,
    ) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "org", "pkgonly", '\n[vm]\npackages = ["qemu-system-x86"]\n')
        cfg = _identities(tmp_path, projects)
        template = tmp_path / "tpl.yaml"
        template.write_text("x")
        mock_fetch.return_value = template

        assert main(["create", "org/pkgonly", "--config", str(cfg)]) == 0
        kwargs = mock_create.call_args.kwargs
        assert kwargs["packages"] == ["qemu-system-x86"]
        assert kwargs["apt_repos"] == []
        assert kwargs["vagrant_plugins"] == []
        assert kwargs["port_forwards"] == []

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.nested_virt_unsupported_reason", return_value=None)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_dedicated_create_passes_nested(
        self,
        _status: MagicMock,
        mock_support: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        _start: MagicMock,
        _link: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        _stop: MagicMock,
        tmp_path: Path,
    ) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION + "\nnested = true\n")
        cfg = _identities(tmp_path, projects)
        template = tmp_path / "tpl.yaml"
        template.write_text("x")
        mock_fetch.return_value = template

        assert main(["create", "lmf/mq", "--config", str(cfg)]) == 0
        mock_support.assert_called_once()
        assert mock_create.call_args.kwargs["nested"] is True

    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch(
        "vergil_tooling.bin.vrg_vm.nested_virt_unsupported_reason",
        return_value="macOS 15+ on M3-or-later Apple silicon required",
    )
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_aborts_on_unsupported_host_before_build(
        self,
        _status: MagicMock,
        _support: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        _start: MagicMock,
        _link: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION + "\nnested = true\n")
        cfg = _identities(tmp_path, projects)

        assert main(["create", "lmf/mq", "--config", str(cfg)]) == 1
        assert "M3-or-later" in capsys.readouterr().err
        mock_fetch.assert_not_called()
        mock_create.assert_not_called()

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch(
        "vergil_tooling.bin.vrg_vm.nested_virt_unsupported_reason",
        return_value="unsupported host",
    )
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_create_without_nested_skips_host_check(
        self,
        _status: MagicMock,
        mock_support: MagicMock,
        mock_fetch: MagicMock,
        _create: MagicMock,
        _start: MagicMock,
        _link: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        _stop: MagicMock,
        tmp_path: Path,
    ) -> None:
        # An unsupported host must not block profiles that never asked for nested.
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION)
        cfg = _identities(tmp_path, projects)
        template = tmp_path / "tpl.yaml"
        template.write_text("x")
        mock_fetch.return_value = template

        assert main(["create", "lmf/mq", "--config", str(cfg)]) == 0
        mock_support.assert_not_called()

    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch(
        "vergil_tooling.bin.vrg_vm.nested_virt_unsupported_reason",
        return_value="unsupported host",
    )
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_rebuild_aborts_on_unsupported_host_before_destroy(
        self,
        _status: MagicMock,
        mock_delete: MagicMock,
        _support: MagicMock,
        mock_fetch: MagicMock,
        _create: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        _link: MagicMock,
        _copy: MagicMock,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # The preflight must fire before the old VM is destroyed, or an
        # unsupported host turns a rebuild into a destroy-only operation.
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION + "\nnested = true\n")
        cfg = _identities(tmp_path, projects)

        assert main(["rebuild", "lmf/mq", "--config", str(cfg)]) == 1
        assert "unsupported host" in capsys.readouterr().err
        mock_delete.assert_not_called()
        mock_fetch.assert_not_called()

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch("vergil_tooling.bin.vrg_vm.link_claude_dirs")
    @patch("vergil_tooling.bin.vrg_vm.install_tooling")
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.create_vm")
    @patch("vergil_tooling.bin.vrg_vm.fetch_template")
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_dedicated_rebuild_passes_spec(
        self,
        _status: MagicMock,
        _delete: MagicMock,
        mock_fetch: MagicMock,
        mock_create: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _install: MagicMock,
        _link: MagicMock,
        _copy: MagicMock,
        _stop: MagicMock,
        tmp_path: Path,
    ) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION)
        cfg = _identities(tmp_path, projects)
        template = tmp_path / "tpl.yaml"
        template.write_text("x")
        mock_fetch.return_value = template

        assert main(["rebuild", "lmf/mq", "--config", str(cfg)]) == 0
        assert mock_create.call_args.args[0] == "vergil-user.lmf.mq"
        assert mock_create.call_args.kwargs["fingerprint"] != ""


def _target(*, dedicated: bool, under: tuple[str, ...] = (), fingerprint: str = "fp") -> Target:
    ident = Identity(vm_instance="vergil-user", projects_dir="/projects")
    spec = ComposedSpec(
        cpus=12,
        memory="64GiB",
        disk="300GiB",
        stale_days=7,
        packages=(),
        apt_repos=(),
        vagrant_plugins=(),
        port_forwards=(),
        dedicated=dedicated,
        under=under,
    )
    cfg = IdentityConfig(identities={"vergil-user": ident}, default_identity="vergil-user")
    backend = select_backend(spec)
    if dedicated:
        return Target(
            "vergil-user",
            ident,
            cfg,
            "lmf",
            "mq",
            spec,
            "vergil-user.lmf.mq",
            fingerprint,
            backend,
        )
    return Target("vergil-user", ident, cfg, None, None, spec, "vergil-user", "", backend)


class TestPreflight:
    def test_base_passes(self) -> None:
        assert _preflight_target(_target(dedicated=False)) == 0

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_dedicated_missing_aborts(self, _status: MagicMock) -> None:
        assert _preflight_target(_target(dedicated=True)) == 1

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status", return_value="needs-rebuild")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_dedicated_drift_aborts(self, _status: MagicMock, _spec: MagicMock) -> None:
        assert _preflight_target(_target(dedicated=True)) == 1

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status", return_value="unreachable")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_dedicated_unreachable_aborts_without_rebuild(
        self, _status: MagicMock, _spec: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # A Running-but-unreachable VM says nothing about its spec. The gate must
        # abort with a reachability message and must NOT tell the user to rebuild.
        assert _preflight_target(_target(dedicated=True)) == 1
        err = capsys.readouterr().err
        assert "reach" in err.lower()
        assert "rebuild" not in err.lower()

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status", return_value="needs-rebuild")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_dedicated_stopped_defers_spec_check(
        self, _status: MagicMock, mock_spec: MagicMock
    ) -> None:
        # A stopped guest's fingerprint lives at /etc/vergil/vm-spec.fingerprint
        # and is only readable over `limactl shell` while the VM runs. The drift
        # gate therefore cannot run pre-start; it must not block start (else every
        # stopped dedicated VM is un-startable and falsely told to rebuild). The
        # post-start spec-check stage performs the real check once the VM is up.
        assert _preflight_target(_target(dedicated=True)) == 0
        mock_spec.assert_not_called()

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status", return_value="ok")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_dedicated_ok_passes(self, _status: MagicMock, _spec: MagicMock) -> None:
        assert _preflight_target(_target(dedicated=True)) == 0

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status", return_value="ok")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Running")
    def test_dedicated_under_warns(
        self, _status: MagicMock, _spec: MagicMock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _preflight_target(_target(dedicated=True, under=("mem",))) == 0
        assert "under-provisioned" in capsys.readouterr().err

    def test_warn_under_noop_when_empty(self, capsys: pytest.CaptureFixture[str]) -> None:
        _warn_under(_target(dedicated=True, under=()))
        assert capsys.readouterr().err == ""

    def test_target_ref_dedicated_and_base(self) -> None:
        assert _target_ref(_target(dedicated=True)) == "lmf/mq"
        assert _target_ref(_target(dedicated=False)) == "--identity vergil-user"


class TestDedicatedGateThroughCommands:
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_session_aborts_when_dedicated_missing(
        self, _status: MagicMock, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION)
        cfg = _identities(tmp_path, projects)
        # session uses REMAINDER for `cmd`, so --config must precede the workspace.
        assert main(["session", "--config", str(cfg), "lmf/mq"]) == 1


class TestLifecyclePositional:
    @patch("vergil_tooling.bin.vrg_vm.delete_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_destroy_targets_dedicated_instance(
        self, _status: MagicMock, mock_delete: MagicMock, tmp_path: Path
    ) -> None:
        # No repo/spec needed: an orphan (repo dropped [vm]) is still reachable by name.
        cfg = _identities(tmp_path, tmp_path / "projects")
        assert main(["destroy", "lmf/mq", "--config", str(cfg)]) == 0
        mock_delete.assert_called_once_with("vergil-user.lmf.mq")

    @patch("vergil_tooling.bin.vrg_vm.stop_vm")
    def test_stop_targets_dedicated_instance(self, mock_stop: MagicMock, tmp_path: Path) -> None:
        cfg = _identities(tmp_path, tmp_path / "projects")
        assert main(["stop", "lmf/mq", "--config", str(cfg)]) == 0
        mock_stop.assert_called_once_with("vergil-user.lmf.mq")


class TestDiscoverDedicated:
    def test_classifies_instances_via_targeted_reads(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "org", "present", '\n[vm]\npackages = ["x"]\n')
        _make_repo(projects, "org", "nospec", "")  # valid vergil.toml, no [vm]
        instances = [
            "vergil-user.org.present",  # instance + spec -> present
            "vergil-user.org.gone",  # instance, no repo -> orphaned
            "vergil-user.org.nospec",  # repo without [vm] -> orphaned
            "vergil-user",  # base instance -> ignored (org is None)
            "weird.name",  # unparseable (2 tiers) -> ignored
            "vergil-audit.org.present",  # other identity -> ignored
        ]
        rows = discover_dedicated("vergil-user", instances, str(projects))
        by_repo = {r.repo: r.state for r in rows}
        assert by_repo == {
            "present": "present",
            "gone": "orphaned",
            "nospec": "orphaned",
        }
        assert all(isinstance(r, DedicatedRow) for r in rows)
        assert capsys.readouterr().err == ""  # clean configs -> no warnings

    def test_present_row_carries_parsed_stanza(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "org", "present", '\n[vm]\npackages = ["x"]\n')
        rows = discover_dedicated("vergil-user", ["vergil-user.org.present"], str(projects))
        assert rows[0].stanza is not None
        assert rows[0].stanza.packages == ["x"]

    def test_broken_config_is_loud_and_conservative(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        projects = tmp_path / "projects"
        broken = projects / "org" / "broken"
        broken.mkdir(parents=True)
        (broken / "vergil.toml").write_text("[invalid toml")  # ConfigError on read
        rows = discover_dedicated("vergil-user", ["vergil-user.org.broken"], str(projects))
        assert [(r.repo, r.state, r.stanza) for r in rows] == [("broken", "present", None)]
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert str(broken / "vergil.toml") in err
        assert "vergil-user.org.broken" in err

    def test_no_instances_means_no_tree_scan(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        # Spec-bearing repos with no instance are NOT enumerated (no tree walk);
        # the session/start preflight gate owns that signal.
        _make_repo(projects, "org", "todo", '\n[vm]\npackages = ["x"]\n')
        assert discover_dedicated("vergil-user", [], str(projects)) == []

    def test_nonexistent_projects_dir(self, tmp_path: Path) -> None:
        rows = discover_dedicated("vergil-user", ["vergil-user.org.gone"], str(tmp_path / "nope"))
        assert [(r.repo, r.state) for r in rows] == [("gone", "orphaned")]


class TestListRows:
    def _identity(self, projects: Path, **over: Any) -> Identity:
        base: dict[str, Any] = {
            "vm_instance": "vergil-user",
            "projects_dir": str(projects),
            "cpus": 4,
            "memory": "4GiB",
            "disk": "50GiB",
        }
        base.update(over)
        return Identity(**base)

    def _row(self, rows: list[dict[str, object]], scope: str) -> dict[str, object]:
        return next(r for r in rows if r["scope"] == scope)

    def _present(self, projects: Path) -> list[DedicatedRow]:
        """One present lmf/mq row with its stanza threaded through discovery."""
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION)
        return discover_dedicated("vergil-user", ["vergil-user.lmf.mq"], str(projects))

    @patch("vergil_tooling.bin.vrg_vm.spec_fingerprint", return_value="fp")
    def test_base_and_present_running_ok(self, _fp: MagicMock, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        ident = self._identity(projects)
        dedic = self._present(projects)
        status = {"vergil-user": "Running", "vergil-user.lmf.mq": "Running"}
        probes = {"vergil-user": (2, 1, None), "vergil-user.lmf.mq": (2, 1, "fp")}
        rows = _list_rows("vergil-user", ident, dedic, status, probes)
        base = self._row(rows, "base")
        ded = self._row(rows, "lmf/mq")
        assert base["cpus"] == 4
        assert base["agents"] == "2"
        assert base["humans"] == "1"
        assert ded["cpus"] == 12
        assert ded["spec"] == "ok"

    @patch("vergil_tooling.bin.vrg_vm.spec_fingerprint", return_value="fp")
    def test_present_running_needs_rebuild(self, _fp: MagicMock, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        ident = self._identity(projects)
        dedic = self._present(projects)
        status = {"vergil-user.lmf.mq": "Running"}
        probes = {"vergil-user.lmf.mq": (0, 0, "stale")}
        rows = _list_rows("vergil-user", ident, dedic, status, probes)
        assert self._row(rows, "lmf/mq")["spec"] == "NEEDS-REBUILD"

    @patch("vergil_tooling.bin.vrg_vm.spec_fingerprint", return_value="fp")
    def test_present_running_missing_fingerprint_needs_rebuild(
        self, _fp: MagicMock, tmp_path: Path
    ) -> None:
        projects = tmp_path / "projects"
        ident = self._identity(projects)
        dedic = self._present(projects)
        status = {"vergil-user.lmf.mq": "Running"}
        probes = {"vergil-user.lmf.mq": (0, 0, None)}
        rows = _list_rows("vergil-user", ident, dedic, status, probes)
        assert self._row(rows, "lmf/mq")["spec"] == "NEEDS-REBUILD"

    @patch("vergil_tooling.bin.vrg_vm.spec_fingerprint", return_value="fp")
    def test_present_running_under(self, _fp: MagicMock, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        ident = self._identity(projects, overrides={("lmf", "mq"): {"memory": "32GiB"}})
        dedic = self._present(projects)
        status = {"vergil-user.lmf.mq": "Running"}
        probes = {"vergil-user.lmf.mq": (0, 0, "fp")}
        rows = _list_rows("vergil-user", ident, dedic, status, probes)
        assert "under (mem)" in str(self._row(rows, "lmf/mq")["spec"])

    def test_present_not_running_is_ok(self, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        ident = self._identity(projects)
        dedic = self._present(projects)
        rows = _list_rows("vergil-user", ident, dedic, {}, {})  # nothing running
        ded = self._row(rows, "lmf/mq")
        assert ded["spec"] == "ok"
        assert ded["agents"] == "—"

    def test_present_without_stanza_uses_base(self, tmp_path: Path) -> None:
        # An unverified present row (broken config -> stanza None) falls back
        # to the base footprint.
        ident = self._identity(tmp_path / "projects")
        dedic = [DedicatedRow("lmf", "mq", "vergil-user.lmf.mq", "present", None)]
        rows = _list_rows("vergil-user", ident, dedic, {}, {})
        assert self._row(rows, "lmf/mq")["cpus"] == 4  # stanza None -> base footprint

    def test_orphaned(self, tmp_path: Path) -> None:
        ident = self._identity(tmp_path / "projects")
        dedic = [DedicatedRow("o", "gone", "vergil-user.o.gone", "orphaned")]
        rows = _list_rows("vergil-user", ident, dedic, {}, {})
        assert self._row(rows, "o/gone")["spec"] == "orphaned"

    def test_orphaned_running_shows_occupancy(self, tmp_path: Path) -> None:
        ident = self._identity(tmp_path / "projects")
        dedic = [DedicatedRow("o", "gone", "vergil-user.o.gone", "orphaned")]
        status = {"vergil-user.o.gone": "Running"}
        probes = {"vergil-user.o.gone": (1, 0, None)}
        rows = _list_rows("vergil-user", ident, dedic, status, probes)
        assert self._row(rows, "o/gone")["agents"] == "1"

    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="")
    def test_start_aborts_when_dedicated_missing(self, _status: MagicMock, tmp_path: Path) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION)
        cfg = _identities(tmp_path, projects)
        assert main(["start", "lmf/mq", "--config", str(cfg)]) == 1


class TestProbeRunning:
    def _identity(self, tmp_path: Path) -> Identity:
        return Identity(vm_instance="vergil-user", projects_dir=str(tmp_path / "projects"))

    @patch("vergil_tooling.bin.vrg_vm.vm_probe")
    def test_probes_running_only_with_fingerprint_for_present(
        self, mock_probe: MagicMock, tmp_path: Path
    ) -> None:
        mock_probe.side_effect = lambda _transport, *, fingerprint=False: (
            (1, 0, "fp") if fingerprint else (1, 0, None)
        )
        identities = {"vergil-user": self._identity(tmp_path)}
        discovered = {
            "vergil-user": [
                DedicatedRow("lmf", "mq", "vergil-user.lmf.mq", "present", None),
                DedicatedRow("o", "gone", "vergil-user.o.gone", "orphaned"),
                DedicatedRow("o", "off", "vergil-user.o.off", "present", None),
            ]
        }
        status = {
            "vergil-user": "Running",
            "vergil-user.lmf.mq": "Running",
            "vergil-user.o.gone": "Running",
            "vergil-user.o.off": "Stopped",
        }
        probes = _probe_running(identities, discovered, status)
        assert probes == {
            "vergil-user": (1, 0, None),
            "vergil-user.lmf.mq": (1, 0, "fp"),
            "vergil-user.o.gone": (1, 0, None),
        }
        wants = {c.args[0].instance: c.kwargs["fingerprint"] for c in mock_probe.call_args_list}
        assert wants == {
            "vergil-user": False,  # base: occupancy only
            "vergil-user.lmf.mq": True,  # present dedicated: combined probe
            "vergil-user.o.gone": False,  # orphaned: no spec to compare
        }
        assert all(isinstance(c.args[0], LimaTransport) for c in mock_probe.call_args_list)

    @patch("vergil_tooling.bin.vrg_vm.vm_probe")
    def test_nothing_running_probes_nothing(self, mock_probe: MagicMock, tmp_path: Path) -> None:
        identities = {"vergil-user": self._identity(tmp_path)}
        discovered: dict[str, list[DedicatedRow]] = {"vergil-user": []}
        assert _probe_running(identities, discovered, {"vergil-user": "Stopped"}) == {}
        mock_probe.assert_not_called()

    @patch("vergil_tooling.bin.vrg_vm.vm_probe", side_effect=RuntimeError("boom"))
    def test_probe_errors_propagate(self, _probe: MagicMock, tmp_path: Path) -> None:
        # Parallelization must not swallow new error modes: anything beyond
        # vm_probe's documented (0, 0, None) contract surfaces to the caller.
        identities = {"vergil-user": self._identity(tmp_path)}
        discovered: dict[str, list[DedicatedRow]] = {"vergil-user": []}
        with pytest.raises(RuntimeError, match="boom"):
            _probe_running(identities, discovered, {"vergil-user": "Running"})


def _lifecycle_target(tmp_path: Path) -> Any:
    from vergil_tooling.lib.identity import load_config
    from vergil_tooling.lib.vm_spec import compose_vm_spec

    p = tmp_path / "identities.toml"
    p.write_text(
        textwrap.dedent("""\
        default_identity = "vergil"
        vergil = "v2.0"

        [identities.vergil]
        vm_instance = "vergil-agent"
        projects_dir = "/home/user/projects"
    """)
    )
    config = load_config(p)
    identity = config.identities["vergil"]
    spec = compose_vm_spec(
        identity="vergil",
        base={"cpus": 4, "memory": "4GiB", "disk": "50GiB"},
        stanza=None,
        override=None,
    )
    backend = select_backend(spec)
    return Target("vergil", identity, config, None, None, spec, "vergil-agent", "", backend)


class TestLifecycleStages:
    def test_create_stage_order_and_modes(self) -> None:
        from vergil_tooling.bin.vrg_vm import _create_stages

        stages = _create_stages()
        assert [s.name for s in stages] == [
            "fetch-template",
            "create",
            "start",
            "link-config",
            "credentials",
            "tooling",
            "cycle-ssh",
        ]
        assert all(s.mode == "fail_fast" for s in stages)

    def test_start_stage_order_and_modes(self) -> None:
        from vergil_tooling.bin.vrg_vm import _start_stages

        stages = _start_stages()
        assert [s.name for s in stages] == [
            "start",
            "spec-check",
            "credentials",
            "copy-config",
            "update-tooling",
            "update-plugins",
        ]
        modes = {s.name: s.mode for s in stages}
        # spec-check verifies the freshly-booted guest's fingerprint; it is
        # non-fatal (warn) so a drifted-but-running VM stays usable, and it sits
        # immediately after start so the warning surfaces before later work.
        # update-plugins is warn for the same reason as update-tooling: a failed
        # plugin refresh must not abort a usable session.
        assert modes == {
            "start": "fail_fast",
            "spec-check": "warn",
            "credentials": "fail_fast",
            "copy-config": "fail_fast",
            "update-tooling": "warn",
            "update-plugins": "warn",
        }

    def test_rebuild_stage_order_and_modes(self) -> None:
        from vergil_tooling.bin.vrg_vm import _rebuild_stages

        stages = _rebuild_stages()
        assert [s.name for s in stages] == [
            "destroy",
            "fetch-template",
            "create",
            "start",
            "credentials",
            "tooling",
            "copy-config",
            "update-plugins",
            "cycle-ssh",
        ]
        # All fail_fast except update-plugins, which is warn so a failed plugin
        # refresh does not abort the rebuild.
        modes = {s.name: s.mode for s in stages}
        assert modes["update-plugins"] == "warn"
        assert all(s.mode == "fail_fast" for s in stages if s.name != "update-plugins")

    def test_st_create_requires_template(self, tmp_path: Path) -> None:
        from vergil_tooling.bin.vrg_vm import _LifecycleState, _st_create

        state = _LifecycleState(target=_lifecycle_target(tmp_path))
        with pytest.raises(RuntimeError, match="fetch-template did not run"):
            _st_create(state)

    def test_st_update_tooling_resolves_fallback(self, tmp_path: Path) -> None:
        from vergil_tooling.bin.vrg_vm import _LifecycleState, _st_update_tooling

        state = _LifecycleState(target=_lifecycle_target(tmp_path))
        with patch("vergil_tooling.bin.vrg_vm.update_tooling") as m_update:
            _st_update_tooling(state)
        m_update.assert_called_once_with(ANY, fallback_tag="v2.0")
        _assert_transport(m_update, "vergil-agent")

    def test_st_cycle_ssh_stops_then_starts(self, tmp_path: Path) -> None:
        # The cycle must be a stop *then* a start: only a full power cycle is
        # guaranteed to drop the stale boot-time SSH ControlMaster (#1463).
        from vergil_tooling.bin.vrg_vm import _LifecycleState, _st_cycle_ssh

        state = _LifecycleState(target=_lifecycle_target(tmp_path), timeout="45m")
        manager = MagicMock()
        with (
            patch("vergil_tooling.bin.vrg_vm.stop_vm") as m_stop,
            patch("vergil_tooling.bin.vrg_vm.start_vm") as m_start,
        ):
            manager.attach_mock(m_stop, "stop_vm")
            manager.attach_mock(m_start, "start_vm")
            _st_cycle_ssh(state)
        assert manager.mock_calls == [
            call.stop_vm("vergil-agent"),
            call.start_vm("vergil-agent", timeout="45m"),
        ]


class TestSpecCheckStage:
    """The post-start drift check: now the guest is up, its stamped fingerprint
    is finally readable. Warn-mode — it raises to surface a non-fatal ⚠, never
    aborts the start, and is skipped entirely for non-dedicated (base) targets."""

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status", return_value="needs-rebuild")
    def test_warns_on_drift(self, _spec: MagicMock) -> None:
        from vergil_tooling.bin.vrg_vm import (
            SpecDriftError,
            _LifecycleState,
            _st_spec_check,
        )

        with pytest.raises(SpecDriftError) as exc:
            _st_spec_check(_LifecycleState(target=_target(dedicated=True)))
        # The warning must carry the actionable rebuild command.
        assert "rebuild" in str(exc.value).lower()

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status", return_value="unreachable")
    def test_warns_unreachable_not_drift(self, _spec: MagicMock) -> None:
        from vergil_tooling.bin.vrg_vm import (
            SpecCheckUnreachableError,
            _LifecycleState,
            _st_spec_check,
        )

        # Post-start, a VM we cannot reach must warn about reachability — never
        # raise drift or suggest a rebuild (the spec was never read).
        with pytest.raises(SpecCheckUnreachableError) as exc:
            _st_spec_check(_LifecycleState(target=_target(dedicated=True)))
        assert "reach" in str(exc.value).lower()
        assert "rebuild" not in str(exc.value).lower()

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status", return_value="ok")
    def test_silent_when_in_spec(self, _spec: MagicMock) -> None:
        from vergil_tooling.bin.vrg_vm import _LifecycleState, _st_spec_check

        # In spec -> no raise (stage records "ok").
        _st_spec_check(_LifecycleState(target=_target(dedicated=True)))

    @patch("vergil_tooling.bin.vrg_vm.vm_spec_status")
    def test_skips_base_target(self, mock_spec: MagicMock) -> None:
        from vergil_tooling.bin.vrg_vm import _LifecycleState, _st_spec_check

        # A base (non-dedicated) box carries no per-repo spec to drift from.
        _st_spec_check(_LifecycleState(target=_target(dedicated=False)))
        mock_spec.assert_not_called()


class TestRunLifecycle:
    def _args(self) -> argparse.Namespace:
        return argparse.Namespace(output_window=5, output_format="plain")

    def test_cleans_up_template_on_success(self, tmp_path: Path) -> None:
        from vergil_tooling.bin.vrg_vm import _LifecycleState, _run_lifecycle
        from vergil_tooling.lib.progress import Stage

        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        state = _LifecycleState(target=_lifecycle_target(tmp_path), template=template)
        rc = _run_lifecycle(
            "create",
            state,
            [Stage("noop", lambda ctx: None, mode="fail_fast")],
            self._args(),
        )
        assert rc == 0
        assert not template.exists()

    def test_cleans_up_template_on_failure(self, tmp_path: Path) -> None:
        from vergil_tooling.bin.vrg_vm import _LifecycleState, _run_lifecycle
        from vergil_tooling.lib.progress import Stage

        def _boom(ctx: object) -> None:
            msg = "boom"
            raise RuntimeError(msg)

        template = tmp_path / "template.yaml"
        template.write_text("cpus: 4")
        state = _LifecycleState(target=_lifecycle_target(tmp_path), template=template)
        rc = _run_lifecycle("create", state, [Stage("bad", _boom, mode="fail_fast")], self._args())
        assert rc == 1
        assert not template.exists()

    @patch("vergil_tooling.bin.vrg_vm.update_plugins")
    @patch("vergil_tooling.bin.vrg_vm.copy_claude_config")
    @patch(
        "vergil_tooling.bin.vrg_vm.update_tooling",
        side_effect=RuntimeError("update failed"),
    )
    @patch("vergil_tooling.bin.vrg_vm.inject_credentials")
    @patch("vergil_tooling.bin.vrg_vm.start_vm")
    @patch("vergil_tooling.bin.vrg_vm.vm_age_days", return_value=1.0)
    @patch("vergil_tooling.bin.vrg_vm.vm_status", return_value="Stopped")
    def test_start_update_failure_is_warning_not_error(
        self,
        _status: MagicMock,
        _age: MagicMock,
        _start: MagicMock,
        _inject: MagicMock,
        _update: MagicMock,
        _copy: MagicMock,
        _plugins: MagicMock,
        config_file: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # warn-mode stage: a failed tooling update must not fail the start.
        result = main(["start", "--config", str(config_file), "--output-format", "plain"])
        assert result == 0
        out = capsys.readouterr().out
        assert "⚠  warnings (non-fatal):" in out
        assert "update-tooling — RuntimeError: update failed" in out


class TestLogRoot:
    def test_inside_repo_uses_toplevel(self, tmp_path: Path) -> None:
        completed = MagicMock(returncode=0, stdout=f"{tmp_path}\n")
        with patch("vergil_tooling.bin.vrg_vm.subprocess.run", return_value=completed):
            assert _REAL_LOG_ROOT() == tmp_path

    def test_outside_repo_falls_back_to_home(self) -> None:
        completed = MagicMock(returncode=128, stdout="")
        with patch("vergil_tooling.bin.vrg_vm.subprocess.run", return_value=completed):
            assert _REAL_LOG_ROOT() == Path.home()


class TestPluginStage:
    def test_start_pipeline_includes_update_plugins(self) -> None:
        from vergil_tooling.bin.vrg_vm import _start_stages

        names = [s.name for s in _start_stages()]
        assert "update-plugins" in names
        # Runs after tooling, in warn mode.
        assert names.index("update-plugins") > names.index("update-tooling")
        stage = next(s for s in _start_stages() if s.name == "update-plugins")
        assert stage.mode == "warn"

    def test_rebuild_pipeline_includes_update_plugins(self) -> None:
        from vergil_tooling.bin.vrg_vm import _rebuild_stages

        names = [s.name for s in _rebuild_stages()]
        assert "update-plugins" in names
        stage = next(s for s in _rebuild_stages() if s.name == "update-plugins")
        assert stage.mode == "warn"


# --- Off-platform (cloud) lifecycle ------------------------------------------


@pytest.fixture()
def _cloud_repo(tmp_path: Path) -> Path:
    """An identities.toml whose lmf/cloud repo declares an off-platform [vm]."""
    projects = tmp_path / "projects"
    _make_repo(projects, "lmf", "cloud", _OFF_PLATFORM_VM)
    return _identities(tmp_path, projects)


class _CloudPatches:
    """Bundle of patches that stub the entire cloud engine + backend surface.

    Patches ``vm_cloud.*`` module functions (vrg_vm calls them via the module),
    the credential/tooling helpers imported by name into vrg_vm, and the
    OffPlatformBackend methods that talk to gcloud/tofu.
    """

    def __init__(self, state_dir: Path, *, status: str = "") -> None:
        self.state_dir = state_dir
        self.status = status

    def __enter__(self) -> dict[str, MagicMock]:
        self._ctx = []
        mocks: dict[str, MagicMock] = {}

        _unset = object()

        def _patch(target: str, return_value: object = _unset) -> MagicMock:
            if return_value is _unset:
                p = patch(target)
            else:
                p = patch(target, return_value=return_value)
            mock = p.start()
            mocks[target.rsplit(".", 1)[-1]] = mock
            self._ctx.append(p)
            return mock

        modules_root = self.state_dir / "modules"
        modules_root.mkdir(parents=True, exist_ok=True)
        _patch("vergil_tooling.bin.vrg_vm.vm_cloud.fetch_modules", return_value=modules_root)
        _patch(
            "vergil_tooling.bin.vrg_vm.vm_cloud.apply_volume",
            return_value=("vol-123", "us-central1-a"),
        )
        _patch(
            "vergil_tooling.bin.vrg_vm.vm_cloud.apply_vm",
            return_value={"host": "cloud-host"},
        )
        _patch("vergil_tooling.bin.vrg_vm.vm_cloud.await_readiness")
        _patch("vergil_tooling.bin.vrg_vm.vm_cloud.bootstrap_volume")
        _patch("vergil_tooling.bin.vrg_vm.vm_cloud.link_cloud_claude_dirs")
        _patch("vergil_tooling.bin.vrg_vm.vm_cloud.preflight")
        _patch("vergil_tooling.bin.vrg_vm.vm_cloud.destroy_vm")
        _patch("vergil_tooling.bin.vrg_vm.vm_cloud.destroy_volume")
        _patch("vergil_tooling.bin.vrg_vm.inject_credentials")
        _patch("vergil_tooling.bin.vrg_vm.install_tooling")
        _patch(
            "vergil_tooling.lib.vm_cloud.OffPlatformBackend.state_dir",
            return_value=self.state_dir,
        )
        _patch(
            "vergil_tooling.lib.vm_cloud.OffPlatformBackend.status",
            return_value=self.status,
        )
        _patch(
            "vergil_tooling.lib.vm_cloud.OffPlatformBackend.transport",
            return_value=MagicMock(),
        )
        return mocks

    def __exit__(self, *exc: object) -> None:
        for p in reversed(self._ctx):
            p.stop()


class TestCloudStageGuards:
    def _state(self, tmp_path: Path) -> _CloudState:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "cloud", _OFF_PLATFORM_VM)
        cfg = _identities(tmp_path, projects)
        target = _resolve_target(_args(cfg, "lmf/cloud"))
        return _CloudState(
            target=target,
            backend=_cloud_backend(target),
            state_dir=tmp_path / "state",
        )

    def test_require_modules_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="fetch-modules did not run"):
            _cs_tofu_volume(self._state(tmp_path))

    def test_require_transport_raises(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="tofu-vm did not run"):
            _cs_credentials(self._state(tmp_path))


class TestCloudCreate:
    def test_cloud_create_happy_path(self, _cloud_repo: Path, tmp_path: Path) -> None:
        with _CloudPatches(tmp_path / "state") as m:
            result = main(
                ["create", "lmf/cloud", "--config", str(_cloud_repo), "--output-format", "plain"]
            )
        assert result == 0
        m["preflight"].assert_called_once()
        m["fetch_modules"].assert_called_once_with("v2.0")
        m["apply_volume"].assert_called_once()
        m["apply_vm"].assert_called_once()
        m["await_readiness"].assert_called_once()
        m["inject_credentials"].assert_called_once()
        m["install_tooling"].assert_called_once()
        m["bootstrap_volume"].assert_called_once()
        m["link_cloud_claude_dirs"].assert_called_once()
        m["destroy_vm"].assert_not_called()

    def test_cloud_create_cleans_up_modules(self, _cloud_repo: Path, tmp_path: Path) -> None:
        state = tmp_path / "state"
        with _CloudPatches(state):
            main(["create", "lmf/cloud", "--config", str(_cloud_repo), "--output-format", "plain"])
        # The fetched modules' parent temp dir is removed in the finally.
        assert not (state / "modules").exists()

    def test_cloud_create_concurrency_guard(
        self, _cloud_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _CloudPatches(tmp_path / "state", status="Running") as m:
            result = main(
                ["create", "lmf/cloud", "--config", str(_cloud_repo), "--output-format", "plain"]
            )
        assert result == 1
        assert "already exists" in capsys.readouterr().err
        m["apply_volume"].assert_not_called()


class TestCloudDestroy:
    def test_cloud_destroy_calls_destroy_vm(
        self, _cloud_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _CloudPatches(tmp_path / "state") as m:
            result = main(["destroy", "lmf/cloud", "--config", str(_cloud_repo)])
        assert result == 0
        m["destroy_vm"].assert_called_once()
        m["destroy_volume"].assert_not_called()
        assert "destroyed" in capsys.readouterr().out


class TestCloudRebuild:
    def test_cloud_rebuild_destroys_then_builds(self, _cloud_repo: Path, tmp_path: Path) -> None:
        # Rebuild is allowed even when Running: it destroys the disposable VM first.
        with _CloudPatches(tmp_path / "state", status="Running") as m:
            result = main(
                ["rebuild", "lmf/cloud", "--config", str(_cloud_repo), "--output-format", "plain"]
            )
        assert result == 0
        m["destroy_vm"].assert_called_once()
        m["apply_volume"].assert_called_once()
        m["apply_vm"].assert_called_once()


class TestCloudSession:
    def test_cloud_session_uses_iap_transport(self, _cloud_repo: Path, tmp_path: Path) -> None:
        transport = MagicMock()
        with _CloudPatches(tmp_path / "state") as m:
            m["transport"].return_value = transport
            main(["session", "lmf/cloud", "--config", str(_cloud_repo)])
        m["preflight"].assert_called_once()
        transport.exec_session.assert_called_once()
        kwargs = transport.exec_session.call_args.kwargs
        assert kwargs["workdir"] == "/vergil/projects/lmf/cloud"
        assert "vrg-vm-resolve-session" in kwargs["inner"]


class TestCloudUpdate:
    def test_cloud_update_delegates_to_rebuild(self, _cloud_repo: Path, tmp_path: Path) -> None:
        with _CloudPatches(tmp_path / "state") as m:
            result = main(
                ["update", "lmf/cloud", "--config", str(_cloud_repo), "--output-format", "plain"]
            )
        assert result == 0
        # update -> rebuild -> destroy_vm + re-apply
        m["destroy_vm"].assert_called_once()
        m["apply_vm"].assert_called_once()


class TestCloudStopStartUnsupported:
    @pytest.mark.parametrize("verb", ["stop", "restart", "start"])
    def test_ephemeral_message(
        self,
        verb: str,
        _cloud_repo: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with _CloudPatches(tmp_path / "state"):
            result = main([verb, "lmf/cloud", "--config", str(_cloud_repo)])
        assert result == 1
        assert "ephemeral" in capsys.readouterr().err


class TestDestroyVolume:
    def test_requires_off_platform(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        projects = tmp_path / "projects"
        _make_repo(projects, "lmf", "mq", _MQ_VM_SECTION)  # Lima dedicated repo
        cfg = _identities(tmp_path, projects)
        result = main(["destroy-volume", "lmf/mq", "--config", str(cfg), "--yes"])
        assert result == 1
        assert "only for off-platform" in capsys.readouterr().err

    def test_confirmation_mismatch_aborts(
        self, _cloud_repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with _CloudPatches(tmp_path / "state") as m, patch("builtins.input", return_value="nope"):
            result = main(["destroy-volume", "lmf/cloud", "--config", str(_cloud_repo)])
        assert result == 1
        assert "did not match" in capsys.readouterr().err
        m["destroy_volume"].assert_not_called()

    def test_confirmation_match_destroys(self, _cloud_repo: Path, tmp_path: Path) -> None:
        with (
            _CloudPatches(tmp_path / "state") as m,
            patch("builtins.input", return_value="lmf/cloud"),
        ):
            result = main(["destroy-volume", "lmf/cloud", "--config", str(_cloud_repo)])
        assert result == 0
        m["destroy_volume"].assert_called_once()

    def test_yes_flag_skips_prompt(self, _cloud_repo: Path, tmp_path: Path) -> None:
        with _CloudPatches(tmp_path / "state") as m:
            result = main(["destroy-volume", "lmf/cloud", "--config", str(_cloud_repo), "--yes"])
        assert result == 0
        m["destroy_volume"].assert_called_once()


class TestCloudList:
    def test_backend_column_present(self, config_file: Path) -> None:
        from vergil_tooling.bin.vrg_vm import _cloud_list_rows

        with (
            patch("vergil_tooling.bin.vrg_vm.list_vms", return_value=[]),
            patch("vergil_tooling.bin.vrg_vm._cloud_list_rows", return_value=[]),
        ):
            assert _cloud_list_rows is not None  # imported symbol exists
            # exercise the printed header
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                result = main(["list", "--config", str(config_file)])
        assert result == 0
        assert "BACKEND" in buf.getvalue()
        assert "local" in buf.getvalue()

    def test_cloud_rows_enumerated_with_degraded_status(
        self, config_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Build a fake tofu state tree: ~/.config/vergil/tofu/<key>/<provider>/volume.tfstate
        fake_home = tmp_path / "home"
        tofu = fake_home / ".config" / "vergil" / "tofu" / "vergil-lmf-cloud" / "gcp"
        tofu.mkdir(parents=True)
        (tofu / "volume.tfstate").write_text("{}")
        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with (
            patch("vergil_tooling.bin.vrg_vm.list_vms", return_value=[]),
            # No persisted zone -> read_zone raises -> degraded placeholder.
            redirect_stdout(buf),
        ):
            result = main(["list", "--config", str(config_file)])
        assert result == 0
        out = buf.getvalue()
        assert "vergil-lmf-cloud" in out
        assert "gcp" in out
        assert "unknown (no gcp creds)" in out

    def test_cloud_status_reads_live_status(self, tmp_path: Path) -> None:
        from vergil_tooling.bin.vrg_vm import _cloud_status

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch("vergil_tooling.bin.vrg_vm.vm_cloud.read_zone", return_value="us-central1-a"):
            completed = MagicMock(stdout="RUNNING\n")
            with patch("vergil_tooling.bin.vrg_vm.subprocess.run", return_value=completed):
                assert _cloud_status(state_dir, "key") == "RUNNING"

    def test_cloud_status_gcloud_failure_is_empty(self, tmp_path: Path) -> None:
        from vergil_tooling.bin.vrg_vm import _cloud_status

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with (
            patch("vergil_tooling.bin.vrg_vm.vm_cloud.read_zone", return_value="us-central1-a"),
            patch(
                "vergil_tooling.bin.vrg_vm.subprocess.run",
                side_effect=subprocess.CalledProcessError(1, "gcloud"),
            ),
        ):
            assert _cloud_status(state_dir, "key") == ""

    def test_cloud_list_rows_empty_when_no_tofu_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vergil_tooling.bin.vrg_vm import _cloud_list_rows

        monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path / "empty"))
        assert _cloud_list_rows() == []


class TestCloudUnderProvision:
    def _cloud_repo_instance(self, tmp_path: Path, instance: str, *, cpus: int) -> Path:
        projects = tmp_path / "projects"
        section = textwrap.dedent(f"""
            [vm]
            backend = "off-platform"
            provider = "gcp"
            region = "us-central1"
            instance = "{instance}"
            volume = "300GiB"

            [vm.vergil-user]
            cpus = {cpus}
            memory = "256GiB"
        """)
        _make_repo(projects, "lmf", "cloud", section)
        return _identities(tmp_path, projects)

    def test_warns_for_undersized_known_instance(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # n2-standard-8 is (8, 32); declared 16 cpus / 256GiB is bigger -> warn.
        cfg = self._cloud_repo_instance(tmp_path, "n2-standard-8", cpus=16)
        transport = MagicMock()
        with _CloudPatches(tmp_path / "state") as m:
            m["transport"].return_value = transport
            main(["session", "lmf/cloud", "--config", str(cfg)])
        assert "under-provisioned" in capsys.readouterr().err

    def test_silent_for_unknown_instance(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = self._cloud_repo_instance(tmp_path, "z9-mystery-99", cpus=16)
        transport = MagicMock()
        with _CloudPatches(tmp_path / "state") as m:
            m["transport"].return_value = transport
            main(["session", "lmf/cloud", "--config", str(cfg)])
        assert "under-provisioned" not in capsys.readouterr().err

    def test_silent_for_adequate_known_instance(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # n2-standard-16 is (16, 64); declared 8 cpus / 64GiB fits -> no warning.
        projects = tmp_path / "projects"
        section = textwrap.dedent("""
            [vm]
            backend = "off-platform"
            provider = "gcp"
            region = "us-central1"
            instance = "n2-standard-16"
            volume = "300GiB"

            [vm.vergil-user]
            cpus = 8
            memory = "64GiB"
        """)
        _make_repo(projects, "lmf", "cloud", section)
        cfg = _identities(tmp_path, projects)
        transport = MagicMock()
        with _CloudPatches(tmp_path / "state") as m:
            m["transport"].return_value = transport
            main(["session", "lmf/cloud", "--config", str(cfg)])
        assert "under-provisioned" not in capsys.readouterr().err
