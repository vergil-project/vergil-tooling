"""The Python resolver extension (epic vergil-project/.github#407, spec §6.4-§7).

Every Python-specific version rule lives here, and nowhere in the shared code:

* **Packaged runtime.** A repo with ``[package].builder = "python"`` deploys on
  a ``vergil-python`` runtime, which ships separately from (and can lag) the
  container that builds the version. An app resolves to the newest candidate
  whose window entry has ``runtime_published``; with none, resolution fails
  loud. :func:`derived_runtime` names the ``x.y`` the package builder installs
  (``vergil-python<x.y>`` at ``/opt/vergil/python/<x.y>``) when
  ``[package.python].runtime`` is not pinned, and an explicit pin must name a
  window version.
* **Floor.** ``requires-python`` below the window's oldest version, or an
  upper bound that excludes its newest, is a warning (spec §6.4).
* **Toolchain selection.** ``.python-version`` and uv's ``python-preference``
  choose the interpreter behind Vergil's back, so they are errors (spec §6.5).
  Vergil hands uv the resolved version through ``UV_PYTHON`` instead.
"""

from __future__ import annotations

import re
import tomllib
from typing import TYPE_CHECKING, Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from vergil_tooling.lib.config import _version_sort_key
from vergil_tooling.lib.versions import ResolutionError, VersionsError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vergil_tooling.lib.versions.extensions.base import ExtensionContext
    from vergil_tooling.lib.versions.resolve import Resolution
    from vergil_tooling.lib.versions.window import Window

__all__ = [
    "check_floor",
    "check_toolchain",
    "container_env",
    "derived_runtime",
    "python_extension",
    "runtime_series",
]

_LANGUAGE = "python"
_PYTHON_VERSION_FILE = ".python-version"
_PYPROJECT = "pyproject.toml"
_UV_TOML = "uv.toml"
_PREFERENCE_KEY = "python-preference"
# Specifier operators that bound the version from below or above.
_LOWER_OPS = frozenset({">=", ">", "~=", "=="})
_UPPER_OPS = frozenset({"<", "<=", "~=", "=="})
_SERIES_RE = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?")


def runtime_series(runtime: str) -> str:
    """The ``x.y`` of a runtime: ``3.14.8`` and ``3.14`` are both ``3.14``."""
    match = _SERIES_RE.fullmatch(runtime)
    if match is None:
        msg = f"python runtime {runtime!r} is not an x.y or x.y.z version"
        raise ResolutionError(msg)
    return f"{match.group(1)}.{match.group(2)}"


def _runtime_published(window: Window, language: str) -> dict[str, bool | None]:
    return {
        family.token(v): v.runtime_published
        for family in window.languages[language]
        for v in family.versions
    }


def _check_explicit_runtime(ctx: ExtensionContext, window_tokens: list[str]) -> None:
    """An explicit ``[package.python].runtime`` must be an ``x.y`` in the window."""
    pkg = ctx.cfg.package
    runtime = pkg.python.runtime if pkg is not None and pkg.python is not None else None
    if runtime is None:
        return
    if runtime_series(runtime) not in window_tokens:
        msg = (
            f"[package.python].runtime {runtime} is not in the {ctx.language} window "
            f"({', '.join(window_tokens)}) published at {ctx.window.source}. Fix: remove "
            f"[package.python].runtime so the runtime follows the resolved version, or "
            f"update the pin to a window version"
        )
        raise ResolutionError(msg)


def python_extension(ctx: ExtensionContext, candidates: tuple[str, ...]) -> tuple[str, ...]:
    """Narrow a packaged repo's candidates to versions with a published runtime.

    An unpackaged repo is untouched. A packaged app keeps the newest candidate
    whose runtime is published. A packaged library keeps every candidate: its
    tests must still cover each version its consumers build on, and only the
    deployed runtime (:func:`derived_runtime`) needs a published package. Either
    way, a packaged repo with no published runtime among its candidates fails.
    """
    published = _runtime_published(ctx.window, ctx.language)
    _check_explicit_runtime(ctx, list(published))
    pkg = ctx.cfg.package
    if pkg is None or pkg.builder != "python":
        return candidates
    with_runtime = tuple(token for token in candidates if published[token] is True)
    if not with_runtime:
        msg = (
            f"no published vergil-python runtime for any of {', '.join(candidates)} "
            f"(window {ctx.window.source}). A packaged Python repo deploys on a "
            f"vergil-python runtime, so it needs one. Fix: wait for vergil-python to "
            f"publish one, or pin [ci].versions to a version that has one"
        )
        raise ResolutionError(msg)
    if ctx.kind == "library":
        return candidates
    return with_runtime[:1]


