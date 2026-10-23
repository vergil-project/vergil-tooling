"""Tests for vergil_tooling.lib.package.runtime (one runtime per release, #3478)."""

from __future__ import annotations

import re

import pytest

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.package import runtime as rt


def test_runtime_package_names_the_series() -> None:
    assert rt.runtime_package("3.14") == "vergil-python3.14"


@pytest.mark.parametrize(("runtime", "series"), [("3.14", "3.14"), ("3.14.8", "3.14")])
def test_series_of(runtime: str, series: str) -> None:
    assert rt.series_of(runtime) == series


def test_series_of_bad_runtime_is_a_package_error() -> None:
    with pytest.raises(PackageError, match=r"'bogus' is not an x.y or x.y.z version"):
        rt.series_of("bogus")


def test_consistent_release_passes() -> None:
    rt.check_consistent(
        "3.14",
        [
            ("t_2.1.0-1_amd64.deb", ["vergil-python3.14", "libc6"]),
            ("t-2.1.0-1.aarch64.rpm", ("vergil-python3.14",)),
        ],
        "dist",
    )


def test_pinned_patch_runtime_checks_its_series() -> None:
    rt.check_consistent("3.14.8", [("t_2.1.0-1_amd64.deb", ["vergil-python3.14"])], "dist")


def test_mixed_runtimes_fail_naming_each_mismatch() -> None:
    """The vergil-tooling 2.1.258 shape: amd64 on 3.15, arm64 on 3.14."""
    packages = [
        ("t_2.1.258-1_amd64.deb", ["vergil-python3.15"]),
        ("t-2.1.258-1.x86_64.rpm", ["vergil-python3.15"]),
        ("t_2.1.258-1_arm64.deb", ["vergil-python3.14"]),
        ("t-2.1.258-1.aarch64.rpm", ["vergil-python3.14"]),
    ]
    with pytest.raises(PackageError) as exc:
        rt.check_consistent("3.14", packages, "dist/packages")
    msg = str(exc.value)
    assert msg.startswith(
        "dist/packages: packages disagree with the recorded runtime vergil-python3.14 "
        "(runtime 3.14, recorded by vrg-package matrix): "
    )
    assert "t_2.1.258-1_amd64.deb depends on vergil-python3.15" in msg
    assert "t-2.1.258-1.x86_64.rpm depends on vergil-python3.15" in msg
    assert "arm64.deb" not in msg
    assert "aarch64" not in msg


@pytest.mark.parametrize(
    ("depends", "shown"),
    [
        ([], "no vergil-python runtime"),
        (["vergil-python3.14.8"], "no vergil-python runtime"),
        (["vergil-python3.14", "vergil-python3.15"], "vergil-python3.14, vergil-python3.15"),
    ],
)
def test_missing_legacy_or_extra_runtime_dependency_fails(depends: list[str], shown: str) -> None:
    with pytest.raises(PackageError, match=rf"t\.deb depends on {re.escape(shown)}\."):
        rt.check_consistent("3.14", [("t.deb", depends)], "dist")


def test_no_packages_fails() -> None:
    with pytest.raises(PackageError, match=r"^dist: no packages to check .* vergil-python3\.14$"):
        rt.check_consistent("3.14", [], "dist")
