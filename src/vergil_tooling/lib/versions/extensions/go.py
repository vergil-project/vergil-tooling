"""The Go resolver extension (epic vergil-project/.github#407, spec §6.4).

Go's only rule is its floor: the ``go`` directive in ``go.mod`` below the
window's oldest version is a warning. The directive is a minimum with no upper
bound, and Go has no toolchain-selection setting for Vergil to own (spec §6.5).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from vergil_tooling.lib.config import _version_sort_key
from vergil_tooling.lib.versions import VersionsError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["check_floor"]

_GO_MOD = "go.mod"
_DIRECTIVE_RE = re.compile(r"^go[ \t]+(\S+)[ \t]*(?://.*)?$", re.MULTILINE)
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?")


def check_floor(repo_root: Path, oldest: str, _newest: str) -> list[str]:
    """Warn when ``go.mod``'s ``go`` directive is below the window's *oldest*."""
    path = repo_root / _GO_MOD
    if not path.is_file():
        return []
    directive = _DIRECTIVE_RE.search(path.read_text(encoding="utf-8"))
    if directive is None:
        return []
    version = _VERSION_RE.fullmatch(directive.group(1))
    if version is None:
        msg = f"{path}: the go directive {directive.group(1)!r} is not a Go version"
        raise VersionsError(msg)
    floor = f"{version.group(1)}.{version.group(2)}"
    if _version_sort_key(floor) < _version_sort_key(oldest):
        return [
            f"go.mod go directive floor {floor} is below the oldest supported version "
            f"{oldest}. Fix: raise it to go {oldest}"
        ]
    return []
