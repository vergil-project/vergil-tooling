"""Tests for vergil_tooling.lib.update_deps.vergil_index (spec §8.2, vergil-tooling#3456)."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from vergil_tooling.lib.config import PackageConfig, PackagePythonConfig
from vergil_tooling.lib.package import matrix, python_builder
from vergil_tooling.lib.update_deps import vergil_index
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.vergil_index import ServedRuntime

if TYPE_CHECKING:
    from collections.abc import Callable

_MOD = "vergil_tooling.lib.update_deps.vergil_index"
_PKG = PackageConfig(
    builder="python",
    vendor="vergil",
    summary="s",
    smoke="x",
    python=PackagePythonConfig(runtime="3.14.8"),
)
_REPOMD = (
    b'<repomd xmlns="http://linux.duke.edu/metadata/repo"><data type="primary">'
    b'<location href="repodata/primary.xml.gz"/></data></repomd>'
)
_TWO = [("deb", "noble", "amd64"), ("rpm", "el9", "x86_64")]

Pkgs = tuple[tuple[str, str], ...]


def _deb(*pkgs: tuple[str, str]) -> bytes:
    return "\n\n".join(
        f"Package: {n}\nVersion: {v}-1\nArchitecture: amd64" for n, v in pkgs
    ).encode()


def _rpm_primary(*pkgs: tuple[str, str]) -> bytes:
    primary = (
        '<metadata xmlns="http://linux.duke.edu/metadata/common">'
        + "".join(
            f'<package><name>{n}</name><version ver="{v}" rel="1"/></package>' for n, v in pkgs
        )
        + "</metadata>"
    )
    return gzip.compress(primary.encode())


def _fetch(deb: Pkgs, rpm: Pkgs) -> Callable[[str], bytes]:
    primary = _rpm_primary(*rpm)

    def fetch(url: str) -> bytes:
        if url.endswith("/Packages"):
            return _deb(*deb)
        if url.endswith("repomd.xml"):
            return _REPOMD
        if url.endswith("primary.xml.gz"):
            return primary
        raise AssertionError(url)

    return fetch


@pytest.fixture
def two_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_MOD + ".runtime_indexes", lambda pkg: _TWO)


def test_runtime_indexes_cover_shared_suite_and_every_target() -> None:
    indexes = vergil_index.runtime_indexes(_PKG)
    assert indexes[:2] == [
        ("deb", python_builder.SHARED_SUITE, "amd64"),
        ("deb", python_builder.SHARED_SUITE, "arm64"),
    ]
    for t in matrix.select_targets(_PKG):
        expected = (t.fmt, t.suite, t.arch if t.fmt == "deb" else t.rpm_arch)
        assert expected in indexes
    assert ("rpm", "el9", "x86_64") in indexes
    assert ("rpm", "el10", "aarch64") in indexes
    assert len(indexes) == len(set(indexes))


def test_runtime_indexes_keep_shared_suite_when_targets_exclude_it() -> None:
    pkg = PackageConfig(
        builder="python",
        vendor="vergil",
        summary="s",
        smoke="x",
        targets=["rhel/9/*"],
        python=PackagePythonConfig(runtime="3.14.8"),
    )
    assert vergil_index.runtime_indexes(pkg) == [
        ("deb", python_builder.SHARED_SUITE, "amd64"),
        ("deb", python_builder.SHARED_SUITE, "arm64"),
        ("rpm", "el9", "x86_64"),
        ("rpm", "el9", "aarch64"),
    ]


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_reads_the_series_package_patch() -> None:
    # The series package serves 3.14.9; the legacy per-patch name is ignored.
    pkgs = (
        ("vergil-python3.14", "3.14.9+20261020"),
        ("vergil-python3.14.8", "3.14.8+20261003"),
        ("vergil-tooling", "2.1.258"),
    )
    served = vergil_index.served_runtime(_PKG, fetch=_fetch(pkgs, pkgs))
    assert served == ServedRuntime(
        package="vergil-python3.14",
        patches={"deb noble/amd64": "3.14.9", "rpm el9/x86_64": "3.14.9"},
        newer_series=None,
    )


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_takes_the_newest_listed_version() -> None:
    # apt/dnf install the newest version an index lists, so that is the served patch.
    pkgs = (
        ("vergil-python3.14", "3.14.8+20261013"),
        ("vergil-python3.14", "3.14.10+20261001"),
        ("vergil-python3.14", "3.14.9+20261030"),
    )
    served = vergil_index.served_runtime(_PKG, fetch=_fetch(pkgs, pkgs))
    assert served.patch == "3.14.10"


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_reports_newer_series_served_everywhere() -> None:
    deb = (
        ("vergil-python3.14", "3.14.8+20261013"),
        ("vergil-python3.15", "3.15.1+20261021"),
        ("vergil-python3.16", "3.16.0+20261022"),  # deb only: not everywhere
        ("vergil-python3.13", "3.13.9+20261001"),  # older series
    )
    rpm = (
        ("vergil-python3.14", "3.14.8+20261013"),
        ("vergil-python3.15", "3.15.1+20261021"),
    )
    served = vergil_index.served_runtime(_PKG, fetch=_fetch(deb, rpm))
    assert served.patch == "3.14.8"
    assert served.newer_series == "3.15"


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_never_takes_the_patch_from_another_series() -> None:
    pkgs = (("vergil-python3.14", "3.14.8+20261013"), ("vergil-python3.15", "3.15.1+20261021"))
    assert vergil_index.served_runtime(_PKG, fetch=_fetch(pkgs, pkgs)).patch == "3.14.8"


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_missing_series_package_fails_loud() -> None:
    # Only the legacy per-patch name is left: a loud error that explains the migration.
    deb = (("vergil-python3.14", "3.14.9+20261020"),)
    rpm = (("vergil-python3.14.8", "3.14.8+20261003"),)
    with pytest.raises(UpdateDepsError, match="vergil-python3.14 is not in") as info:
        vergil_index.served_runtime(_PKG, fetch=_fetch(deb, rpm))
    assert "rpm el9/x86_64" in str(info.value)
    assert "deb noble/amd64" not in str(info.value)
    assert "per-patch" in str(info.value)


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_unpublished_rpm_index_fails_loud() -> None:
    def fetch(url: str) -> bytes:
        return _deb(("vergil-python3.14", "3.14.9+20261020")) if url.endswith("/Packages") else b""

    with pytest.raises(UpdateDepsError, match="vergil-python3.14 is not in"):
        vergil_index.served_runtime(_PKG, fetch=fetch)


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_indexes_disagreeing_have_no_patch() -> None:
    # Not an error: the deb and rpm indexes briefly disagree after each release.
    deb = (("vergil-python3.14", "3.14.9+20261020"),)
    rpm = (("vergil-python3.14", "3.14.8+20261013"),)
    served = vergil_index.served_runtime(_PKG, fetch=_fetch(deb, rpm))
    assert served.patch is None
    assert served.patches == {"deb noble/amd64": "3.14.9", "rpm el9/x86_64": "3.14.8"}
    assert served.disagreement() == "3.14.9 on deb noble/amd64; 3.14.8 on rpm el9/x86_64"


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_same_patch_different_builds_agree() -> None:
    deb = (("vergil-python3.14", "3.14.9+20261021"),)
    rpm = (("vergil-python3.14", "3.14.9+20261020"),)
    assert vergil_index.served_runtime(_PKG, fetch=_fetch(deb, rpm)).patch == "3.14.9"


def test_served_parser_accepts_release_suffix() -> None:
    """``<cpython>+<pbs_tag>.<X.Y.Z>`` (epic .github#472, spec §5.5) orders like dpkg/rpm."""

    def key(v: str) -> tuple[tuple[int, ...], int, tuple[int, ...]]:
        return vergil_index._served_key(v, "vergil-python3.15", "deb noble/amd64")  # noqa: SLF001

    assert key("3.15.1+20261021.1.0.5") == ((3, 15, 1), 20261021, (1, 0, 5))
    assert key("3.15.1+20261021.1.0.5") > key("3.15.1+20261021")
    assert key("3.15.1+20261021.1.0.10") > key("3.15.1+20261021.1.0.9")
    assert key("3.15.2+20261101.1.0.5") > key("3.15.1+20261021.1.0.6")
    with pytest.raises(UpdateDepsError, match="<cpython>\\+<pbs_tag>"):
        key("3.15.1")


@pytest.mark.usefixtures("two_indexes")
def test_served_runtime_reads_the_patch_of_a_release_suffixed_version() -> None:
    pkgs = (
        ("vergil-python3.14", "3.14.9+20261021"),
        ("vergil-python3.14", "3.14.9+20261021.1.0.5"),
        ("vergil-python3.14", "3.14.8+20261003.1.0.9"),
    )
    assert vergil_index.served_runtime(_PKG, fetch=_fetch(pkgs, pkgs)).patch == "3.14.9"


@pytest.mark.usefixtures("two_indexes")
@pytest.mark.parametrize(
    "version",
    ["3.14.9", "3.14+20261020", "3.14.9+abc", "x+1", "3.14.9+20261020.1", "3.14.9+20261020.1.0"],
)
def test_served_runtime_malformed_version_fails_loud(version: str) -> None:
    pkgs = (("vergil-python3.14", version),)
    with pytest.raises(UpdateDepsError, match="<cpython>\\+<pbs_tag>"):
        vergil_index.served_runtime(_PKG, fetch=_fetch(pkgs, pkgs))


def test_served_runtime_without_pin_fails_loud() -> None:
    for python in (None, PackagePythonConfig()):
        pkg = PackageConfig(
            builder="python", vendor="vergil", summary="s", smoke="x", python=python
        )
        with pytest.raises(UpdateDepsError, match="no \\[package.python\\].runtime pin"):
            vergil_index.served_runtime(pkg, fetch=lambda url: b"")


def test_served_runtime_lookup_error_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_MOD + ".runtime_indexes", lambda pkg: [("rpm", "el9", "x86_64")])

    def fetch(url: str) -> NoReturn:
        raise OSError("connection reset")

    with pytest.raises(UpdateDepsError, match="connection reset") as info:
        vergil_index.served_runtime(_PKG, fetch=fetch)
    assert "el9/x86_64" in info.value.command
    assert isinstance(info.value.__cause__, OSError)


