"""Org package repositories and their pinned trust roots (spec §7.4, §9).

Each org publishes its packages at ``base_url`` and its signing key at
``<base_url>/keys/<vendor>.asc``. Consumers trust the org's **primary** key,
whose fingerprint is pinned here, in code: the signing subkey rotates through
ordinary keyring-package upgrades without touching this file.
"""

from __future__ import annotations

from dataclasses import dataclass

from vergil_tooling.lib.package import PackageError


@dataclass(frozen=True)
class OrgRepo:
    """One org's package repository and the primary-key fingerprint it is trusted by."""

    vendor: str
    github_org: str
    base_url: str
    fingerprint: str
    keyring_package: str


ORGS: dict[str, OrgRepo] = {
    "vergil": OrgRepo(
        vendor="vergil",
        github_org="vergil-project",
        base_url="https://vergil-project.github.io/packages",
        # Primary (certify-only) key attested in OP1, vergil-project/vergil-tooling#3073.
        fingerprint="B3A1D804DC03AB036566A4E367861822166ECABE",
        keyring_package="vergil-archive-keyring",
    ),
}


def for_vendor(vendor: str) -> OrgRepo:
    """Return the registered org repository for ``vendor``; unknown vendors are fatal."""
    if vendor not in ORGS:
        msg = f"unknown package vendor {vendor!r} (known: {', '.join(sorted(ORGS))})"
        raise PackageError(msg)
    return ORGS[vendor]
