"""Preflight checks for vrg-release."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, github, version
from vergil_tooling.lib.managed_worktree import (
    ManagedWorktreeError,
    adopt_worktree,
    create_worktree,
)
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import find_existing_tracking_issue

if TYPE_CHECKING:
    from pathlib import Path

_VERSION_OVERRIDE_FIELDS = ("minor", "major")


def preflight(
    *,
    version_override: str | None,
    repo_root: Path,
    resume: bool = False,
) -> ReleaseContext:
    """Run all preflight checks and return an initialized ReleaseContext.

    On *resume* (#1612), the checks that assume a fresh start are skipped — the
    version is expected to be tagged and the tracking issue is expected to exist
    — and an existing release branch is adopted rather than refused.
    """
    _check_host_prerequisites()
    repo = check_gh_auth()
    _read_and_validate_config(repo_root)
    _check_branch_and_tree()

    try:
        current_version = version.show(repo_root)
    except (FileNotFoundError, version.VersionSyncError) as exc:
        raise ReleaseError(
            phase="preflight",
            command="version.show",
            message=str(exc),
        ) from exc

    if version_override in _VERSION_OVERRIDE_FIELDS:
        release_version = _compute_release_version(current_version, version_override)
    else:
        release_version = current_version

    if not resume:
        _check_version_not_tagged(release_version)
        _check_no_existing_tracking_issue(repo, release_version)

    branch = f"release/{release_version}"
    worktree = _acquire_release_worktree(repo_root, branch, resume=resume)
    os.chdir(worktree)

    print(f"Preflight passed: {repo} v{release_version} — worktree {worktree}")
    return ReleaseContext(
        repo=repo,
        version=release_version,
        repo_root=repo_root,
        version_override=version_override,
        release_branch=branch,
        worktree_path=worktree,
    )


def _acquire_release_worktree(repo_root: Path, branch: str, *, resume: bool) -> Path:
    """Create the managed worktree on *branch* off develop, or adopt it on resume.

    All release branch work happens in the worktree, so the root checkout's HEAD
    never moves while the release runs (#1578). A fresh run refuses an existing
    release branch (the lock); a resume adopts it — creating the local branch
    from origin if needed, then attaching a worktree (#1612).
    """
    exists = git.ref_exists(branch) or git.ref_exists(f"origin/{branch}")
    if exists and not resume:
        raise ReleaseError(
            phase="preflight",
            command=f"git rev-parse {branch}",
            message=f"Release branch '{branch}' already exists.",
        )
    try:
        if resume and exists:
            if not git.ref_exists(branch):
                git.run("branch", branch, f"origin/{branch}")
            return adopt_worktree(repo_root, branch=branch)
        return create_worktree(repo_root, branch=branch, base="develop")
    except ManagedWorktreeError as exc:
        raise ReleaseError(
            phase="preflight",
            command=f"git worktree add {branch}",
            message=str(exc),
        ) from exc


def _compute_release_version(current: str, override: str) -> str:
    """Compute the target release version without modifying any files."""
    parts = current.split(".")
    if override == "minor":
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{int(parts[0]) + 1}.0.0"


def _check_host_prerequisites() -> None:
    if shutil.which("git-cliff") is None:
        raise ReleaseError(
            phase="preflight",
            command="which git-cliff",
            message="git-cliff is not on PATH. Install it before running vrg-release.",
        )


def run_audit() -> None:
    """Standalone audit stage: resolve the repo and audit its GitHub config."""
    audit_repo_config(check_gh_auth())


def check_gh_auth() -> str:
    try:
        return github.read_output(
            "repo",
            "view",
            "--json",
            "nameWithOwner",
            "--jq",
            ".nameWithOwner",
        )
    except Exception as exc:
        raise ReleaseError(
            phase="preflight",
            command="gh repo view",
            message="GitHub CLI authentication failed.",
            detail=str(exc),
        ) from exc


def _read_and_validate_config(repo_root: Path) -> None:
    config.read_config(repo_root)


def _check_branch_and_tree() -> None:
    branch = git.current_branch()
    if branch != "develop":
        raise ReleaseError(
            phase="preflight",
            command="git rev-parse --abbrev-ref HEAD",
            message=f"Must be on develop branch (currently on '{branch}').",
        )
    status = git.read_output("status", "--porcelain")
    if status:
        raise ReleaseError(
            phase="preflight",
            command="git status --porcelain",
            message="Working tree is not clean.",
            detail=status,
        )
    git.run("fetch", "--tags", "--force", "origin", "develop")
    local_sha = git.read_output("rev-parse", "HEAD")
    remote_sha = git.read_output("rev-parse", "origin/develop")
    if local_sha != remote_sha:
        raise ReleaseError(
            phase="preflight",
            command="git rev-parse HEAD vs origin/develop",
            message=(
                f"Local develop ({local_sha[:8]}) does not match "
                f"origin/develop ({remote_sha[:8]}). Pull latest first."
            ),
        )


def audit_repo_config(repo: str) -> None:
    result = subprocess.run(  # noqa: S603
        ("vrg-github-repo-config", "audit", "--repo", repo),  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseError(
            phase="preflight",
            command=f"vrg-github-repo-config audit --repo {repo}",
            message="Repository configuration is non-compliant.",
            detail=result.stdout + result.stderr,
        )


def _check_version_not_tagged(ver: str) -> None:
    try:
        latest_tag = git.read_output(
            "describe",
            "--tags",
            "--abbrev=0",
            "--match",
            "v*",
        )
    except subprocess.CalledProcessError:
        return
    if latest_tag == f"v{ver}":
        raise ReleaseError(
            phase="preflight",
            command="git describe --tags --match v*",
            message=(
                f"Version {ver} is already tagged as {latest_tag}. "
                f"The post-publish version bump may not have run."
            ),
        )


def _check_no_existing_tracking_issue(repo: str, ver: str) -> None:
    existing = find_existing_tracking_issue(repo, ver)
    if existing is not None:
        raise ReleaseError(
            phase="preflight",
            command=f"gh issue list --search 'release: {ver}'",
            message=(
                f"A tracking issue already exists for version {ver}: {existing}\n"
                f"Close the stale issue or investigate before re-running."
            ),
        )
