"""Tests for vergil_tooling.lib.package.matrix (spec §5.3)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from vergil_tooling.lib.config import ConfigError, PackageConfig, PackagePythonConfig
from vergil_tooling.lib.package import matrix


def _pkg(**kw: Any) -> PackageConfig:
    base: dict[str, Any] = {
        "builder": "python",
        "vendor": "vergil",
        "summary": "s",
        "smoke": "true",
        "python": PackagePythonConfig(runtime="3.14.4"),
    }
    base.update(kw)
    return PackageConfig(**base)


def test_default_is_two_shared_cells_and_eight_tests() -> None:
    m = matrix.resolve(_pkg())
    assert [(c.id, c.runner, c.image, c.fmts, c.native, c.suite) for c in m.build] == [
        ("shared-amd64", "ubuntu-24.04", "ubuntu:24.04", ("deb", "rpm"), False, None),
        ("shared-arm64", "ubuntu-24.04-arm", "ubuntu:24.04", ("deb", "rpm"), False, None),
    ]
    assert m.build[0].targets == (
        "rhel/10/amd64",
        "rhel/9/amd64",
        "ubuntu/24.04/amd64",
        "ubuntu/26.04/amd64",
    )
    assert len(m.test) == 8
    assert {t.runner for t in m.test if t.arch == "arm64"} == {"ubuntu-24.04-arm"}
    assert {t.runner for t in m.test if t.arch == "amd64"} == {"ubuntu-24.04"}
    assert not any(t.native for t in m.test)


def test_test_cells_carry_the_target_image_and_suite() -> None:
    m = matrix.resolve(_pkg())
    cell = next(t for t in m.test if t.target == "rhel/9/arm64")
    assert (cell.id, cell.image, cell.fmt, cell.suite) == (
        "test-rhel-9-arm64",
        "registry.access.redhat.com/ubi9/ubi",
        "rpm",
        "el9",
    )


def test_exclude_drops_rhel_arm() -> None:
    m = matrix.resolve(_pkg(exclude=["rhel/*/arm64"]))
    assert [(c.id, c.fmts) for c in m.build] == [
        ("shared-amd64", ("deb", "rpm")),
        ("shared-arm64", ("deb",)),
    ]
    assert len(m.test) == 6


def test_targets_subset_on_one_arch_gives_one_cell() -> None:
    m = matrix.resolve(_pkg(targets=["ubuntu/*/amd64"]))
    assert [(c.id, c.fmts) for c in m.build] == [("shared-amd64", ("deb",))]
    assert [t.target for t in m.test] == ["ubuntu/24.04/amd64", "ubuntu/26.04/amd64"]


def test_native_gets_its_own_cell_in_its_os() -> None:
    m = matrix.resolve(_pkg(native=["ubuntu/26.04/*"]))
    native = [c for c in m.build if c.native]
    assert [(c.id, c.image, c.fmts, c.suite) for c in native] == [
        ("native-ubuntu-26.04-amd64", "ubuntu:26.04", ("deb",), "resolute"),
        ("native-ubuntu-26.04-arm64", "ubuntu:26.04", ("deb",), "resolute"),
    ]
    assert [c.targets for c in native] == [("ubuntu/26.04/amd64",), ("ubuntu/26.04/arm64",)]
    assert native[1].runner == "ubuntu-24.04-arm"
    shared_amd = next(c for c in m.build if c.id == "shared-amd64")
    assert "ubuntu/26.04/amd64" not in shared_amd.targets
    assert all(t.native for t in m.test if t.target.startswith("ubuntu/26.04"))
    assert not any(t.native for t in m.test if not t.target.startswith("ubuntu/26.04"))


def test_all_native_has_no_shared_cells() -> None:
    m = matrix.resolve(_pkg(targets=["rhel/9/*"], native=["*/*/*"]))
    assert [c.id for c in m.build] == ["native-rhel-9-amd64", "native-rhel-9-arm64"]


def test_noarch_builds_once_on_amd64() -> None:
    m = matrix.resolve(_pkg(noarch=True))
    assert [(c.id, c.arch, c.runner, c.fmts) for c in m.build] == [
        ("shared-noarch", "amd64", "ubuntu-24.04", ("deb", "rpm")),
    ]
    assert len(m.build[0].targets) == 8
    assert len(m.test) == 8


def test_json_and_manifest_shapes() -> None:
    m = matrix.resolve(_pkg(exclude=["rhel/*/arm64"]))
    j = matrix.to_json(m)
    assert set(j) == {"build", "test"}
    assert j["build"][0]["id"] == "shared-amd64"
    assert json.loads(json.dumps(j))["build"][0]["fmts"] == ["deb", "rpm"]
    assert matrix.manifest(m) == {
        "artifacts": [
            {"fmt": "deb", "arch": "amd64", "suite": None},
            {"fmt": "rpm", "arch": "amd64", "suite": None},
            {"fmt": "deb", "arch": "arm64", "suite": None},
        ]
    }


def test_manifest_noarch_and_native() -> None:
    assert matrix.manifest(matrix.resolve(_pkg(noarch=True))) == {
        "artifacts": [
            {"fmt": "deb", "arch": "all", "suite": None},
            {"fmt": "rpm", "arch": "all", "suite": None},
        ]
    }
    m = matrix.resolve(_pkg(targets=["ubuntu/26.04/arm64"], native=["ubuntu/*/*"]))
    assert matrix.manifest(m) == {
        "artifacts": [{"fmt": "deb", "arch": "arm64", "suite": "resolute"}],
    }


def test_select_targets_default_source_label() -> None:
    with pytest.raises(ConfigError, match=r"^vergil\.toml: \[package\] selects no targets"):
        matrix.select_targets(_pkg(exclude=["*/*/*"]))


def test_select_targets_dedupes_overlapping_patterns() -> None:
    keys = [t.key for t in matrix.select_targets(_pkg(targets=["rhel/*/*", "rhel/9/*"]))]
    assert keys == ["rhel/10/amd64", "rhel/10/arm64", "rhel/9/amd64", "rhel/9/arm64"]
