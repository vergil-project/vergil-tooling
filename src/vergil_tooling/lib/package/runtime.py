"""The one ``vergil-python`` runtime a package builds against (vergil-tooling#3478).

Each ``python``-builder package of a release resolves its runtime **once**, in
``vrg-package matrix``, and records it in every build cell of that package and
in ``packages-manifest.json``.
Each build cell then builds against that recorded value instead of re-reading
the version window, so cells running at slightly different times cannot pick
different runtimes per architecture.

:func:`check_consistent` is the backstop wherever every package of a release
is together (``vrg-package check-runtime`` before publishing, and the index
collecting a release): every package must depend on exactly the recorded
``vergil-python<x.y>`` and on no other runtime series.
:func:`check_consistent_by_name` applies it per package name, for a release
of several ``[[package]]`` entries.

Every reader of a ``[package.python].runtime`` pin goes through
:func:`python_packages` and :func:`pinned_python_package` (epic
vergil-project/.github#472, spec §5.2). A repo may list several
``[[package]]`` entries, and **at most one** ``python``-builder entry may pin a
runtime; more than one is refused, never guessed.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.extensions.python import runtime_series

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from vergil_tooling.lib.config import PackageConfig, VergilConfig

# The refusal when more than one ``[[package]]`` entry pins a runtime.
SEVERAL_PINS = "several [[package]] entries pin a runtime; pin it on one entry"

# A per-series runtime package name: ``vergil-python3.14``.
_RUNTIME_PACKAGE = re.compile(r"vergil-python\d+\.\d+")


def runtime_package(series: str) -> str:
    """The per-series runtime package: ``vergil-python<x.y>``, whose patch floats."""
    return f"vergil-python{series}"


def series_of(runtime: str) -> str:
    """The ``x.y`` of ``runtime`` (``3.14`` or ``3.14.8``); a bad value is a ``PackageError``."""
    try:
        return runtime_series(runtime)
    except VersionsError as exc:
        raise PackageError(str(exc)) from exc


def check_consistent(
    runtime: str, packages: Iterable[tuple[str, Iterable[str]]], where: str
) -> None:
    """Fail unless every package depends on exactly the recorded runtime package.

    ``packages`` pairs each package's label (its file name) with the names of
    the packages it depends on. ``runtime`` is the value ``vrg-package matrix``
    recorded. A package that depends on another ``vergil-python<x.y>``, on more
    than one, or on none is a mismatch; every mismatch is listed, then the
    check fails. A release with no packages to check fails too: an empty set
    proves nothing.
    """
    want = runtime_package(series_of(runtime))
    seen = 0
    bad: list[str] = []
    for label, depends in packages:
        seen += 1
        got = sorted({d for d in depends if _RUNTIME_PACKAGE.fullmatch(d)})
        if got != [want]:
            bad.append(f"{label} depends on {', '.join(got) or 'no vergil-python runtime'}")
    if not seen:
        msg = f"{where}: no packages to check against the recorded runtime {want}"
        raise PackageError(msg)
    if bad:
        msg = (
            f"{where}: packages disagree with the recorded runtime {want} "
            f"(runtime {runtime}, recorded by vrg-package matrix): " + "; ".join(bad) + ". "
            "Every package of one release must depend on the same runtime"
        )
        raise PackageError(msg)


def check_consistent_by_name(
    runtimes: Mapping[str, str | None],
    packages: Iterable[tuple[str, str, Iterable[str]]],
    where: str,
) -> None:
    """:func:`check_consistent` per package name, for a release of several packages.

    ``runtimes`` maps each package name the release builds to the runtime
    ``vrg-package matrix`` recorded for it (``None`` for a builder without one,
    such as ``staged``). ``packages`` gives each file as ``(file name, package
    name, depends)``. Every package with a runtime is checked against its own
    runtime, and must have at least one file; a package whose runtime is
    ``None`` is not checked. A file whose package name the release does not
    build fails: it cannot be checked against anything.
    """
    by_name: dict[str, list[tuple[str, Iterable[str]]]] = {name: [] for name in runtimes}
    stray: list[str] = []
    for label, name, depends in packages:
        if name in by_name:
            by_name[name].append((label, depends))
        else:
            stray.append(f"{label} (package {name})")
    if stray:
        msg = (
            f"{where}: files for packages the release does not build "
            f"(it builds {', '.join(runtimes) or 'none'}): " + "; ".join(stray)
        )
        raise PackageError(msg)
    for name, runtime in runtimes.items():
        if runtime is not None:
            check_consistent(runtime, by_name[name], f"{where} ({name})")


def python_packages(cfg: VergilConfig) -> tuple[PackageConfig, ...]:
    """The ``python``-builder entries of ``cfg.packages``, in file order.

    Empty when the repo has no ``[package]`` or only ``staged`` entries.
    """
    return tuple(p for p in cfg.packages if p.builder == "python")


def pinned_python_package(cfg: VergilConfig) -> PackageConfig | None:
    """The one ``python``-builder entry that pins ``[package.python].runtime``, if any.

    More than one pin is a ``PackageError`` (:data:`SEVERAL_PINS`): which pin
    the runtime follows would be a guess.
    """
    pinned = [p for p in python_packages(cfg) if p.python is not None and p.python.runtime]
    if len(pinned) > 1:
        raise PackageError(SEVERAL_PINS)
    return pinned[0] if pinned else None
