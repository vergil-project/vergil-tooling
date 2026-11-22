"""Managed git-worktree create/remove for automated workflows.

The deliberate replacement for switching branches in the root checkout,
which leaves the base branch in-flight and can collide with a parallel
agent. Shared by ``vrg-update-deps`` (#1379) and ``vrg-release`` (#1578)
so neither tool moves ``HEAD`` in the root checkout. Each caller
translates ``ManagedWorktreeError`` into its own pipeline error type at
the boundary.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from pathlib import Path


class ManagedWorktreeError(Exception):
    """Raised when a managed worktree cannot be created or removed.

    *detail* carries the failed command's own output, when there is one, for
    the caller to show beneath the message.
    """

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        self.detail = detail
        super().__init__(message)


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


def create_detached_worktree(repo_root: Path, *, branch: str, base: str) -> Path:
    """Create *branch*'s ``.worktrees/`` worktree detached at *base*, on no branch.

    For work that needs the managed worktree's path but not *branch* itself:
    a resumed release past its merge, whose ``release/<version>`` GitHub may
    already have deleted (#4889). The worktree checks out *base* with a
    detached ``HEAD``; *branch* is neither read nor created.
    """
    path = worktree_path(repo_root, branch)
    if path.exists():
        msg = f"Worktree path already exists: {path}. Remove it and re-run."
        raise ManagedWorktreeError(msg)
    git.run("worktree", "add", "--detach", str(path), base)
    return path


def adopt_worktree(repo_root: Path, *, branch: str) -> Path:
    """Attach a worktree under ``.worktrees/`` to an existing *branch*.

    The resume counterpart to :func:`create_worktree`: if the worktree path
    already exists (a prior run left it), it is reused as-is; otherwise a
    worktree is created checked out on the existing *branch* rather than a new
    branch off a base.

    A failed ``git worktree add`` (most often *branch* already checked out in
    another worktree) raises :class:`ManagedWorktreeError` naming the branch,
    the worktree and the recovery step, with git's own output as its detail
    (#4442).
    """
    path = worktree_path(repo_root, branch)
    if path.exists():
        return path
    try:
        git.run("worktree", "add", str(path), branch)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"Could not attach the worktree {path} to branch '{branch}'. If the "
            "branch is checked out in another worktree (see `git worktree list`), "
            "remove that worktree or switch it to another branch, then re-run."
        )
        raise ManagedWorktreeError(msg, detail=error_text(exc)) from exc
    return path


def remove_worktree(path: Path) -> None:
    """Force-remove a managed worktree (the branch ref is left for the caller)."""
    git.run("worktree", "remove", "--force", str(path))