def test_served_runtime_for_reads_package(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cfg:
        packages = (_PKG,)

    expected = ServedRuntime(
        package="vergil-python3.14", patches={"deb noble/amd64": "3.14.9"}, newer_series=None
    )
    monkeypatch.setattr(_MOD + ".config.read_config", lambda base: _Cfg())
    monkeypatch.setattr(_MOD + ".served_runtime", lambda pkg: expected)
    assert vergil_index.served_runtime_for(Path("/r")) == expected


def test_served_runtime_for_without_package_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cfg:
        packages = ()

    monkeypatch.setattr(_MOD + ".config.read_config", lambda base: _Cfg())
    with pytest.raises(UpdateDepsError, match=r"\[package\] is missing"):
        vergil_index.served_runtime_for(Path("/r"))


def _entry(name: str, builder: str = "python", runtime: str | None = None) -> PackageConfig:
    return PackageConfig(
        builder=builder,
        vendor="vergil",
        summary="s",
        smoke="x",
        name=name,
        python=PackagePythonConfig(runtime=runtime) if builder == "python" else None,
    )


def test_served_runtime_for_reads_the_pinned_entry_of_a_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = _entry("b", runtime="3.14.8")

    class _Cfg:
        packages = (_entry("a", builder="staged"), pinned, _entry("c"))

    seen: list[PackageConfig] = []
    expected = ServedRuntime(package="vergil-python3.14", patches={}, newer_series=None)
    monkeypatch.setattr(_MOD + ".config.read_config", lambda base: _Cfg())

    def served(pkg: PackageConfig) -> ServedRuntime:
        seen.append(pkg)
        return expected

    monkeypatch.setattr(_MOD + ".served_runtime", served)
    assert vergil_index.served_runtime_for(Path("/r")) == expected
    assert seen == [pinned]


def test_served_runtime_for_unpinned_list_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cfg:
        packages = (_entry("a", builder="staged"), _entry("b"))

    monkeypatch.setattr(_MOD + ".config.read_config", lambda base: _Cfg())
    with pytest.raises(UpdateDepsError, match=r"no \[package\.python\]\.runtime pin"):
        vergil_index.served_runtime_for(Path("/r"))


def test_served_runtime_for_two_pins_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cfg:
        packages = (_entry("a", runtime="3.14.8"), _entry("b", runtime="3.15.1"))

    monkeypatch.setattr(_MOD + ".config.read_config", lambda base: _Cfg())
    with pytest.raises(UpdateDepsError) as info:
        vergil_index.served_runtime_for(Path("/r"))
    assert str(info.value) == "several [[package]] entries pin a runtime; pin it on one entry"
    assert info.value.command == "served_runtime_for"
