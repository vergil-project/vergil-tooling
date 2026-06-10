"""Preflight checks and managed-worktree creation for vrg-update-deps."""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git
from vergil_tooling.lib.managed_worktree import ManagedWorktreeError, create_worktree
from vergil_tooling.lib.release.preflight import check_gh_auth
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError

if TYPE_CHECKING:
    from pathlib import Path


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")  # noqa: DTZ005


def preflight(*, repo_root: Path) -> UpdateDepsContext:
    """Validate preconditions and create the managed worktree."""
    repo = check_gh_auth()
    config.read_config(repo_root)

    branch_now = git.current_branch()
    if branch_now != "develop":
        raise UpdateDepsError(
            phase="preflight",
            command="git rev-parse --abbrev-ref HEAD",
            message=f"Must be on develop branch (currently on '{branch_now}').",
        )
    if git.read_output("status", "--porcelain"):
        raise UpdateDepsError(
            phase="preflight",
            command="git status --porcelain",
            message="Working tree is not clean.",
        )
    git.run("fetch", "origin", "develop")
    local_sha = git.read_output("rev-parse", "HEAD")
    remote_sha = git.read_output("rev-parse", "origin/develop")
    if local_sha != remote_sha:
        raise UpdateDepsError(
            phase="preflight",
            command="git rev-parse HEAD vs origin/develop",
            message=(
                f"Local develop ({local_sha[:8]}) is not in sync with "
                f"origin/develop ({remote_sha[:8]}). Pull latest first."
            ),
        )

    branch = f"chore/dep-update-{_today()}"
    try:
        worktree_path = create_worktree(repo_root, branch=branch, base="develop")
    except ManagedWorktreeError as exc:
        raise UpdateDepsError(
            phase="preflight",
            command=f"git worktree add {branch}",
            message=str(exc),
        ) from exc
    os.chdir(worktree_path)
    print(f"Preflight passed: {repo} — worktree {worktree_path}")
    return UpdateDepsContext(
        repo=repo,
        repo_root=repo_root,
        branch=branch,
        worktree_path=worktree_path,
    )
