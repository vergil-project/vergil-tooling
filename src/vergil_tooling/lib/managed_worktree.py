"""Managed git-worktree create/remove for automated workflows.

The deliberate replacement for switching branches in the root checkout,
which leaves the base branch in-flight and can collide with a parallel
agent. Shared by ``vrg-update-deps`` (#1379) and ``vrg-release`` (#1578)
so neither tool moves ``HEAD`` in the root checkout. Each caller
translates ``ManagedWorktreeError`` into its own pipeline error type at
the boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import git

if TYPE_CHECKING:
    from pathlib import Path


class ManagedWorktreeError(Exception):
    """Raised when a managed worktree cannot be created or removed."""


def worktree_path(repo_root: Path, branch: str) -> Path:
    """Return the canonical ``.worktrees/`` path for *branch*."""
    return repo_root / ".worktrees" / branch.replace("/", "-")


def create_worktree(repo_root: Path, *, branch: str, base: str) -> Path:
    """Create a worktree under ``.worktrees/`` on a new ``branch`` off ``base``."""
    path = worktree_path(repo_root, branch)
    if path.exists():
        msg = f"Worktree path already exists: {path}. Remove it and re-run."
        raise ManagedWorktreeError(msg)
    git.run("worktree", "add", "-b", branch, str(path), base)
    return path


def remove_worktree(path: Path) -> None:
    """Force-remove a managed worktree (the branch ref is left for the caller)."""
    git.run("worktree", "remove", "--force", str(path))
