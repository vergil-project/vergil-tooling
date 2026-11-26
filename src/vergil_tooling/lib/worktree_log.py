"""The one log line every worktree removal writes (spec §5.8 of epic vergil-project/.github#1664).

A leaf module, so ``managed_worktree`` (which the release's last stages load)
can log a removal without loading ``worktrees`` and everything it imports.
``worktrees.log_removal`` is the same function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import progress

if TYPE_CHECKING:
    from pathlib import Path


def log_removal(path: Path, branch: str, reason: str) -> None:
    """Record one worktree removal: on stderr and in the running command's run log.

    The line names the worktree, its branch and why it was judged removable:
    ``removed worktree <path> (branch <branch>): <reason>``.
    """
    progress.stderr_line(f"removed worktree {path} (branch {branch}): {reason}")
