"""Tests for vergil_tooling.lib.package.matrix (spec §5.3)."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.config import (
    ConfigError,
    PackageConfig,
    PackagePythonConfig,
    PackageStagedConfig,
)
from vergil_tooling.lib.package import matrix

if TYPE_CHECKING:
    from pathlib import Path


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
    m = matrix.resolve(_pkg(), "t")
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
    m = matrix.resolve(_pkg(), "t")
    cell = next(t for t in m.test if t.target == "rhel/9/arm64")
    assert (cell.id, cell.image, cell.fmt, cell.suite) == (
        "test-rhel-9-arm64",
        "registry.access.redhat.com/ubi9/ubi",
        "rpm",
        "el9",
    )


def test_exclude_drops_rhel_arm() -> None:
    m = matrix.resolve(_pkg(exclude=["rhel/*/arm64"]), "t")
    assert [(c.id, c.fmts) for c in m.build] == [
        ("shared-amd64", ("deb", "rpm")),
        ("shared-arm64", ("deb",)),
    ]
    assert len(m.test) == 6


def test_targets_subset_on_one_arch_gives_one_cell() -> None:
    m = matrix.resolve(_pkg(targets=["ubuntu/*/amd64"]), "t")
    assert [(c.id, c.fmts) for c in m.build] == [("shared-amd64", ("deb",))]
    assert [t.target for t in m.test] == ["ubuntu/24.04/amd64", "ubuntu/26.04/amd64"]


def test_native_gets_its_own_cell_in_its_os() -> None:
    m = matrix.resolve(_pkg(native=["ubuntu/26.04/*"]), "t")
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
    m = matrix.resolve(_pkg(targets=["rhel/9/*"], native=["*/*/*"]), "t")
    assert [c.id for c in m.build] == ["native-rhel-9-amd64", "native-rhel-9-arm64"]


def test_noarch_builds_once_on_amd64() -> None:
    m = matrix.resolve(_pkg(noarch=True), "t")
    assert [(c.id, c.arch, c.runner, c.fmts) for c in m.build] == [
        ("shared-noarch", "amd64", "ubuntu-24.04", ("deb", "rpm")),
    ]
    assert len(m.build[0].targets) == 8
    assert len(m.test) == 8


def test_json_and_manifest_shapes() -> None:
    m = matrix.resolve(_pkg(exclude=["rhel/*/arm64"]), "t")
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


def test_runtime_rides_every_build_cell_and_the_manifest() -> None:
    m = matrix.resolve(_pkg(), "t")
    j = matrix.to_json(m, runtimes={"t": "3.14"})
    assert [c["runtime"] for c in j["build"]] == ["3.14", "3.14"]
    assert all("runtime" not in c for c in j["test"])
    without = matrix.to_json(m)
    assert all("runtime" not in c for c in without["build"])
    assert [{k: v for k, v in c.items() if k != "runtime"} for c in j["build"]] == without["build"]
    assert matrix.manifest(m, runtimes={"t": "3.14"}) == {
        **matrix.manifest(m),
        "runtimes": {"t": "3.14"},
        "runtime": "3.14",
    }
    assert "runtime" not in matrix.manifest(m)
    staged = matrix.manifest(m, runtimes={"t": None})
    assert staged == {**matrix.manifest(m), "runtimes": {"t": None}}
    assert all("runtime" not in c for c in matrix.to_json(m, runtimes={"t": None})["build"])


def test_manifest_noarch_and_native() -> None:
    assert matrix.manifest(matrix.resolve(_pkg(noarch=True), "t")) == {
        "artifacts": [
            {"fmt": "deb", "arch": "all", "suite": None},
            {"fmt": "rpm", "arch": "all", "suite": None},
        ]
    }
    m = matrix.resolve(_pkg(targets=["ubuntu/26.04/arm64"], native=["ubuntu/*/*"]), "t")
    assert matrix.manifest(m) == {
        "artifacts": [{"fmt": "deb", "arch": "arm64", "suite": "resolute"}],
    }


def test_select_targets_default_source_label() -> None:
    with pytest.raises(ConfigError, match=r"^vergil\.toml: \[package\] selects no targets"):
        matrix.select_targets(_pkg(exclude=["*/*/*"]))


def test_select_targets_dedupes_overlapping_patterns() -> None:
    keys = [t.key for t in matrix.select_targets(_pkg(targets=["rhel/*/*", "rhel/9/*"]))]
    assert keys == ["rhel/10/amd64", "rhel/10/arm64", "rhel/9/amd64", "rhel/9/arm64"]


# --- tiers (issue #3127) -----------------------------------------------------


def _reduced(**kw: Any) -> matrix.Matrix:
    return matrix.resolve(_pkg(**kw), "t", tier=matrix.Tier.REDUCED)


_TIER_CASES: list[dict[str, Any]] = [
    {},
    {"exclude": ["rhel/*/arm64"]},
    {"native": ["ubuntu/26.04/*"]},
    {"noarch": True},
]


def test_tier_values_are_the_cli_choices() -> None:
    assert [t.value for t in matrix.Tier] == ["full", "reduced"]


@pytest.mark.parametrize("kw", _TIER_CASES)
def test_full_tier_is_the_default(kw: dict[str, Any]) -> None:
    assert matrix.resolve(_pkg(**kw), "t", tier=matrix.Tier.FULL) == matrix.resolve(_pkg(**kw), "t")


@pytest.mark.parametrize("kw", _TIER_CASES)
def test_reduced_keeps_every_build_cell_and_a_subset_of_test_cells(kw: dict[str, Any]) -> None:
    full = matrix.resolve(_pkg(**kw), "t")
    m = _reduced(**kw)
    assert m.build == full.build
    assert all(t in full.test for t in m.test)
    assert matrix.manifest(m) == matrix.manifest(full)


def test_reduced_default_is_one_amd64_test_per_format() -> None:
    m = _reduced()
    # The OLDEST release of each format is tested (strictest compatibility case:
    # lowest glibc, oldest rpm/dnf/systemd), compared numerically, so RHEL 9 is
    # chosen even though the key "rhel/10" sorts before "rhel/9" as text.
    assert [(t.id, t.runner, t.fmt) for t in m.test] == [
        ("test-rhel-9-amd64", "ubuntu-24.04", "rpm"),
        ("test-ubuntu-24.04-amd64", "ubuntu-24.04", "deb"),
    ]


def test_reduced_with_rhel_arm_excluded() -> None:
    m = _reduced(exclude=["rhel/*/arm64"])
    assert [c.id for c in m.build] == ["shared-amd64", "shared-arm64"]
    assert [t.target for t in m.test] == ["rhel/9/amd64", "ubuntu/24.04/amd64"]


def test_reduced_picks_the_oldest_release_numerically_not_textually() -> None:
    m = _reduced(
        targets=["rhel/10/amd64", "rhel/9/amd64", "ubuntu/26.04/amd64", "ubuntu/24.04/amd64"]
    )
    assert [t.target for t in m.test] == ["rhel/9/amd64", "ubuntu/24.04/amd64"]


def test_reduced_deb_only_product() -> None:
    m = _reduced(targets=["ubuntu/*/*"])
    assert [(c.id, c.fmts) for c in m.build] == [
        ("shared-amd64", ("deb",)),
        ("shared-arm64", ("deb",)),
    ]
    assert [t.target for t in m.test] == ["ubuntu/24.04/amd64"]


def test_reduced_falls_back_to_arm64_when_no_amd64_target() -> None:
    m = _reduced(targets=["*/*/arm64"])
    assert [c.id for c in m.build] == ["shared-arm64"]
    assert [(t.target, t.runner) for t in m.test] == [
        ("rhel/9/arm64", "ubuntu-24.04-arm"),
        ("ubuntu/24.04/arm64", "ubuntu-24.04-arm"),
    ]


def test_reduced_prefers_amd64_even_when_an_arm64_target_sorts_first() -> None:
    m = _reduced(targets=["ubuntu/24.04/arm64", "ubuntu/26.04/amd64"])
    assert [t.target for t in m.test] == ["ubuntu/26.04/amd64"]


def test_reduced_skips_native_targets_when_a_shared_one_of_the_format_exists() -> None:
    m = _reduced(native=["ubuntu/24.04/*", "rhel/10/amd64"])
    assert [(t.target, t.native) for t in m.test] == [
        ("rhel/9/amd64", False),
        ("ubuntu/26.04/amd64", False),
    ]


def test_reduced_shared_arm64_beats_native_amd64() -> None:
    m = _reduced(targets=["rhel/9/*"], native=["rhel/9/amd64"])
    assert [(t.target, t.native) for t in m.test] == [("rhel/9/arm64", False)]


def test_reduced_uses_native_targets_only_when_no_shared_target_of_the_format() -> None:
    m = _reduced(targets=["rhel/9/*", "ubuntu/*/*"], native=["rhel/*/*"])
    assert [c.id for c in m.build] == [
        "shared-amd64",
        "shared-arm64",
        "native-rhel-9-amd64",
        "native-rhel-9-arm64",
    ]
    assert [(t.target, t.native) for t in m.test] == [
        ("rhel/9/amd64", True),
        ("ubuntu/24.04/amd64", False),
    ]


def test_reduced_all_native_arm64_only() -> None:
    m = _reduced(targets=["rhel/9/arm64"], native=["*/*/*"])
    assert [c.id for c in m.build] == ["native-rhel-9-arm64"]
    assert [(t.target, t.native) for t in m.test] == [("rhel/9/arm64", True)]


def test_reduced_noarch() -> None:
    m = _reduced(noarch=True)
    assert [c.id for c in m.build] == ["shared-noarch"]
    assert [t.target for t in m.test] == ["rhel/9/amd64", "ubuntu/24.04/amd64"]


def test_to_json_is_tier_agnostic() -> None:
    assert set(matrix.to_json(_reduced())) == {"build", "test"}


# --- the package dimension (epic vergil-project/.github#472, spec §5.2) -----


def _tooling_repo(tmp_path: Path) -> Path:
    """A repo shaped like vergil-tooling: the package name comes from pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vergil-tooling"\n')
    return tmp_path


