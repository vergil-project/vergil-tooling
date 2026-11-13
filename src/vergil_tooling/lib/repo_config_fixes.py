"""Fixers for the managed files that ``vrg-github-repo-config update`` keeps in sync.

Each fixer, ``fix_<name>(root) -> bool``, writes one managed file in place
under ``root`` (a fresh clone of the repo's ``develop``) so that it matches
the released master copy, and returns whether it changed anything. It asks
the same ``<name>_drift`` function in :mod:`vergil_tooling.lib.repo_config`
that the file's audit check asks, so the check reports drift exactly when
the fixer would change something or raise :class:`CannotFix` (epic
vergil-project/.github#1064, Task 3a).

The fixers own only the file change: no git, no GitHub API and no PR. They
replace the write logic of the retired per-target applicators
(``vrg-hook-guard-shim-sync``, ``vrg-claude-md-sync`` and
``vrg-claude-settings-sync``).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib.repo_config import (
    ManagedFileDrift,
    claude_md_drift,
    claude_settings_drift,
    hook_guard_shim_drift,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class CannotFix(Exception):  # noqa: N818 - the plan's name for the interface
    """A managed file whose check fails but for which no fix can be computed.

    ``check`` is the audit item's field (for example ``local.claude_md``) and
    ``reason`` is what the check reported (for example ``no template
    markers``).
    """

    def __init__(self, check: str, reason: str) -> None:
        super().__init__(f"{check}: cannot fix: {reason}")
        self.check = check
        self.reason = reason


def fix_hook_guard_shim(root: Path) -> bool:
    """Write the packaged hook guard shim to ``.claude/hooks/guard.sh``, mode 0755."""
    return _apply(root, hook_guard_shim_drift(root))


def fix_claude_md(root: Path) -> bool:
    """Replace CLAUDE.md's marked template region with the packaged template."""
    return _apply(root, claude_md_drift(root))


def fix_claude_settings(root: Path) -> bool:
    """Replace ``.claude/settings.json`` with the packaged template."""
    return _apply(root, claude_settings_drift(root))


#: Every managed-file fixer, in the order ``update`` runs them.
FIXERS: list[Callable[[Path], bool]] = [
    fix_hook_guard_shim,
    fix_claude_md,
    fix_claude_settings,
]


def _apply(root: Path, drift: ManagedFileDrift | None) -> bool:
    """Carry out ``drift``'s fix under ``root``; ``False`` when there is no drift.

    Raises :class:`CannotFix` when the file drifts but has no fix.
    """
    if drift is None:
        return False
    if drift.fix is None:
        raise CannotFix(drift.item.field, str(drift.item.actual))
    path = root / drift.fix.path
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, drift.fix.data, drift.fix.mode)
    return True


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` via a temp file in the same directory and a rename.

    The bytes are written as they are, with no newline translation, and the
    temp file is given ``mode`` before the rename, so the file is never in
    place with the wrong mode.
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        tmp.chmod(mode)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
