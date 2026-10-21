"""Preflight checks and managed-worktree creation for vrg-update-deps."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import branch_sweep, config, git, github, version
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


def _pr_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def _finished_pr(
    repo: str, branch: str, develop_version: str, *, local: bool, remote: bool
) -> branch_sweep.PullRequest:
    """The merged or closed PR an existing *branch* belongs to, else refuse (#3330).

    The branch is in flight — and preflight refuses — when it has no PR, when
    any of its PRs is open, or when a present copy's tip is not the newest
    PR's head (the name was reused after that PR finished, so the branch holds
    commits no PR covers). The same rules as ``vrg-branch-sweep``.
    """
    try:
        prs = branch_sweep.list_prs(repo, branch)
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise UpdateDepsError(
            phase="preflight",
            command=f"gh pr list --head {branch} --state all",
            message=(
                f"{branch} already exists, and its PRs could not be listed; "
                "refusing to treat it as anything but in flight."
            ),
            detail=str(exc),
        ) from exc
    newest = branch_sweep.newest_pr(prs)
    if newest is None:
        raise _in_flight(branch, develop_version, "it has no PR")
    open_prs = [pr.number for pr in prs if pr.state == "OPEN"]
    if open_prs:
        numbers = ", ".join(f"#{n}" for n in sorted(open_prs))
        raise _in_flight(branch, develop_version, f"open PR {numbers}")
    if newest.state not in {"MERGED", "CLOSED"}:
        raise _in_flight(
            branch, develop_version, f"PR #{newest.number} has unexpected state {newest.state!r}"
        )
    tips: list[str] = []
    if local:
        tips.append(git.read_output("rev-parse", f"refs/heads/{branch}"))
    if remote:
        tips.append(branch_sweep.current_tip(repo, branch))
    for tip in tips:
        if tip != newest.head_oid:
            raise _in_flight(
                branch,
                develop_version,
                f"its tip {tip[:12]} is not the head of its last PR "
                f"#{newest.number}, {newest.head_oid[:12]}",
            )
    return newest


def _delete_stale_branch(
    repo: str, branch: str, pr: branch_sweep.PullRequest, *, local: bool, remote: bool
) -> None:
    """Delete *branch* left behind by a PR closed without merging (#3330).

    GitHub's delete-on-merge setting never covers a closed PR. Unlike the
    release gate's best-effort delete (#3332), a failure here refuses: the
    update cannot push a new PR head over the stale branch.
    """
    url = _pr_url(repo, pr.number)
    if remote:
        try:
            github.run("api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}")
        except subprocess.CalledProcessError as exc:
            raise UpdateDepsError(
                phase="preflight",
                command=f"git push origin --delete {branch}",
                message=(
                    f"{branch} is stale (PR {url} was closed without merging), but "
                    "deleting it on origin failed. Delete it by hand and re-run."
                ),
                detail=str(exc),
            ) from exc
        print(f"Deleted stale branch {branch} on origin: PR {url} was closed without merging.")
    if local:
        try:
            git.run("branch", "-D", branch)
        except subprocess.CalledProcessError as exc:
            raise UpdateDepsError(
                phase="preflight",
                command=f"git branch -D {branch}",
                message=(
                    f"{branch} is stale (PR {url} was closed without merging), but "
                    "deleting the local branch failed. Delete it by hand and re-run."
                ),
                detail=str(exc),
            ) from exc
        print(f"Deleted stale local branch {branch}: PR {url} was closed without merging.")


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
    # A branch whose PR finished is not in flight (#3330): merged means the
    # update for this version is done; closed unmerged means the branch is stale.
    local = git.ref_exists(f"refs/heads/{branch}")
    remote = branch in git.remote_branches()
    if local or remote:
        finished = _finished_pr(repo, branch, develop_version, local=local, remote=remote)
        if finished.state == "MERGED":
            url = _pr_url(repo, finished.number)
            print(
                f"The dependency update for {develop_version} already merged in {url}; "
                "nothing to do."
            )
            return UpdateDepsContext(
                repo=repo,
                repo_root=repo_root,
                branch=branch,
                version=develop_version,
                landed_pr_url=url,
            )
        _delete_stale_branch(repo, branch, finished, local=local, remote=remote)
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
