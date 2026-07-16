"""Empirical, fail-closed platform detection.

A single platform-awareness signal distinguishes three states along two
axes — *sandbox* (in a VM) vs. *host* (not a VM), and *cloud* vs. *local*:

- ``physical-host`` — not a VM; the human's machine and the single source
  of truth for canonical agent data.
- ``local-vm`` — a Lima VM whose writes reach the live host store through
  path-preserved mounts (durable).
- ``cloud-vm`` — an off-platform x86 VM whose data disk is ephemeral; its
  copy of canonical data is a read-only cache.

The resolver derives the platform from **empirical heuristics — there is
no written marker file.** Candidate signals, in precedence order:

1. macOS (``platform.system() == "Darwin"``) and no ``/vergil`` mount ⇒
   ``PHYSICAL_HOST``.
2. ``/vergil`` mount present ⇒ in a VM. A reachable cloud metadata
   endpoint ⇒ ``CLOUD_VM``; else Lima markers ⇒ ``LOCAL_VM``.
3. **Fail closed:** any VM signal present (or a box that cannot be
   positively confirmed as the physical host) that is not positively
   confirmed ``LOCAL_VM`` ⇒ ``CLOUD_VM``. The resolver never falls
   through to ``PHYSICAL_HOST`` from an unconfirmed box.

Fail-open — silently deciding "host" and re-enabling futile writes — is
the exact failure the read-only cloud-memory control exists to kill, and
is prohibited here regardless of detection mechanism.

The platform axis is distinct from the identity axis
(:mod:`vergil_tooling.lib.identity_mode`) but correlates with it
(host → human, any VM → agent). The resolver keeps them separate and
*reports* the correlation via :attr:`PlatformResolution.disagreement`
rather than conflating them.

This mirrors the dataclass / ``disagreement`` shape of
:mod:`vergil_tooling.lib.identity_mode`.
"""

from __future__ import annotations

import enum
import platform as _platform_mod
import socket
from dataclasses import dataclass

from vergil_tooling.lib import identity_mode


class Platform(enum.Enum):
    PHYSICAL_HOST = "physical-host"
    LOCAL_VM = "local-vm"
    CLOUD_VM = "cloud-vm"


# Signal names recorded in :attr:`PlatformResolution.signals`.
_SIGNAL_OS = "os"
_SIGNAL_VERGIL = "vergil-mount"
_SIGNAL_CLOUD_METADATA = "cloud-metadata"
_SIGNAL_LIMA_MARKER = "lima-marker"
_SIGNAL_IDENTITY = "identity"

# Provenance tokens for how the platform was resolved.
_FROM_DARWIN_NO_VERGIL = "darwin-no-vergil"
_FROM_CLOUD_METADATA = "cloud-metadata"
_FROM_LIMA_MARKER = "lima-marker"
_FROM_FAIL_CLOSED = "fail-closed"

# The link-local metadata address shared by the major cloud providers
# (GCP metadata.google.internal, Azure IMDS, AWS IMDS all answer here).
_METADATA_HOST = "169.254.169.254"
_METADATA_PORT = 80
_METADATA_TIMEOUT_S = 0.25

_VERGIL_MOUNT = "/vergil"
_LIMA_MARKERS = ("/mnt/lima", "/dev/virtio-ports/lima")


@dataclass(frozen=True)
class PlatformResolution:
    """The resolved platform plus the evidence it was resolved from."""

    platform: Platform
    resolved_from: str
    signals: dict[str, str]
    disagreement: bool


def _vergil_mount_present() -> bool:
    """True when the ``/vergil`` data disk is present (a VM signal)."""
    from pathlib import Path

    return Path(_VERGIL_MOUNT).exists()


def _cloud_metadata_reachable() -> bool:
    """True when a cloud metadata endpoint answers a short-timeout probe.

    A TCP connect to the link-local metadata address succeeds only inside
    a cloud VM; on a physical host or a Lima VM the address is unrouted
    and the connect fails fast. Never raises — any failure means "not
    reachable."
    """
    try:
        with socket.create_connection(
            (_METADATA_HOST, _METADATA_PORT), timeout=_METADATA_TIMEOUT_S
        ):
            return True
    except OSError:
        return False


def _lima_marker_present() -> bool:
    """True when a Lima-specific marker is present (a local-VM signal)."""
    from pathlib import Path

    return any(Path(marker).exists() for marker in _LIMA_MARKERS)


def _identity_is_agent() -> bool:
    """True when the resolved identity is an agent (corroboration only)."""
    return identity_mode.is_agent()


def resolve_platform() -> PlatformResolution:
    """Resolve the platform empirically and record the evidence.

    This is the single source of truth for platform resolution; both
    :func:`current_platform` and the ``vrg-whoami`` CLI consult it. See
    the module docstring for the precedence and the fail-closed rule.
    """
    is_darwin = _platform_mod.system() == "Darwin"
    vergil = _vergil_mount_present()
    is_agent = _identity_is_agent()

    signals = {
        _SIGNAL_OS: _platform_mod.system(),
        _SIGNAL_VERGIL: "present" if vergil else "absent",
        _SIGNAL_CLOUD_METADATA: "not-probed",
        _SIGNAL_LIMA_MARKER: "not-probed",
        _SIGNAL_IDENTITY: "agent" if is_agent else "human",
    }

    if is_darwin and not vergil:
        platform_val = Platform.PHYSICAL_HOST
        resolved_from = _FROM_DARWIN_NO_VERGIL
    else:
        # In a VM, or a box we cannot positively confirm as the physical
        # host. Distinguish cloud from local; anything not positively
        # confirmed local fails closed to cloud (never PHYSICAL_HOST).
        cloud_reachable = _cloud_metadata_reachable()
        signals[_SIGNAL_CLOUD_METADATA] = "reachable" if cloud_reachable else "unreachable"
        if cloud_reachable:
            platform_val = Platform.CLOUD_VM
            resolved_from = _FROM_CLOUD_METADATA
        else:
            lima = _lima_marker_present()
            signals[_SIGNAL_LIMA_MARKER] = "present" if lima else "absent"
            if lima:
                platform_val = Platform.LOCAL_VM
                resolved_from = _FROM_LIMA_MARKER
            else:
                platform_val = Platform.CLOUD_VM
                resolved_from = _FROM_FAIL_CLOSED

    disagreement = _correlation_disagrees(platform_val, is_agent)
    return PlatformResolution(
        platform=platform_val,
        resolved_from=resolved_from,
        signals=signals,
        disagreement=disagreement,
    )


def _correlation_disagrees(platform_val: Platform, is_agent: bool) -> bool:
    """Flag a platform/identity mismatch against the expected correlation.

    The expected correlation is host ↔ human, any VM ↔ agent. An agent on
    the physical host, or a human inside a VM, is a mismatch worth
    surfacing — it is the condition that precedes a misread.
    """
    is_host = platform_val is Platform.PHYSICAL_HOST
    if is_host:
        return is_agent
    return not is_agent


def current_platform() -> Platform:
    """Return the resolved platform.

    Thin wrapper over :func:`resolve_platform`; see it for the order.
    """
    return resolve_platform().platform


def is_cloud() -> bool:
    """Return True only on a ``cloud-vm`` (where the memory control activates)."""
    return current_platform() is Platform.CLOUD_VM
