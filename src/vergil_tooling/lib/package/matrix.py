"""Resolve ``[package]`` into explicit build and test cells (spec §5.3).

- Every non-``native`` target on an architecture shares one build cell, built
  on Ubuntu 24.04, emitting every format its targets need. A ``noarch`` package
  has exactly one shared cell, on amd64.
- Each ``native`` target gets its own build cell, inside a container of its OS.
- Every target gets its own test cell (the ``full`` tier).
- The ``reduced`` tier (issue #3127) keeps every build cell but only one test
  cell per format: the first selected target of that format on amd64, else on
  arm64, drawn from the shared targets unless the format has none.

Workflows consume only the JSON this produces (``vrg-package matrix``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.config import CONFIG_FILE, ConfigError
from vergil_tooling.lib.package import targets as tg

if TYPE_CHECKING:
    from vergil_tooling.lib.config import PackageConfig

RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
SHARED_IMAGE = "ubuntu:24.04"
NOARCH_CELL_ID = "shared-noarch"


class Tier(StrEnum):
    """How much of the matrix to run: everything, or every build plus one test per format."""

    FULL = "full"
    REDUCED = "reduced"


@dataclass(frozen=True)
class BuildCell:
    id: str
    arch: str
    runner: str
    image: str
    fmts: tuple[str, ...]
    targets: tuple[str, ...]
    native: bool
    suite: str | None


@dataclass(frozen=True)
class TestCell:
    # Not a pytest collection target despite the ``Test*`` name.
    __test__ = False
    id: str
    target: str
    arch: str
    runner: str
    image: str
    fmt: str
    suite: str
    native: bool


@dataclass(frozen=True)
class Matrix:
    build: tuple[BuildCell, ...]
    test: tuple[TestCell, ...]


def select_targets(pkg: PackageConfig, *, source: str = CONFIG_FILE) -> list[tg.Target]:
    """Return the selected targets, sorted by key; raise ``ConfigError`` on a bad selection.

    A selection that resolves to zero targets is an error, never an empty build.
    """
    if pkg.targets is not None and pkg.exclude:
        msg = f"{source}: [package]: set at most one of exclude, targets"
        raise ConfigError(msg)
    if pkg.targets is not None:
        chosen: dict[str, tg.Target] = {}
        for pat in pkg.targets:
            hits = tg.match(pat)
            if not hits:
                msg = f"{source}: [package].targets pattern {pat!r} matches no target"
                raise ConfigError(msg)
            chosen.update({t.key: t for t in hits})
        selected = sorted(chosen.values(), key=lambda t: t.key)
    else:
        for pat in pkg.exclude:
            if not tg.match(pat):
                msg = f"{source}: [package].exclude pattern {pat!r} matches no target"
                raise ConfigError(msg)
        selected = [
            t for t in tg.all_targets() if not any(fnmatchcase(t.key, p) for p in pkg.exclude)
        ]
    if not selected:
        msg = f"{source}: [package] selects no targets"
        raise ConfigError(msg)
    for pat in pkg.native:
        if not any(fnmatchcase(t.key, pat) for t in selected):
            msg = f"{source}: [package].native pattern {pat!r} matches no selected target"
            raise ConfigError(msg)
    return selected


def _fmts(ts: list[tg.Target]) -> tuple[str, ...]:
    return tuple(sorted({t.fmt for t in ts}))


def reduce_tests(m: Matrix) -> Matrix:
    """``m`` with every build cell but one test cell per format (the ``reduced`` tier).

    Per format, the candidates are its shared (non-``native``) test cells, or
    its native ones if it has no shared target; the first candidate on amd64
    wins, else the first on arm64. Test-cell order is preserved.
    """
    keep: set[str] = set()
    for fmt in sorted({t.fmt for t in m.test}):
        of_fmt = [t for t in m.test if t.fmt == fmt]
        candidates = [t for t in of_fmt if not t.native] or of_fmt
        amd64 = [t for t in candidates if t.arch == "amd64"]
        keep.add((amd64 or candidates)[0].id)
    return replace(m, test=tuple(t for t in m.test if t.id in keep))


def resolve(pkg: PackageConfig, *, tier: Tier = Tier.FULL) -> Matrix:
    """Resolve ``pkg`` into build and test cells for ``tier``."""
    full = _resolve_full(pkg)
    return reduce_tests(full) if tier is Tier.REDUCED else full


def _resolve_full(pkg: PackageConfig) -> Matrix:
    selected = select_targets(pkg)
    is_native = {t.key: any(fnmatchcase(t.key, p) for p in pkg.native) for t in selected}
    shared = [t for t in selected if not is_native[t.key]]
    build: list[BuildCell] = []
    if pkg.noarch:
        # Config validation forbids native with noarch, so ``shared`` is every target.
        build.append(
            BuildCell(
                NOARCH_CELL_ID,
                "amd64",
                RUNNERS["amd64"],
                SHARED_IMAGE,
                _fmts(shared),
                tuple(t.key for t in shared),
                native=False,
                suite=None,
            )
        )
    else:
        for arch in tg.ARCHES:
            ts = [t for t in shared if t.arch == arch]
            if ts:
                build.append(
                    BuildCell(
                        f"shared-{arch}",
                        arch,
                        RUNNERS[arch],
                        SHARED_IMAGE,
                        _fmts(ts),
                        tuple(t.key for t in ts),
                        native=False,
                        suite=None,
                    )
                )
    build.extend(
        BuildCell(
            f"native-{t.distro}-{t.version}-{t.arch}",
            t.arch,
            RUNNERS[t.arch],
            t.image,
            (t.fmt,),
            (t.key,),
            native=True,
            suite=t.suite,
        )
        for t in selected
        if is_native[t.key]
    )
    test = tuple(
        TestCell(
            f"test-{t.distro}-{t.version}-{t.arch}",
            t.key,
            t.arch,
            RUNNERS[t.arch],
            t.image,
            t.fmt,
            t.suite,
            native=is_native[t.key],
        )
        for t in selected
    )
    return Matrix(tuple(build), test)


def to_json(m: Matrix) -> dict[str, Any]:
    """The JSON-serialisable ``{"build": [...], "test": [...]}`` form of ``m``."""
    return {"build": [asdict(c) for c in m.build], "test": [asdict(c) for c in m.test]}


def manifest(m: Matrix) -> dict[str, Any]:
    """The release artifact manifest: one ``(fmt, arch, suite)`` per artifact built."""
    arts: list[dict[str, Any]] = []
    for c in m.build:
        arch = "all" if c.id == NOARCH_CELL_ID else c.arch
        arts.extend({"fmt": f, "arch": arch, "suite": c.suite} for f in c.fmts)
    return {"artifacts": arts}
