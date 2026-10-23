"""The vergil-python runtime the live vergil package repository serves (spec §8.2).

vergil-python publishes **one package per series**, ``vergil-python<x.y>``,
whose version is ``<cpython>+<pbs_tag>``: the CPython patch floats as new builds
are published (vergil-tooling#3455). The package build installs the series
package from the shared deb suite (non-native cells) or the target's own suite
(native cells), and install-test resolves it from every selected target's
index. Each index installs the **newest** version it lists, so the patch a
series serves is the CPython part of that newest version, and it counts only
when **every** one of those indexes serves the same patch. A patch any of them
does not serve would merge green and then fail the next release.

The rpm package carries the same name as the deb (``vergil-python<x.y>``, from
vergil-python's ``[package].name``); both are read the same way. The legacy
per-patch names (``vergil-python<x.y.z>``) are retired (vergil-python#37) and
never read: a missing series package is a loud error that says so
(vergil-tooling#3456).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # noqa: N817
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import config
from vergil_tooling.lib.package import matrix, orgs, python_builder
from vergil_tooling.lib.package.targets import ARCHES
from vergil_tooling.lib.release import package_index
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.versions.extensions.python import runtime_series

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.config import PackageConfig

# The runtime package always comes from the vergil org (spec D7).
_RUNTIME_VENDOR = "vergil"
_SERIES_PKG_RE = re.compile(r"^vergil-python(\d+\.\d+)$")
# A series package's upstream version: ``<cpython x.y.z>+<pbs_tag>``.
_SERVED_RE = re.compile(r"^(\d+\.\d+\.\d+)\+(\d+)$")


@dataclass(frozen=True)
class ServedRuntime:
    """What the pinned runtime's series serves on every build and install-test index."""

    # The CPython patch ``vergil-python<x.y>`` serves (the part before ``+``).
    patch: str
    # The newest later ``x.y`` series package every index lists, if any. It is
    # reported, never applied: moving to a new series is a reviewed change.
    newer_series: str | None


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def runtime_indexes(pkg: PackageConfig) -> list[tuple[str, str, str]]:
    """``(fmt, suite, arch)`` of every index the build or install-test reads the runtime from.

    ``arch`` is the deb arch for deb indexes and the rpm arch for rpm ones.
    """
    indexes = [("deb", python_builder.SHARED_SUITE, arch) for arch in ARCHES]
    for t in matrix.select_targets(pkg):
        entry = (t.fmt, t.suite, t.arch if t.fmt == "deb" else t.rpm_arch)
        if entry not in indexes:
            indexes.append(entry)
    return indexes


def _versions(
    base: str, fmt: str, suite: str, arch: str, fetch: package_index.Fetch
) -> dict[str, set[str]]:
    """Package versions in one index; a failed lookup raises ``UpdateDepsError``."""
    try:
        if fmt == "deb":
            return package_index.deb_package_versions(base, suite, arch, fetch)
        return package_index.rpm_package_versions(base, suite, arch, fetch)
    except (ReleaseError, OSError, ET.ParseError) as exc:
        raise UpdateDepsError(
            phase="update",
            command=f"list {fmt} packages in {base} ({suite}/{arch})",
            message=f"Could not read the vergil package index: {exc}",
        ) from exc


def _served_key(version: str, rt_pkg: str, where: str) -> tuple[tuple[int, ...], int]:
    """The ordering key of a series package version; a malformed one fails loud."""
    match = _SERVED_RE.fullmatch(version)
    if match is None:
        raise UpdateDepsError(
            phase="update",
            command=f"read {rt_pkg} versions ({where})",
            message=(
                f"{rt_pkg} {version!r} in {where} is not a <cpython>+<pbs_tag> version, "
                "so the CPython patch it serves cannot be read."
            ),
        )
    return _key(match.group(1)), int(match.group(2))


def served_runtime(
    pkg: PackageConfig,
    *,
    fetch: package_index.Fetch = package_index._fetch,  # noqa: SLF001
) -> ServedRuntime:
    """The patch the pin's ``vergil-python<x.y>`` serves on every index; fail loud otherwise.

    Fails when the package has no ``[package.python].runtime`` pin, when any index
    lacks the series package or cannot be read, when a version is malformed, and
    when the indexes serve different CPython patches.
    """
    pin = pkg.python.runtime if pkg.python is not None else None
    if pin is None:
        raise UpdateDepsError(
            phase="update",
            command="served_runtime",
            message="[package] has no [package.python].runtime pin, so there is nothing to bump.",
        )
    rt_pkg = python_builder.runtime_package(runtime_series(pin))
    base = orgs.for_vendor(_RUNTIME_VENDOR).base_url
    newest: dict[str, str] = {}
    missing: list[str] = []
    series_per_index: list[set[str]] = []
    for fmt, suite, arch in runtime_indexes(pkg):
        where = f"{fmt} {suite}/{arch}"
        versions = _versions(base, fmt, suite, arch, fetch)
        series_per_index.append({m.group(1) for n in versions if (m := _SERIES_PKG_RE.match(n))})
        if rt_pkg not in versions:
            missing.append(where)
            continue
        newest[where] = max(versions[rt_pkg], key=lambda v: _served_key(v, rt_pkg, where))
    if missing:
        raise UpdateDepsError(
            phase="update",
            command=f"list {rt_pkg} in {base}",
            message=(
                f"{rt_pkg} is not in the vergil package index for {', '.join(missing)}. "
                f"vergil-python publishes one package per series (vergil-python<x.y>); the "
                f"legacy per-patch packages (vergil-python<x.y.z>) are retired and are not "
                f"read. Refusing to treat a missing runtime as 'no change'."
            ),
        )
    patches = {v.split("+", 1)[0] for v in newest.values()}
    if len(patches) != 1:
        listing = "; ".join(f"{where}: {v}" for where, v in newest.items())
        raise UpdateDepsError(
            phase="update",
            command=f"list {rt_pkg} in {base}",
            message=(
                f"The build and install-test indexes disagree on the CPython patch {rt_pkg} "
                f"serves ({listing}), so no pin builds on all of them. Wait for the package "
                f"index to finish publishing, then re-run."
            ),
        )
    series = runtime_series(pin)
    newer = [s for s in set.intersection(*series_per_index) if _key(s) > _key(series)]
    return ServedRuntime(patch=patches.pop(), newer_series=max(newer, key=_key) if newer else None)


def served_runtime_for(base: Path) -> ServedRuntime:
    """``served_runtime`` for the ``[package]`` block in ``base/vergil.toml``."""
    pkg = config.read_config(base).package
    if pkg is None:
        raise UpdateDepsError(
            phase="update",
            command="served_runtime_for",
            message="[package] is missing, so there is no runtime to resolve.",
        )
    return served_runtime(pkg)
