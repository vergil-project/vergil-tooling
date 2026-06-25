"""Tests for the vm_provider strategy seam (Task 4)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from vergil_tooling.lib.vm_provider import (
    AzureStrategy,
    GcpStrategy,
    strategy_for,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestStrategyFor:
    def test_returns_gcp_strategy(self) -> None:
        s = strategy_for("gcp")
        assert isinstance(s, GcpStrategy)
        assert s.name == "gcp"
        assert s.module_segment == "gcp"

    def test_returns_azure_strategy(self) -> None:
        s = strategy_for("azure")
        assert isinstance(s, AzureStrategy)
        assert s.module_segment == "azure"

    def test_unknown_provider_aborts(self) -> None:
        with pytest.raises(SystemExit):
            strategy_for("aws")


class TestAzureTofuEnv:
    def test_subscription_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_SUBSCRIPTION_ID", "sub-from-env")
        monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
        env = AzureStrategy().tofu_env()
        assert env["ARM_SUBSCRIPTION_ID"] == "sub-from-env"
        assert env["TF_IN_AUTOMATION"] == "1"
        assert "plugin-cache" in env["TF_PLUGIN_CACHE_DIR"]
        assert "GOOGLE_CLOUD_PROJECT" not in env

    def test_subscription_from_az_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="sub-from-cli\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        env = AzureStrategy().tofu_env()
        assert env["ARM_SUBSCRIPTION_ID"] == "sub-from-cli"
        argv = sub.call_args[0][0]
        assert argv == ["az", "account", "show", "--query", "id", "-o", "tsv"]

    def test_empty_subscription_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
        sub = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="\n", stderr=""))
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        with pytest.raises(SystemExit):
            AzureStrategy().tofu_env()


class TestAzurePreflight:
    def test_missing_az_aborts(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.shutil.which", lambda _: None)
        with pytest.raises(SystemExit):
            AzureStrategy().preflight()
        assert "az" in capsys.readouterr().err

    def test_missing_token_aborts(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.shutil.which", lambda _: "/usr/bin/az")
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_provider.subprocess.run",
            MagicMock(side_effect=subprocess.CalledProcessError(1, "az")),
        )
        with pytest.raises(SystemExit):
            AzureStrategy().preflight()
        assert "az login" in capsys.readouterr().err

    def test_all_present_passes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.shutil.which", lambda _: "/usr/bin/az")
        monkeypatch.setattr(
            "vergil_tooling.lib.vm_provider.subprocess.run",
            MagicMock(
                return_value=subprocess.CompletedProcess([], 0, stdout='{"accessToken": "t"}')
            ),
        )
        AzureStrategy().preflight()  # no raise


class TestGcpStrategyDelegation:
    """Coverage tests for GcpStrategy methods not exercised by vm_cloud delegation tests."""

    def test_transport_builds_iap(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        (tmp_path / "zone").write_text("us-central1-b")
        transport = GcpStrategy().transport("my-vm", tmp_path, "ubuntu")
        from vergil_tooling.lib.vm_transport import IapTransport

        assert isinstance(transport, IapTransport)
        assert transport.host == "my-vm"
        assert transport.zone == "us-central1-b"
        assert transport.project == "proj-env"

    def test_status_running(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        (tmp_path / "zone").write_text("us-central1-b")
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="RUNNING\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert GcpStrategy().status("my-vm", tmp_path) == "Running"

    def test_status_terminated(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        (tmp_path / "zone").write_text("us-central1-b")
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="TERMINATED\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert GcpStrategy().status("my-vm", tmp_path) == "Stopped"

    def test_status_stopped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        (tmp_path / "zone").write_text("us-central1-b")
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="STOPPED\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert GcpStrategy().status("my-vm", tmp_path) == "Stopped"

    def test_status_unknown_is_empty(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        (tmp_path / "zone").write_text("us-central1-b")
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="PROVISIONING\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert GcpStrategy().status("my-vm", tmp_path) == ""

    def test_status_gcloud_error_is_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj-env")
        (tmp_path / "zone").write_text("us-central1-b")
        sub = MagicMock(side_effect=subprocess.CalledProcessError(1, "gcloud"))
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert GcpStrategy().status("my-vm", tmp_path) == ""

    def test_status_no_zone_is_empty(self, tmp_path: Path) -> None:
        # No zone file -> read_zone raises -> ""
        assert GcpStrategy().status("my-vm", tmp_path) == ""

    def test_volume_disk_type(self) -> None:
        assert GcpStrategy().volume_disk_type() == "google_compute_disk"


class TestAzureStrategyMethods:
    """Coverage tests for AzureStrategy protocol methods."""

    def test_region_zones_returns_region(self) -> None:
        assert AzureStrategy().region_zones("eastus") == ["eastus"]

    def test_is_zone_capacity_error_always_false(self) -> None:
        exc = subprocess.CalledProcessError(1, "tofu", stderr="out of capacity")
        assert AzureStrategy().is_zone_capacity_error(exc) is False

    def test_instance_fallback_candidates_no_ladder(self) -> None:
        assert AzureStrategy().instance_fallback_candidates("Standard_D8s_v3") == [
            "Standard_D8s_v3"
        ]

    def test_transport_raises_not_implemented(self, tmp_path: Path) -> None:
        with pytest.raises(NotImplementedError):
            AzureStrategy().transport("vm", tmp_path, "azureuser")

    def test_status_returns_empty(self, tmp_path: Path) -> None:
        assert AzureStrategy().status("vm", tmp_path) == ""

    def test_volume_disk_type(self) -> None:
        assert AzureStrategy().volume_disk_type() == "azurerm_managed_disk"

    def test_subscription_filenotfounderror_falls_through(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """FileNotFoundError in az account show sets sub to empty -> SystemExit."""
        monkeypatch.delenv("AZURE_SUBSCRIPTION_ID", raising=False)
        sub = MagicMock(side_effect=FileNotFoundError("no az"))
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        with pytest.raises(SystemExit):
            AzureStrategy()._subscription()
        assert "az login" in capsys.readouterr().err
