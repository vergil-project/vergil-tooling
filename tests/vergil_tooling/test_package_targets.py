"""Tests for vergil_tooling.lib.package.targets (spec §5.1)."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.package import PackageError, targets


def test_registry_is_the_full_two_by_two() -> None:
    keys = [t.key for t in targets.all_targets()]
    assert keys == [
        "rhel/10/amd64",
        "rhel/10/arm64",
        "rhel/9/amd64",
        "rhel/9/arm64",
        "ubuntu/24.04/amd64",
        "ubuntu/24.04/arm64",
        "ubuntu/26.04/amd64",
        "ubuntu/26.04/arm64",
    ]


def test_format_suite_and_rpm_arch() -> None:
    t = targets.REGISTRY["rhel/9/arm64"]
    assert (t.fmt, t.suite, t.rpm_arch, t.glibc) == ("rpm", "el9", "aarch64", (2, 34))
    u = targets.REGISTRY["ubuntu/24.04/amd64"]
    assert (u.fmt, u.suite, u.image, u.rpm_arch) == ("deb", "noble", "ubuntu:24.04", "x86_64")


def test_suites_per_release() -> None:
    suites = {(t.distro, t.version): t.suite for t in targets.all_targets()}
    assert suites == {
        ("ubuntu", "24.04"): "noble",
        ("ubuntu", "26.04"): "resolute",
        ("rhel", "9"): "el9",
        ("rhel", "10"): "el10",
    }


def test_glibc_per_release() -> None:
    # Values from `ldd --version` in each registry image (see targets.py).
    glibc = {(t.distro, t.version): t.glibc for t in targets.all_targets()}
    assert glibc == {
        ("ubuntu", "24.04"): (2, 39),
        ("ubuntu", "26.04"): (2, 43),
        ("rhel", "9"): (2, 34),
        ("rhel", "10"): (2, 39),
    }


def test_match_globs() -> None:
    assert [t.key for t in targets.match("rhel/*/arm64")] == ["rhel/10/arm64", "rhel/9/arm64"]
    assert targets.match("debian/*/*") == []
    assert len(targets.match("*/*/*")) == len(targets.REGISTRY)


def test_package_error_is_an_exception() -> None:
    with pytest.raises(PackageError, match="boom"):
        raise PackageError("boom")
