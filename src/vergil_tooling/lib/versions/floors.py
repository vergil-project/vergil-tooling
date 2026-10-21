"""A repo's own language floor and toolchain selection, against the window.

Spec §6.4 and §6.5 (epic vergil-project/.github#407). ``vrg-validate`` runs
both checks:

* :func:`check_floors` warns when a repo's declared floor (``requires-python``,
  the ``go`` directive) has fallen below the window's oldest version, or an
  upper bound excludes its newest. Each repo finds its own staleness.
* :func:`check_toolchain_selection` reports, as errors, the settings that pick
  the interpreter or toolchain behind Vergil's back.

The checks themselves are per language, in :mod:`.extensions`; this module only
dispatches, so it has no language branches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.config import _version_sort_key
from vergil_tooling.lib.versions.extensions import FLOOR_CHECKS, TOOLCHAIN_CHECKS

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.versions.resolve import Resolution
    from vergil_tooling.lib.versions.window import Window

__all__ = ["check_floors", "check_toolchain_selection"]


def check_floors(repo_root: Path, resolution: Resolution | None, window: Window) -> list[str]:
    """Warnings about *repo_root*'s declared floor for *resolution*'s language."""
    if resolution is None:
        return []
    check = FLOOR_CHECKS.get(resolution.language)
    if check is None:
        return []
    versions = sorted(
        (v.version for family in window.languages[resolution.language] for v in family.versions),
        key=_version_sort_key,
    )
    return check(repo_root, versions[0], versions[-1])


def check_toolchain_selection(repo_root: Path, language: str) -> list[str]:
    """Errors for the settings in *repo_root* that select *language*'s toolchain."""
    check = TOOLCHAIN_CHECKS.get(language)
    return check(repo_root) if check is not None else []
