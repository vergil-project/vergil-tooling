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
    """Coverage tests for AzureStrategy protocol methods not covered by the new classes."""

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


class TestAzureZoneCapacity:
    """AzureStrategy.is_zone_capacity_error matches the Azure-specific stockout strings."""

    def test_detects_sku_not_available(self) -> None:
        exc = subprocess.CalledProcessError(
            1, ["tofu", "apply"], stderr="Error: SkuNotAvailable in eastus"
        )
        assert AzureStrategy().is_zone_capacity_error(exc) is True

    def test_detects_zonal_allocation_failed(self) -> None:
        exc = subprocess.CalledProcessError(
            1, ["tofu", "apply"], stderr="ZonalAllocationFailed: no hosts in zone 1"
        )
        assert AzureStrategy().is_zone_capacity_error(exc) is True

    def test_detects_overconstrained_case_insensitive(self) -> None:
        exc = subprocess.CalledProcessError(
            1, [], stderr="overconstrainedallocationrequest encountered"
        )
        assert AzureStrategy().is_zone_capacity_error(exc) is True

    def test_generic_error_is_not_capacity(self) -> None:
        exc = subprocess.CalledProcessError(1, [], stderr="Error: quota exceeded for cores")
        assert AzureStrategy().is_zone_capacity_error(exc) is False

    def test_checks_stdout_when_stderr_empty(self) -> None:
        exc = subprocess.CalledProcessError(1, [], stderr="")
        exc.stdout = "SkuNotAvailable in region"
        assert AzureStrategy().is_zone_capacity_error(exc) is True


class TestAzureZones:
    """AzureStrategy.region_zones returns bare-integer zone strings, or [] for zoneless."""

    def test_enumerates_bare_integer_zones(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sub = MagicMock(
            return_value=subprocess.CompletedProcess([], 0, stdout="1\n2\n3\n", stderr="")
        )
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        result = AzureStrategy().region_zones("eastus")
        assert result == ["1", "2", "3"]
        argv = sub.call_args[0][0]
        assert argv[0] == "az"
        assert argv[1] == "vm"
        assert argv[2] == "list-skus"
        assert "--location" in argv
        assert "eastus" in argv

    def test_zoneless_region_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sub = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="\n", stderr=""))
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert AzureStrategy().region_zones("westus") == []

    def test_empty_stdout_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sub = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        monkeypatch.setattr("vergil_tooling.lib.vm_provider.subprocess.run", sub)
        assert AzureStrategy().region_zones("northeurope") == []


class TestAzureLadder:
    """AzureStrategy.instance_fallback_candidates — requested-first Azure family ladder."""

    def test_requested_first_then_same_size_siblings(self) -> None:
        result = AzureStrategy().instance_fallback_candidates("Standard_D8s_v5")
        assert result[0] == "Standard_D8s_v5"
        # same-vCPU siblings from the ladder follow, deduped
        assert len(result) > 1
        assert result.count("Standard_D8s_v5") == 1

    def test_unsupported_size_yields_only_requested(self) -> None:
        # vCPU count 4 is not in FALLBACK_SHAPES
        assert AzureStrategy().instance_fallback_candidates("Standard_D4s_v5") == [
            "Standard_D4s_v5"
        ]

    def test_unsupported_family_grammar_yields_only_requested(self) -> None:
        # Size string that does not match the Azure grammar
        assert AzureStrategy().instance_fallback_candidates("n2-standard-8") == ["n2-standard-8"]

    def test_requested_family_in_ladder_still_leads_and_deduped(self) -> None:
        # Dsv5 is in the ladder; it must appear exactly once, requested-first.
        result = AzureStrategy().instance_fallback_candidates("Standard_D16s_v5")
        assert result[0] == "Standard_D16s_v5"
        assert result.count("Standard_D16s_v5") == 1

    def test_non_ladder_family_leads_then_full_ladder(self) -> None:
        # Standard_D8s_v3 is not in the ladder (v3 family pre-dates the curated set).
        # The requested type leads, and all ladder families follow at the same vCPU count.
        result = AzureStrategy().instance_fallback_candidates("Standard_D8s_v3")
        assert result[0] == "Standard_D8s_v3"
        assert len(result) > 1

    def test_ladder_change_detector(self) -> None:
        # NOT a validity proof — pins the curated constants so any edit is deliberate.
        # Azure nested-virt validity is verified by hand against Azure docs (#1878):
        # Dsv5 https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv5-series
        # Dsv4 https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/general-purpose/dsv4-series
        # Fsv2 https://learn.microsoft.com/en-us/azure/virtual-machines/sizes/compute-optimized/fsv2-series
        assert AzureStrategy.NESTED_VIRT_FAMILIES == ("Dsv5", "Dsv4", "Fsv2")
        assert AzureStrategy.FALLBACK_SHAPES == frozenset({"8", "16"})  # noqa: SIM300
