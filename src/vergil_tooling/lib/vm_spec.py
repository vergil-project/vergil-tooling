"""Compose, name, and fingerprint per-repo VM specs (pure logic, no I/O)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vergil_tooling.lib.config import RoleOverlay, VmStanza

_DEFAULT_STALE_DAYS = 3


def _gib(value: str) -> int:
    """Parse a ``"<N>GiB"`` string to its integer prefix."""
    return int(value.removesuffix("GiB"))


@dataclass
class ComposedSpec:
    cpus: int
    memory: str
    disk: str
    stale_days: int
    packages: tuple[str, ...]
    provision: str | None
    dedicated: bool
    under: tuple[str, ...]
    # SHA-256 of the provision hook's CONTENTS (not the path). compose_vm_spec has no
    # file access, so it leaves this None; the resolver fills it in before fingerprinting
    # so that editing the script flips NEEDS-REBUILD.
    provision_hash: str | None = None


@dataclass
class _Acc:
    """Mutable accumulator threaded through the overlay tiers."""

    cpus: int
    memory: str
    disk: str
    stale_days: int
    provision: str | None
    packages: list[str]
    customized: bool
    # Repo-declared footprint (tiers 3+4 only) — the floor an override is measured
    # against. None means the repo never declared that scalar, so no floor applies.
    declared_cpus: int | None
    declared_mem: int | None
    declared_disk: int | None


def _apply_overlay(acc: _Acc, overlay: VmStanza | RoleOverlay) -> None:
    """Overlay a repo `[vm]` or `[vm.<role>]` table onto the accumulator."""
    if overlay.packages:
        acc.packages.extend(overlay.packages)
        acc.customized = True
    if overlay.cpus is not None:
        acc.cpus = acc.declared_cpus = overlay.cpus
        acc.customized = True
    if overlay.memory is not None:
        acc.memory = overlay.memory
        acc.declared_mem = _gib(overlay.memory)
        acc.customized = True
    if overlay.disk is not None:
        acc.disk = overlay.disk
        acc.declared_disk = _gib(overlay.disk)
        acc.customized = True
    if overlay.stale_days is not None:
        acc.stale_days = overlay.stale_days
        acc.customized = True
    if overlay.provision is not None:
        acc.provision = overlay.provision
        acc.customized = True


def compose_vm_spec(
    *,
    identity: str,
    base: Mapping[str, object],
    stanza: VmStanza | None,
    override: Mapping[str, object] | None,
) -> ComposedSpec:
    """Overlay the five precedence tiers into the effective spec for one (identity, repo)."""
    # Tier 1+2: built-in/base footprint from the identity.
    acc = _Acc(
        cpus=cast("int", base["cpus"]),
        memory=str(base["memory"]),
        disk=str(base["disk"]),
        stale_days=_DEFAULT_STALE_DAYS,
        provision=None,
        packages=[],
        customized=False,
        declared_cpus=None,
        declared_mem=None,
        declared_disk=None,
    )

    # Tiers 3 + 4: repo [vm] (all-identity), then [vm.<identity>] role overlay.
    if stanza is not None:
        _apply_overlay(acc, stanza)
        role = stanza.roles.get(identity)
        if role is not None:
            _apply_overlay(acc, role)

    # Tier 5: host override (wins). Flag any scalar pushed below the repo-declared floor.
    under: list[str] = []
    if override:
        acc.customized = True
        if "cpus" in override:
            acc.cpus = cast("int", override["cpus"])
            if acc.declared_cpus is not None and acc.cpus < acc.declared_cpus:
                under.append("cpus")
        if "memory" in override:
            acc.memory = str(override["memory"])
            if acc.declared_mem is not None and _gib(acc.memory) < acc.declared_mem:
                under.append("mem")
        if "disk" in override:
            acc.disk = str(override["disk"])
            if acc.declared_disk is not None and _gib(acc.disk) < acc.declared_disk:
                under.append("disk")
        if "stale_days" in override:
            acc.stale_days = cast("int", override["stale_days"])

    return ComposedSpec(
        cpus=acc.cpus,
        memory=acc.memory,
        disk=acc.disk,
        stale_days=acc.stale_days,
        packages=tuple(sorted(set(acc.packages))),
        provision=acc.provision,
        dedicated=acc.customized,
        under=tuple(under),
    )


_TIER_SEP = "--"


def instance_name(identity: str, org: str | None, repo: str | None) -> str:
    """Derive the Lima instance name. Bare identity = base box; ``--``-joined = dedicated."""
    if org is None or repo is None:
        return identity
    return _TIER_SEP.join((identity, org, repo))


def parse_instance_name(name: str) -> tuple[str, str | None, str | None]:
    """Reverse instance_name. Returns (identity, org, repo); org/repo are None for base."""
    parts = name.split(_TIER_SEP)
    if len(parts) == 1:
        return parts[0], None, None
    if len(parts) == 3:  # noqa: PLR2004
        return parts[0], parts[1], parts[2]
    msg = f"unparseable VM instance name: {name!r}"
    raise ValueError(msg)


def spec_fingerprint(spec: ComposedSpec) -> str:
    """Stable SHA-256 over the declaration (NOT the resulting image bytes).

    ``under`` and ``dedicated`` are excluded: they are derived view-state, not part of
    what the VM is built from. Packages are sorted so order cannot change the hash. The
    provision hook's CONTENT hash (when present) is what makes editing the script flip the
    fingerprint; the path is only a fallback.
    """
    payload = "\n".join(
        (
            f"cpus={spec.cpus}",
            f"memory={spec.memory}",
            f"disk={spec.disk}",
            f"stale_days={spec.stale_days}",
            f"provision={spec.provision_hash or spec.provision or ''}",
            "packages=" + ",".join(sorted(spec.packages)),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
