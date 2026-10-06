"""Resolve a package's name (spec §5.2).

``[package].name`` wins when set (it is required for the ``staged`` builder).
Otherwise the Python builder defaults to ``pyproject.toml`` ``[project].name``.
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

from vergil_tooling.lib.package import PackageError

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.config import PackageConfig


def package_name(repo_root: Path, pkg: PackageConfig) -> str:
    """Return the OS package name for ``pkg`` in ``repo_root``."""
    if pkg.name:
        return pkg.name
    pyproject = repo_root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        msg = f"pyproject.toml not found at {repo_root}; set [package].name"
        raise PackageError(msg) from exc
    except tomllib.TOMLDecodeError as exc:
        msg = f"{pyproject} is not valid TOML: {exc}"
        raise PackageError(msg) from exc
    project = data.get("project")
    name = project.get("name") if isinstance(project, dict) else None
    if not isinstance(name, str) or not name:
        msg = "pyproject.toml has no [project].name; set [package].name"
        raise PackageError(msg)
    return name
