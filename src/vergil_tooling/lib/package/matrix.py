"""Resolve ``[package]`` into explicit build and test cells (spec §5.3).

- Every non-``native`` target on an architecture shares one build cell, built
  on Ubuntu 24.04, emitting every format its targets need. A ``noarch`` package
  has exactly one shared cell, on amd64.
- Each ``native`` target gets its own build cell, inside a container of its OS.
- Every target gets its own test cell (the ``full`` tier).
- The ``reduced`` tier (issue #3127) keeps every build cell but only one test
  cell per package and format: the oldest release of that format (compared
  numerically) on amd64, else on arm64, drawn from the shared targets unless
  the format has none.
- Every cell carries its package name (epic vergil-project/.github#472, spec
  §5.2). :func:`resolve_all` resolves every ``[[package]]`` entry: a single
  package keeps today's ids, and with several every id is qualified as
  ``<name>--<id>`` (a double dash, which no unqualified id contains), so the
  per-cell artifact names never collide.

Workflows consume only the JSON this produces (``vrg-package matrix``).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.config import CONFIG_FILE, ConfigError
from vergil_tooling.lib.package import naming
from vergil_tooling.lib.package import targets as tg

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from vergil_tooling.lib.config import PackageConfig

RUNNERS = {"amd64": "ubuntu-24.04", "arm64": "ubuntu-24.04-arm"}
SHARED_IMAGE = "ubuntu:24.04"
NOARCH_CELL_ID = "shared-noarch"
# Joins a package name to a cell id when a repo has several packages.
QUALIFIER = "--"


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
    package: str


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
    package: str


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
    """``m`` with every build cell but one test cell per package and format (``reduced`` tier).

    Per package and format, the candidates are its shared (non-``native``) test cells, or
    its native ones if it has no shared target; among the amd64 candidates
    (else the arm64 ones) the **oldest** release wins, compared numerically —
    the strictest compatibility case (lowest glibc, oldest rpm/dnf/systemd).
    Test-cell order is preserved.
    """
    keep: set[str] = set()
    for package, fmt in sorted({(t.package, t.fmt) for t in m.test}):
        of_fmt = [t for t in m.test if t.package == package and t.fmt == fmt]
        candidates = [t for t in of_fmt if not t.native] or of_fmt
        amd64 = [t for t in candidates if t.arch == "amd64"]
        keep.add(min(amd64 or candidates, key=_release_key).id)
    return replace(m, test=tuple(t for t in m.test if t.id in keep))


def _release_key(cell: TestCell) -> tuple[int, ...]:
    """Numeric release of ``cell``'s target (``rhel/9/amd64`` → ``(9,)``)."""
    return tuple(int(part) for part in tg.REGISTRY[cell.target].version.split("."))


def named(repo_root: Path, packages: Sequence[PackageConfig]) -> dict[str, PackageConfig]:
    """Every entry of ``packages`` keyed by its package name, in entry order.

    Two entries resolving to one name are refused: their cells would collide.
    """
    out: dict[str, PackageConfig] = {}
    for pkg in packages:
        name = naming.package_name(repo_root, pkg)
        if name in out:
            msg = f"two [[package]] entries resolve to the package name {name!r}"
            raise ConfigError(msg)
        out[name] = pkg
    return out


def resolve_all(
    repo_root: Path, packages: Sequence[PackageConfig], *, tier: Tier = Tier.FULL
) -> Matrix:
    """Resolve every entry of ``packages`` into one matrix for ``tier``.

    One package: its cells exactly as :func:`resolve` gives them. Several: each
    entry's cells in entry order, every id qualified as ``<name>--<id>``.
    """
    parts = [resolve(pkg, name, tier=tier) for name, pkg in named(repo_root, packages).items()]
    if len(parts) == 1:
        return parts[0]
    return Matrix(
        tuple(replace(c, id=f"{c.package}{QUALIFIER}{c.id}") for p in parts for c in p.build),
        tuple(replace(c, id=f"{c.package}{QUALIFIER}{c.id}") for p in parts for c in p.test),
    )


def resolve(pkg: PackageConfig, name: str, *, tier: Tier = Tier.FULL) -> Matrix:
    """Resolve ``pkg`` (package ``name``) into build and test cells for ``tier``."""
    full = _resolve_full(pkg, name)
    return reduce_tests(full) if tier is Tier.REDUCED else full


def _resolve_full(pkg: PackageConfig, name: str) -> Matrix:
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
                package=name,
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
                        package=name,
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
            package=name,
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
            package=name,
        )
        for t in selected
    )
    return Matrix(tuple(build), test)


def to_json(m: Matrix, *, runtimes: Mapping[str, str | None] | None = None) -> dict[str, Any]:
    """The JSON-serialisable ``{"build": [...], "test": [...]}`` form of ``m``.

    ``runtimes`` maps each package name to its release's resolved
    ``vergil-python`` runtime (``None`` for a builder without one). Every build
    cell of a package with a runtime carries it as ``"runtime"``, so each cell
    builds against the one value resolved here rather than re-reading the
    window (vergil-tooling#3478).
    """
    rts = runtimes or {}

    def extra(c: BuildCell) -> dict[str, str]:
        rt = rts.get(c.package)
        return {} if rt is None else {"runtime": rt}

    return {
        "build": [{**asdict(c), **extra(c)} for c in m.build],
        "test": [asdict(c) for c in m.test],
    }


def _is_noarch(c: BuildCell) -> bool:
    return c.id in (NOARCH_CELL_ID, f"{c.package}{QUALIFIER}{NOARCH_CELL_ID}")


def manifest(m: Matrix, *, runtimes: Mapping[str, str | None] | None = None) -> dict[str, Any]:
    """The release artifact manifest: one ``(fmt, arch, suite)`` per artifact built.

    With several packages, every artifact also carries its package ``"name"``;
    a single package's artifact list stays exactly as it always was. With
    ``runtimes`` (package name to runtime, ``None`` for a builder without one),
    the manifest records them as ``"runtimes"``: every package with a runtime
    must depend on it (``runtime.check_consistent_by_name``). A single package
    with a runtime also keeps the top-level ``"runtime"`` older readers check.
    """
    packages = list(dict.fromkeys(c.package for c in m.build))
    multi = len(packages) > 1
    arts: list[dict[str, Any]] = []
    for c in m.build:
        arch = "all" if _is_noarch(c) else c.arch
        name = {"name": c.package} if multi else {}
        arts.extend({"fmt": f, "arch": arch, "suite": c.suite, **name} for f in c.fmts)
    out: dict[str, Any] = {"artifacts": arts}
    if runtimes is not None:
        out["runtimes"] = {p: runtimes[p] for p in packages}
        if not multi and runtimes[packages[0]] is not None:
            out["runtime"] = runtimes[packages[0]]
    return out
