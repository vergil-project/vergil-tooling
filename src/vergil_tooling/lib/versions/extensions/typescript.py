"""The TypeScript resolver extension (epic vergil-project/.github#1281, spec §6.2).

Every TypeScript-specific version rule lives here, and nowhere in the shared code:

* **Toolchain selection.** The Node runtime belongs to Vergil, as Python's
  interpreter does (epic vergil-project/.github#407, spec §6.5): Vergil selects
  it through the image, and runs the npm bundled with each window Node major.
  So every setting that names a Node or package-manager version is an error
  (:func:`check_toolchain`): the ``.nvmrc`` and ``.node-version`` files, and,
  in the root ``package.json``, ``packageManager`` (whatever it names, npm
  included), ``volta``, an ``npm``, ``pnpm`` or ``yarn`` key under ``engines``,
  and a ``devEngines`` ``runtime`` or ``packageManager`` entry. npm checks
  ``devEngines`` before ``install``, ``ci`` and ``run``, and by default fails
  when the running Node or npm does not match. ``devEngines``' ``os``, ``cpu``
  and ``libc`` entries are allowed, and ``engines.node`` is a range, not a
  selection.

The root ``package.json`` is read with :func:`~vergil_tooling.lib.container.read_package_json`,
the one parser of the file; a file it cannot read is a ``VersionsError``, so
``vrg-validate`` reports it as an ``ERROR:`` line, never a traceback.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.container import PackageJsonError, read_package_json
from vergil_tooling.lib.versions import VersionsError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["check_toolchain"]

_PACKAGE_JSON = "package.json"
_RUNTIME_OWNED = "the Node runtime, which belongs to Vergil (Vergil selects it through the image)"
_PM_OWNED = (
    "the package manager, which belongs to Vergil (Vergil runs the npm bundled with "
    "each window Node major)"
)
# Version files that select the Node runtime, in the order they are reported.
_VERSION_FILES = (".nvmrc", ".node-version")
# Package managers whose version an ``engines`` key would select.
_ENGINES_PMS = ("npm", "pnpm", "yarn")
# ``devEngines`` entries that select a version, with what each selects.
_DEV_ENGINES_OWNED = (("runtime", _RUNTIME_OWNED), ("packageManager", _PM_OWNED))


def _package_json(repo_root: Path) -> dict[str, object] | None:
    """The root ``package.json``, or ``None`` when there is none.

    A file that cannot be read, is not JSON or is not a JSON object is a
    :class:`VersionsError` with :class:`PackageJsonError`'s message.
    """
    path = repo_root / _PACKAGE_JSON
    if not path.exists():
        return None
    try:
        return read_package_json(path)
    except PackageJsonError as exc:
        raise VersionsError(str(exc)) from exc


def _json_type(value: object) -> str:
    """The JSON name of a parsed value's type (``null``, ``string``, ``array`` ...)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def _object_field(path: Path, data: dict[str, object], field: str) -> dict[str, object]:
    """*data*'s *field* as an object (empty when absent); anything else, null included, is fatal."""
    if field not in data:
        return {}
    value = data[field]
    if not isinstance(value, dict):
        msg = (
            f"{path}: {field} is a JSON {_json_type(value)}, not an object. "
            f"Fix: make {field} an object"
        )
        raise VersionsError(msg)
    return value


def _check_dev_engines(path: Path, dev_engines: dict[str, object]) -> None:
    """Each ``devEngines`` entry must be an object or an array of objects (npm's forms)."""
    for key, value in dev_engines.items():
        entries = value if isinstance(value, list) else [value]
        for entry in entries:
            if not isinstance(entry, dict):
                msg = (
                    f"{path}: devEngines.{key} holds a JSON {_json_type(entry)}; npm "
                    f"accepts an object or an array of objects. Fix: correct devEngines.{key}"
                )
                raise VersionsError(msg)


def check_toolchain(repo_root: Path) -> list[str]:
    """One error for each setting that names a Node or package-manager version.

    In this order: ``.nvmrc``, ``.node-version``, ``packageManager``,
    ``volta``, ``engines.npm``, ``engines.pnpm``, ``engines.yarn``,
    ``devEngines.runtime``, ``devEngines.packageManager``. A malformed
    ``package.json``, ``engines`` or ``devEngines`` is a :class:`VersionsError`
    naming the field.
    """
    errors = [
        f"{name} selects {_RUNTIME_OWNED}. Fix: delete {name}"
        for name in _VERSION_FILES
        if (repo_root / name).exists()
    ]
    data = _package_json(repo_root)
    if data is None:
        return errors
    path = repo_root / _PACKAGE_JSON
    engines = _object_field(path, data, "engines")
    dev_engines = _object_field(path, data, "devEngines")
    _check_dev_engines(path, dev_engines)
    if "packageManager" in data:
        errors.append(
            f'{_PACKAGE_JSON} "packageManager" selects {_PM_OWNED}. '
            f'Fix: remove the "packageManager" field'
        )
    if "volta" in data:
        errors.append(
            f'{_PACKAGE_JSON} "volta" pins the Node runtime and package managers, which '
            f"belong to Vergil (Vergil selects Node through the image, and runs the npm "
            f'bundled with each window Node major). Fix: remove the "volta" field'
        )
    errors.extend(
        f"{_PACKAGE_JSON} engines.{pm} selects {_PM_OWNED}. Fix: remove engines.{pm}"
        for pm in _ENGINES_PMS
        if pm in engines
    )
    errors.extend(
        f"{_PACKAGE_JSON} devEngines.{entry} selects {owned}; by default npm fails "
        f"install, ci and run when it does not match. Fix: remove devEngines.{entry}"
        for entry, owned in _DEV_ENGINES_OWNED
        if entry in dev_engines
    )
    return errors
