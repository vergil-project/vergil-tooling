"""Classify where a repository declares its development dependencies.

The fleet declares dev dependencies in several shapes (uv dependency groups,
PEP 621 optional-dependencies, a ``requirements-*.txt`` file, or a poetry
section). ``classify_dev_deps`` reports the shape for the fleet survey
(``scripts/perf/fleet_survey.py``) — and returns ``UNKNOWN`` rather than
guessing when it recognizes none, so the survey can report loudly instead of
silently no-op'ing (epic vergil-project/.github#333, Task 3; "no silent
failures" repo policy).

This module is an **import-graph leaf**: standard library (``tomllib`` +
filesystem) only, importing nothing else from ``vergil_tooling``, matching the
``languages.py`` leaf convention.
"""

from __future__ import annotations

import tomllib
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class DevDepShape(Enum):
    """Where a repository declares its development dependencies."""

    UV_GROUPS = "uv-groups"
    PEP621_OPTIONAL = "pep621-optional"
    REQUIREMENTS_TXT = "requirements-txt"
    POETRY = "poetry"
    UNKNOWN = "unknown"


#: Requirements filenames, checked in order, that signal a requirements-file
#: dev-dependency shape when ``pyproject.toml`` declares no dev shape of its own.
_REQUIREMENTS_FILES = ("requirements-dev.txt", "requirements-test.txt")


def _classify_pyproject(pyproject: Path) -> DevDepShape | None:
    """Return the dev-dep shape declared in ``pyproject.toml``, or ``None``."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    if "dependency-groups" in data:
        return DevDepShape.UV_GROUPS
    project = data.get("project", {})
    if "optional-dependencies" in project:
        return DevDepShape.PEP621_OPTIONAL
    poetry = data.get("tool", {}).get("poetry", {})
    if "dev-dependencies" in poetry or "group" in poetry:
        return DevDepShape.POETRY
    return None


def classify_dev_deps(repo: Path) -> DevDepShape:
    """Classify where ``repo`` declares its development dependencies.

    ``pyproject.toml`` is authoritative when it declares a dev shape; otherwise
    a recognized ``requirements-*.txt`` file wins; otherwise ``UNKNOWN``.
    """
    pyproject = repo / "pyproject.toml"
    if pyproject.is_file():
        shape = _classify_pyproject(pyproject)
        if shape is not None:
            return shape
    for name in _REQUIREMENTS_FILES:
        if (repo / name).is_file():
            return DevDepShape.REQUIREMENTS_TXT
    return DevDepShape.UNKNOWN