def _tooling_pkg() -> PackageConfig:
    """vergil-tooling's own ``[package]``: python builder, no name, every default target."""
    return PackageConfig(
        builder="python",
        vendor="vergil",
        summary="s",
        smoke="vrg-whoami --mode",
        python=PackagePythonConfig(),
    )


def _series(name: str) -> PackageConfig:
    return PackageConfig(
        builder="staged",
        vendor="vergil",
        summary="s",
        smoke="true",
        name=name,
        upstream_version="3.15.1+20261021",
        staged=PackageStagedConfig(build_command="true"),
    )


def test_single_package_cells_unchanged(tmp_path: Path) -> None:
    """A single-package repo keeps its cell ids and manifest artifacts byte for byte."""
    m = matrix.resolve_all(_tooling_repo(tmp_path), [_tooling_pkg()])
    assert [c.id for c in m.build] == ["shared-amd64", "shared-arm64"]
    assert [t.id for t in m.test] == [
        "test-rhel-10-amd64",
        "test-rhel-10-arm64",
        "test-rhel-9-amd64",
        "test-rhel-9-arm64",
        "test-ubuntu-24.04-amd64",
        "test-ubuntu-24.04-arm64",
        "test-ubuntu-26.04-amd64",
        "test-ubuntu-26.04-arm64",
    ]
    assert {c.package for c in (*m.build, *m.test)} == {"vergil-tooling"}
    assert json.dumps(matrix.manifest(m)["artifacts"]) == json.dumps(
        [
            {"fmt": "deb", "arch": "amd64", "suite": None},
            {"fmt": "rpm", "arch": "amd64", "suite": None},
            {"fmt": "deb", "arch": "arm64", "suite": None},
            {"fmt": "rpm", "arch": "arm64", "suite": None},
        ]
    )
    # The single package keeps the top-level runtime older readers check.
    assert matrix.manifest(m, runtimes={"vergil-tooling": "3.15"})["runtime"] == "3.15"
    reduced = matrix.resolve_all(tmp_path, [_tooling_pkg()], tier=matrix.Tier.REDUCED)
    assert [t.id for t in reduced.test] == ["test-rhel-9-amd64", "test-ubuntu-24.04-amd64"]


