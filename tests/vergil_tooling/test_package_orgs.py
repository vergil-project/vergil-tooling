"""Tests for vergil_tooling.lib.package.orgs (spec §7.4, §9)."""

from __future__ import annotations

import re

import pytest

from vergil_tooling.lib.package import PackageError, orgs


def test_vergil_org_is_pinned() -> None:
    org = orgs.for_vendor("vergil")
    assert re.fullmatch(r"[0-9A-F]{40}", org.fingerprint)
    # The attested primary key (OP1, vergil-project/vergil-tooling#3073); never the subkey.
    assert org.fingerprint == "B3A1D804DC03AB036566A4E367861822166ECABE"
    assert org.vendor == "vergil"
    assert org.github_org == "vergil-project"
    assert org.base_url == "https://vergil-project.github.io/packages"
    assert org.keyring_package == "vergil-archive-keyring"


def test_registry_keys_match_vendors() -> None:
    assert all(key == org.vendor for key, org in orgs.ORGS.items())


def test_unknown_vendor_is_fatal() -> None:
    with pytest.raises(PackageError, match=r"unknown package vendor 'lmf' \(known: vergil\)"):
        orgs.for_vendor("lmf")
