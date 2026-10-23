"""The one ``vergil-python`` runtime a release builds against (vergil-tooling#3478).

A ``python``-builder release resolves its runtime **once**, in ``vrg-package
matrix``, and records it in every build cell and in ``packages-manifest.json``.
Each build cell then builds against that recorded value instead of re-reading
the version window, so cells running at slightly different times cannot pick
different runtimes per architecture.

:func:`check_consistent` is the backstop wherever every package of a release
is together (``vrg-package check-runtime`` before publishing, and the index
collecting a release): every package must depend on exactly the recorded
``vergil-python<x.y>`` and on no other runtime series.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError
from vergil_tooling.lib.versions import VersionsError
from vergil_tooling.lib.versions.extensions.python import runtime_series

if TYPE_CHECKING:
    from collections.abc import Iterable

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