def derived_runtime(resolution: Resolution, window: Window) -> str:
    """The ``x.y`` runtime a packaged repo builds against when none is pinned.

    The newest resolved version whose ``vergil-python`` runtime is published.
    """
    published = _runtime_published(window, resolution.language)
    for token in resolution.versions:
        if published.get(token) is True:
            return token
    msg = (
        f"no published vergil-python runtime for any of {', '.join(resolution.versions)} "
        f"(window {window.source}). Fix: pin [package.python].runtime, or wait for "
        f"vergil-python to publish a runtime for a resolved version"
    )
    raise ResolutionError(msg)


def _read_toml(path: Path) -> dict[str, Any] | None:
    """*path* parsed, or ``None`` when it does not exist. Invalid TOML is fatal."""
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path} is not valid TOML: {exc}"
        raise VersionsError(msg) from exc


def _requires_python(repo_root: Path) -> SpecifierSet | None:
    data = _read_toml(repo_root / _PYPROJECT) or {}
    raw = data.get("project", {}).get("requires-python")
    if raw is None:
        return None
    invalid = (
        f"{repo_root / _PYPROJECT}: [project].requires-python {raw!r} is not a valid "
        f"version specifier"
    )
    if not isinstance(raw, str):
        raise VersionsError(invalid)
    try:
        return SpecifierSet(raw)
    except InvalidSpecifier as exc:
        msg = f"{invalid}: {exc}"
        raise VersionsError(msg) from exc


def _bound(spec_version: str) -> tuple[int, ...]:
    """The ``x.y`` key of a specifier's version (``3.14.*`` and ``3.14.2`` are 3.14)."""
    return _version_sort_key(spec_version)[:2]


def check_floor(repo_root: Path, oldest: str, newest: str) -> list[str]:
    """Warn when ``requires-python`` falls below *oldest* or excludes *newest*.

    A floor above the window's oldest version is a deliberate restriction and
    produces nothing (spec §6.4).
    """
    specs = _requires_python(repo_root)
    if specs is None:
        return []
    warnings: list[str] = []
    lower = [_bound(s.version) for s in specs if s.operator in _LOWER_OPS]
    if lower and max(lower) < _version_sort_key(oldest)[:2]:
        floor = ".".join(str(n) for n in max(lower))
        warnings.append(
            f"requires-python floor {floor} is below the oldest supported version "
            f"{oldest}. Fix: raise it to >={oldest}"
        )
    try:
        probe = Version(f"{newest}.0")
    except InvalidVersion as exc:
        msg = f"the python window's newest version {newest!r} is not a version"
        raise VersionsError(msg) from exc
    excluding = sorted(str(s) for s in specs if s.operator in _UPPER_OPS and not s.contains(probe))
    if excluding:
        warnings.append(
            f"requires-python upper bound {','.join(excluding)} excludes {newest}. "
            f"Fix: drop the upper bound, or widen it to admit {newest}"
        )
    return warnings


def check_toolchain(repo_root: Path) -> list[str]:
    """Errors for every setting that picks the interpreter behind Vergil's back."""
    why = "Vergil hands uv the resolved version through UV_PYTHON"
    errors: list[str] = []
    if (repo_root / _PYTHON_VERSION_FILE).exists():
        errors.append(
            f"{_PYTHON_VERSION_FILE} selects the Python interpreter, which belongs to "
            f"Vergil ({why}). Fix: delete {_PYTHON_VERSION_FILE}"
        )
    pyproject = _read_toml(repo_root / _PYPROJECT) or {}
    if _PREFERENCE_KEY in pyproject.get("tool", {}).get("uv", {}):
        errors.append(
            f"{_PYPROJECT} [tool.uv].{_PREFERENCE_KEY} selects the Python interpreter, "
            f"which belongs to Vergil ({why}). Fix: remove it"
        )
    uv_toml = _read_toml(repo_root / _UV_TOML) or {}
    if _PREFERENCE_KEY in uv_toml:
        errors.append(
            f"{_UV_TOML} {_PREFERENCE_KEY} selects the Python interpreter, which "
            f"belongs to Vergil ({why}). Fix: remove it"
        )
    return errors


def container_env(primary: str) -> Mapping[str, str]:
    """``UV_PYTHON`` names the resolved version, so uv uses the image's interpreter."""
    return {"UV_PYTHON": primary}
