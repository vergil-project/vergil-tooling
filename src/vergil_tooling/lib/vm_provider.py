"""Provider-strategy seam for the off-platform cloud backend.

Each cloud provider (GCP, Azure) is encapsulated as a strategy object that
implements the ``Provider`` protocol.  ``strategy_for`` is the factory; callers
hold a ``Provider`` and never branch on the provider string at the call site.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vergil_tooling.lib.vm_transport import Transport


# ---------------------------------------------------------------------------
# Shared helpers (moved from vm_cloud to avoid circular imports)
# ---------------------------------------------------------------------------


def _plugin_cache_dir() -> Path:
    """Shared OpenTofu provider plugin cache, created on demand."""
    path = Path.home() / ".config" / "vergil" / "tofu" / "plugin-cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---------------------------------------------------------------------------
# Provider protocol
# ---------------------------------------------------------------------------


class Provider(Protocol):  # pragma: no cover
    name: str
    module_segment: str

    def preflight(self) -> None: ...
    def tofu_env(self) -> dict[str, str]: ...
    def region_zones(self, region: str) -> list[str]: ...
    def is_zone_capacity_error(self, exc: subprocess.CalledProcessError) -> bool: ...
    def instance_fallback_candidates(self, requested: str) -> list[str]: ...
    def transport(self, name: str, state_dir: Path, ssh_user: str) -> Transport: ...
    def status(self, name: str, state_dir: Path) -> str: ...
    def volume_disk_type(self) -> str: ...


# ---------------------------------------------------------------------------
# GCP strategy
# ---------------------------------------------------------------------------

# A GCP zone-capacity stockout ("the zone does not have enough resources" /
# ZONE_RESOURCE_POOL_EXHAUSTED) is transient and zone-specific, so it is worth
# retrying in another zone — unlike a real config/quota error, which must abort.
_ZONE_CAPACITY_RE = re.compile(
    r"does not have enough resources available|ZONE_RESOURCE_POOL_EXHAUSTED",
    re.IGNORECASE,
)

# The ladder may contain ONLY families that support GCP nested virtualization.
# GCE nested virt requires an Intel (VT-x) processor: AMD, Arm, E2,
# memory-optimized, and H4D VMs are all excluded.  Verified 2026-06-24.
NESTED_VIRT_FAMILIES = ("n2", "c2")

# Shapes verified to exist for EVERY family in the ladder.
FALLBACK_SHAPES = frozenset({"standard-8", "standard-16"})


class GcpStrategy:
    name = "gcp"
    module_segment = "gcp"

    NESTED_VIRT_FAMILIES = NESTED_VIRT_FAMILIES
    FALLBACK_SHAPES = FALLBACK_SHAPES

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_project(self) -> str:
        """The GCP project: ``GOOGLE_CLOUD_PROJECT`` if set, else gcloud config."""
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            result = subprocess.run(  # noqa: S603
                ["gcloud", "config", "get-value", "project"],  # noqa: S607
                capture_output=True,
                text=True,
                check=True,
            )
            project = result.stdout.strip()
        if not project:
            print(
                "ERROR: no GCP project — set GOOGLE_CLOUD_PROJECT or run: "
                "gcloud config set project <project>",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return project

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def preflight(self) -> None:
        """Check gcloud is present and ADC is valid."""
        if shutil.which("gcloud") is None:
            print("ERROR: gcloud not found — install the gcloud CLI", file=sys.stderr)
            raise SystemExit(1)

        try:
            subprocess.run(  # noqa: S603
                ["gcloud", "auth", "application-default", "print-access-token"],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(
                "ERROR: no application-default credentials — run: "
                "gcloud auth application-default login",
                file=sys.stderr,
            )
            raise SystemExit(1) from None

    def tofu_env(self) -> dict[str, str]:
        """Environment for every tofu invocation: non-interactive, shared plugin cache,
        and the GCP project.
        """
        return {
            **os.environ,
            "TF_IN_AUTOMATION": "1",
            "TF_PLUGIN_CACHE_DIR": str(_plugin_cache_dir()),
            "GOOGLE_CLOUD_PROJECT": self._resolve_project(),
        }

    def region_zones(self, region: str) -> list[str]:
        """The UP zones of a GCP region, sorted (e.g. us-central1 -> -a/-b/-c/-f)."""
        result = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "gcloud",
                "compute",
                "zones",
                "list",
                f"--filter=name~^{region}- AND status=UP",
                "--format=value(name)",
                f"--project={self._resolve_project()}",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return sorted(result.stdout.split())

    def is_zone_capacity_error(self, exc: subprocess.CalledProcessError) -> bool:
        """True when a tofu apply failed purely because the zone is out of capacity."""
        blob = f"{exc.stderr or ''}{exc.stdout or ''}"
        return bool(_ZONE_CAPACITY_RE.search(blob))

    def instance_fallback_candidates(self, requested: str) -> list[str]:
        """Ordered machine types to try for ``requested``, the requested type first."""
        _family, _, shape = requested.partition("-")
        if not shape or shape not in FALLBACK_SHAPES:
            return [requested]
        candidates = [requested]
        for family in NESTED_VIRT_FAMILIES:
            candidate = f"{family}-{shape}"
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def transport(self, name: str, state_dir: Path, ssh_user: str) -> Transport:
        """Build an IAP transport for the given box."""
        from vergil_tooling.lib.vm_cloud import read_zone
        from vergil_tooling.lib.vm_transport import IapTransport

        zone = read_zone(state_dir)
        return IapTransport(name, zone, self._resolve_project(), ssh_user)

    def status(self, name: str, state_dir: Path) -> str:
        """Return the GCP instance status string, or '' if unknown."""
        from vergil_tooling.lib.vm_cloud import read_zone

        try:
            zone = read_zone(state_dir)
        except RuntimeError:
            return ""
        try:
            result = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "gcloud",
                    "compute",
                    "instances",
                    "describe",
                    name,
                    f"--zone={zone}",
                    f"--project={self._resolve_project()}",
                    "--format=value(status)",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return ""
        raw = result.stdout.strip()
        if raw == "RUNNING":
            return "Running"
        if raw in {"TERMINATED", "STOPPED"}:
            return "Stopped"
        return ""

    def volume_disk_type(self) -> str:
        return "google_compute_disk"


# ---------------------------------------------------------------------------
# Azure strategy
# ---------------------------------------------------------------------------


class AzureStrategy:
    name = "azure"
    module_segment = "azure"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _subscription(self) -> str:
        """Azure subscription ID: ``AZURE_SUBSCRIPTION_ID`` env or ``az account show``."""
        sub = os.environ.get("AZURE_SUBSCRIPTION_ID")
        if not sub:
            try:
                result = subprocess.run(  # noqa: S603
                    ["az", "account", "show", "--query", "id", "-o", "tsv"],  # noqa: S607
                    capture_output=True,
                    text=True,
                    check=True,
                )
                sub = result.stdout.strip()
            except (subprocess.CalledProcessError, FileNotFoundError):
                sub = ""
        if not sub:
            print(
                "ERROR: no Azure subscription — set AZURE_SUBSCRIPTION_ID or run: az login",
                file=sys.stderr,
            )
            raise SystemExit(1)
        return sub

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    def preflight(self) -> None:
        """Check az CLI is present and a valid access token can be obtained."""
        if shutil.which("az") is None:
            print("ERROR: az not found — install the Azure CLI", file=sys.stderr)
            raise SystemExit(1)

        try:
            subprocess.run(  # noqa: S603
                ["az", "account", "get-access-token"],  # noqa: S607
                check=True,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            print(
                "ERROR: no Azure credentials — run: az login",
                file=sys.stderr,
            )
            raise SystemExit(1) from None

    def tofu_env(self) -> dict[str, str]:
        """Environment for Azure tofu invocations."""
        return {
            **os.environ,
            "TF_IN_AUTOMATION": "1",
            "TF_PLUGIN_CACHE_DIR": str(_plugin_cache_dir()),
            "ARM_SUBSCRIPTION_ID": self._subscription(),
        }

    def region_zones(self, region: str) -> list[str]:
        """Azure uses locations, not zones — return the region itself."""
        return [region]

    def is_zone_capacity_error(self, exc: subprocess.CalledProcessError) -> bool:  # noqa: ARG002
        """Azure has different quota behavior — not treated as a zone-capacity error."""
        return False

    def instance_fallback_candidates(self, requested: str) -> list[str]:
        """No family ladder for Azure — return the requested type only."""
        return [requested]

    def transport(self, name: str, state_dir: Path, ssh_user: str) -> Transport:  # noqa: ARG002
        """Not yet implemented (Tasks 5/6/7)."""
        raise NotImplementedError("Azure transport is not yet implemented")

    def status(self, name: str, state_dir: Path) -> str:  # noqa: ARG002
        """Not yet implemented — return empty."""
        return ""

    def volume_disk_type(self) -> str:
        return "azurerm_managed_disk"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def strategy_for(provider: str) -> Provider:
    """Return the strategy object for *provider*, or abort on unknown values."""
    if provider == "gcp":
        return GcpStrategy()
    if provider == "azure":
        return AzureStrategy()
    print(f"ERROR: unknown off-platform provider '{provider}'", file=sys.stderr)
    raise SystemExit(1)
