"""Preflight checks and managed-worktree creation for vrg-update-deps."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, version
from vergil_tooling.lib.managed_worktree import ManagedWorktreeError, create_worktree
from vergil_tooling.lib.release.preflight import check_gh_auth
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError

if TYPE_CHECKING:
    from pathlib import Path


def _in_flight(branch: str, develop_version: str, why: str) -> UpdateDepsError:
    return UpdateDepsError(
        phase="preflight",
        command=f"git worktree add {branch}",
        message=(
            f"{branch} already exists ({why}). An earlier dependency update for "
            f"{develop_version} is still in flight; land or close its "
            "dependency-update PR, then delete the branch."
        ),
    )


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

    develop_version = version.show(repo_root)
    # deps/ is deliberately not an issue-branch prefix (lib/branch_names.py):
    # only this driver commits here. Fixes go on chore/<N>-dep-update-<version>
    # (spec §5.1, §5.2). Version-keyed, because repos release several times a day.
    branch = f"deps/update-{develop_version}"
    # A failed run deletes its local branch but leaves it on origin as the open
    # PR's head, so check both: never reuse or clobber an in-flight update.
    if git.ref_exists(f"refs/heads/{branch}") or branch in git.remote_branches():
        raise _in_flight(branch, develop_version, "the branch is present locally or on origin")
    try:
        worktree_path = create_worktree(repo_root, branch=branch, base="develop")
    except ManagedWorktreeError as exc:
        raise _in_flight(branch, develop_version, str(exc)) from exc
    os.chdir(worktree_path)
    print(f"Preflight passed: {repo} — worktree {worktree_path}")
    return UpdateDepsContext(
        repo=repo,
        repo_root=repo_root,
        branch=branch,
        worktree_path=worktree_path,
        version=develop_version,
    )