def test_multi_package_cells_qualified(tmp_path: Path) -> None:
    m = matrix.resolve_all(tmp_path, [_series("vergil-python3.14"), _series("vergil-python3.15")])
    ids = [c.id for c in m.build]
    assert ids == [
        "vergil-python3.14--shared-amd64",
        "vergil-python3.14--shared-arm64",
        "vergil-python3.15--shared-amd64",
        "vergil-python3.15--shared-arm64",
    ]
    test_ids = [t.id for t in m.test]
    assert len(set(test_ids)) == len(test_ids) == 16
    assert "vergil-python3.15--test-rhel-9-arm64" in test_ids
    assert all(c.id.startswith(f"{c.package}--") for c in (*m.build, *m.test))


def test_multi_reduced_tier_tests_every_package(tmp_path: Path) -> None:
    m = matrix.resolve_all(
        tmp_path,
        [_series("vergil-python3.14"), _series("vergil-python3.15")],
        tier=matrix.Tier.REDUCED,
    )
    assert [t.id for t in m.test] == [
        "vergil-python3.14--test-rhel-9-amd64",
        "vergil-python3.14--test-ubuntu-24.04-amd64",
        "vergil-python3.15--test-rhel-9-amd64",
        "vergil-python3.15--test-ubuntu-24.04-amd64",
    ]


