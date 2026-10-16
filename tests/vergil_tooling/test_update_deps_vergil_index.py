"""Tests for vergil_tooling.lib.update_deps.vergil_index (spec §8.2)."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import pytest

from vergil_tooling.lib.config import PackageConfig, PackagePythonConfig
from vergil_tooling.lib.package import matrix, python_builder
from vergil_tooling.lib.update_deps import vergil_index
from vergil_tooling.lib.update_deps.context import UpdateDepsError

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


def _deb(*names: str) -> bytes:
    return "\n\n".join(f"Package: {n}\nVersion: 1\nArchitecture: all" for n in names).encode()


def _rpm_primary(*names: str) -> bytes:
    primary = (
        '<metadata xmlns="http://linux.duke.edu/metadata/common">'
        + "".join(
            f'<package><name>{n}</name><version ver="1" rel="1"/></package>' for n in names
        )
        + "</metadata>"
    )
    return gzip.compress(primary.encode())


def _fetch(deb_names: tuple[str, ...], rpm_names: tuple[str, ...]) -> Callable[[str], bytes]:
    primary = _rpm_primary(*rpm_names)

    def fetch(url: str) -> bytes:
        if url.endswith("/Packages"):
            return _deb(*deb_names)
        if url.endswith("repomd.xml"):
            return _REPOMD
        if url.endswith("primary.xml.gz"):
            return primary
        raise AssertionError(url)

    return fetch


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


def test_served_runtimes_requires_every_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _MOD + ".runtime_indexes",
        lambda pkg: [("deb", "noble", "amd64"), ("rpm", "el9", "x86_64")],
    )
    fetch = _fetch(
        ("vergil-python3.14.8", "vergil-python3.14.9", "vergil-tooling"),
        ("vergil-python3.14.8",),  # rpm not yet published for 3.14.9
    )
    assert vergil_index.served_runtimes(_PKG, fetch=fetch) == {"3.14.8"}


def test_served_runtimes_empty_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_MOD + ".runtime_indexes", lambda pkg: [("deb", "noble", "amd64")])
    with pytest.raises(UpdateDepsError, match="no vergil-python runtime"):
        vergil_index.served_runtimes(_PKG, fetch=lambda url: b"")


def test_served_runtimes_unpublished_rpm_index_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _MOD + ".runtime_indexes",
        lambda pkg: [("deb", "noble", "amd64"), ("rpm", "el9", "x86_64")],
    )

    def fetch(url: str) -> bytes:
        return _deb("vergil-python3.14.8") if url.endswith("/Packages") else b""

    with pytest.raises(UpdateDepsError, match="no vergil-python runtime"):
        vergil_index.served_runtimes(_PKG, fetch=fetch)


def test_served_runtimes_lookup_error_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_MOD + ".runtime_indexes", lambda pkg: [("rpm", "el9", "x86_64")])

    def fetch(url: str) -> NoReturn:
        raise OSError("connection reset")

    with pytest.raises(UpdateDepsError, match="connection reset") as info:
        vergil_index.served_runtimes(_PKG, fetch=fetch)
    assert "el9/x86_64" in info.value.command
    assert isinstance(info.value.__cause__, OSError)


def test_served_runtimes_for_reads_package(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cfg:
        package = _PKG

    monkeypatch.setattr(_MOD + ".config.read_config", lambda base: _Cfg())
    monkeypatch.setattr(_MOD + ".served_runtimes", lambda pkg: {"3.14.9"})
    assert vergil_index.served_runtimes_for(Path("/r")) == {"3.14.9"}


def test_served_runtimes_for_without_package_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Cfg:
        package = None

    monkeypatch.setattr(_MOD + ".config.read_config", lambda base: _Cfg())
    with pytest.raises(UpdateDepsError, match=r"\[package\]"):
        vergil_index.served_runtimes_for(Path("/r"))


@pytest.mark.parametrize(
    ("current", "served", "expected"),
    [
        ("3.14.8", {"3.14.8", "3.14.9", "3.14.10", "3.15.1"}, "3.14.10"),
        ("3.14.10", {"3.14.8", "3.14.10"}, None),
        ("3.14.8", {"3.15.0"}, None),
    ],
)
def test_newest_patch(current: str, served: set[str], expected: str | None) -> None:
    assert vergil_index.newest_patch(current, served) == expected


def test_newer_minor() -> None:
    assert vergil_index.newer_minor("3.14.8", {"3.14.9", "3.15.0", "3.15.2"}) == "3.15.2"
    assert vergil_index.newer_minor("3.14.8", {"3.14.9"}) is None
