"""Managed git-worktree create/remove for automated workflows.

Kept independent of vrg-update-deps internals so vrg-release can adopt the same
mechanism (#1578).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from pathlib import Path


def create_worktree(repo_root: Path, *, branch: str, base: str) -> Path:
    """Create a worktree under ``.worktrees/`` on a new ``branch`` off ``base``."""
    path = repo_root / ".worktrees" / branch.replace("/", "-")
    if path.exists():
        raise UpdateDepsError(
            phase="preflight",
            command=f"git worktree add {path}",
            message=f"Worktree path already exists: {path}. Remove it and re-run.",
        )
    git.run("worktree", "add", "-b", branch, str(path), base)
    return path


def remove_worktree(path: Path) -> None:
    """Force-remove a managed worktree (the branch ref is left for the caller)."""
    git.run("worktree", "remove", "--force", str(path))