def test_manifest_names_artifacts(tmp_path: Path) -> None:
    m = matrix.resolve_all(tmp_path, [_series("vergil-python3.14"), _series("vergil-python3.15")])
    runtimes = {"vergil-python3.14": None, "vergil-python3.15": None}
    got = matrix.manifest(m, runtimes=runtimes)
    assert got["runtimes"] == runtimes
    assert "runtime" not in got
    assert len(got["artifacts"]) == 8
    assert all("name" in a for a in got["artifacts"])
    assert [a["name"] for a in got["artifacts"]].count("vergil-python3.15") == 4
    assert all("runtime" not in c for c in matrix.to_json(m, runtimes=runtimes)["build"])


def test_multi_noarch_and_per_package_runtime(tmp_path: Path) -> None:
    noarch = dataclasses.replace(_series("data"), noarch=True)
    py = PackageConfig(
        builder="python",
        vendor="vergil",
        summary="s",
        smoke="true",
        name="tool",
        python=PackagePythonConfig(),
        targets=["ubuntu/24.04/amd64"],
    )
    m = matrix.resolve_all(tmp_path, [noarch, py])
    assert [c.id for c in m.build] == ["data--shared-noarch", "tool--shared-amd64"]
    runtimes = {"data": None, "tool": "3.15"}
    man = matrix.manifest(m, runtimes=runtimes)
    assert man["artifacts"] == [
        {"fmt": "deb", "arch": "all", "suite": None, "name": "data"},
        {"fmt": "rpm", "arch": "all", "suite": None, "name": "data"},
        {"fmt": "deb", "arch": "amd64", "suite": None, "name": "tool"},
    ]
    assert man["runtimes"] == runtimes
    assert "runtime" not in man
    build = matrix.to_json(m, runtimes=runtimes)["build"]
    assert [c.get("runtime") for c in build] == [None, "3.15"]
    assert [c["package"] for c in build] == ["data", "tool"]


def test_named_refuses_two_entries_with_one_name(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"two \[\[package\]\] entries resolve to .*'a'"):
        matrix.named(tmp_path, [_series("a"), _series("a")])
