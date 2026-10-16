"""vergil-python runtimes the live vergil package repository serves (spec §8.2).

The package build installs ``vergil-python<runtime>`` from the shared deb suite
(non-native cells) or the target's own suite (native cells), and install-test
resolves it from every selected target's index. A runtime is therefore a bump
candidate only when **every** one of those indexes serves it. A version missing
from any of them would merge green and then fail the next release.

The rpm package carries the same name as the deb (``vergil-python<runtime>``,
from vergil-python's ``[package].name``); both are matched by one pattern.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET  # noqa: N817
from typing import TYPE_CHECKING

from vergil_tooling.lib import config
from vergil_tooling.lib.package import matrix, orgs, python_builder
from vergil_tooling.lib.package.targets import ARCHES
from vergil_tooling.lib.release import package_index
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.config import PackageConfig

# The runtime package always comes from the vergil org (spec D7).
_RUNTIME_VENDOR = "vergil"
_RUNTIME_RE = re.compile(r"^vergil-python(\d+\.\d+\.\d+)$")


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


def _names(base: str, fmt: str, suite: str, arch: str, fetch: package_index.Fetch) -> set[str]:
    """Package names in one index; a failed lookup raises ``UpdateDepsError``."""
    try:
        if fmt == "deb":
            return package_index.deb_package_names(base, suite, arch, fetch)
        return package_index.rpm_package_names(base, suite, arch, fetch)
    except (ReleaseError, OSError, ET.ParseError) as exc:
        raise UpdateDepsError(
            phase="update",
            command=f"list {fmt} packages in {base} ({suite}/{arch})",
            message=f"Could not read the vergil package index: {exc}",
        ) from exc


def served_runtimes(
    pkg: PackageConfig,
    *,
    fetch: package_index.Fetch = package_index._fetch,  # noqa: SLF001
) -> set[str]:
    """Runtimes served by **every** index in ``runtime_indexes``; fail loud when none are."""
    base = orgs.for_vendor(_RUNTIME_VENDOR).base_url
    per_index: list[set[str]] = []
    for fmt, suite, arch in runtime_indexes(pkg):
        names = _names(base, fmt, suite, arch, fetch)
        per_index.append({m.group(1) for n in names if (m := _RUNTIME_RE.match(n))})
    served = set.intersection(*per_index)
    if not served:
        raise UpdateDepsError(
            phase="update",
            command=f"list vergil-python runtimes in {base}",
            message=(
                "The vergil package index lists no vergil-python runtime served by every "
                "build and install-test index; refusing to treat a failed lookup as "
                "'no change'."
            ),
        )
    return served


def served_runtimes_for(base: Path) -> set[str]:
    """``served_runtimes`` for the ``[package]`` block in ``base/vergil.toml``."""
    pkg = config.read_config(base).package
    if pkg is None:
        raise UpdateDepsError(
            phase="update",
            command="served_runtimes_for",
            message="[package] is missing, so there is no runtime to resolve.",
        )
    return served_runtimes(pkg)


def newest_patch(current: str, served: set[str]) -> str | None:
    """The newest served runtime on ``current``'s minor line, if newer than ``current``."""
    major_minor = _key(current)[:2]
    same = [v for v in served if _key(v)[:2] == major_minor and _key(v) > _key(current)]
    return max(same, key=_key) if same else None


def newer_minor(current: str, served: set[str]) -> str | None:
    """The newest served runtime on a later minor line than ``current``, if any."""
    newer = [v for v in served if _key(v)[:2] > _key(current)[:2]]
    return max(newer, key=_key) if newer else None
