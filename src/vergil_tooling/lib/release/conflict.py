"""Self-heal a CONFLICTING release PR by merging main into the release branch.

The usual cause of a conflicting release PR is ``main`` having moved since the
release branch was cut, and the usual conflict is ``CHANGELOG.md`` alone
(#3526). That file is a generated artifact: prepare regenerated the release
branch's copy with git-cliff from the full history being released, so it
supersedes main's older generation and the conflict resolves by keeping the
release branch's side.

Only a conflict that is provably CHANGELOG-only is resolved. Any other
conflicted path aborts the merge, leaves the worktree as it was, and raises
``ReleaseError`` with the commands to resolve it by hand.

Re-running is idempotent: when main is already merged into the release branch
(a prior run merged but did not push, or GitHub's ``mergeable`` is stale) no
new merge is made, and the push of an unchanged branch is a no-op.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import sync_local_branch

if TYPE_CHECKING:
    from pathlib import Path

CHANGELOG = "CHANGELOG.md"
_BASE = "main"


def merge_main_into_release(pr_url: str, *, branch: str, worktree: Path, phase: str) -> None:
    """Merge ``origin/main`` into *branch* (checked out in *worktree*) and push it.

    The local branch is first brought up to the PR's head when GitHub moved
    it (an update-branch merge commit), so the push is a fast-forward. A
    CHANGELOG-only conflict is resolved by keeping the release branch's
    CHANGELOG; any other conflict aborts the merge and raises ``ReleaseError``.
    """
    if (git.worktree_git_dir(worktree) / "MERGE_HEAD").exists():
        raise ReleaseError(
            phase=phase,
            command="git merge",
            message=(
                f"A merge is already in progress in {worktree}; refusing to start "
                f"another. Finish it (git commit) or abort it (git merge --abort) "
                f"there, then re-run: vrg-release --resume"
            ),
        )
    wt = str(worktree)
    git.run("-C", wt, "fetch", "origin", _BASE, branch)
    if not git.is_ancestor(f"origin/{branch}", branch):
        # GitHub moved the PR branch (update-branch). A local tip ahead of it
        # (a prior run merged main but did not push) needs no sync.
        sync_local_branch(pr_url, branch=branch, worktree=worktree, phase=phase)
    base = f"origin/{_BASE}"
    if git.is_ancestor(base, branch):
        print(f"{base} is already merged into {branch} — nothing to merge.")
    else:
        print(f"Release PR conflicts with {_BASE} — merging {base} into {branch}...")
        _merge(wt, base, branch=branch, worktree=worktree, phase=phase)
    git.run("-C", wt, "push", "origin", branch)


def _merge(wt: str, base: str, *, branch: str, worktree: Path, phase: str) -> None:
    message = f"chore(release): merge {_BASE} into {branch}"
    try:
        git.run("-C", wt, "merge", "--no-ff", "-m", message, base)
    except subprocess.CalledProcessError as exc:
        conflicted = _conflicted_paths(wt)
        if not conflicted:
            # The merge failed before producing conflicts (e.g. local changes
            # it would overwrite): nothing was merged, so nothing to abort.
            raise ReleaseError(
                phase=phase,
                command=f"git merge {base}",
                message=(
                    f"Merging {base} into {branch} failed without a conflict. "
                    f"Inspect worktree {worktree}, then re-run: vrg-release --resume"
                ),
                detail=exc.stderr or exc.stdout,
            ) from exc
        if conflicted != [CHANGELOG]:
            git.run("-C", wt, "merge", "--abort")
            raise ReleaseError(
                phase=phase,
                command=f"git merge {base}",
                message=_manual_resolution(conflicted, base, branch=branch, worktree=worktree),
            ) from exc
        print(f"Only {CHANGELOG} conflicts — keeping the release branch's generated copy.")
        git.run("-C", wt, "checkout", "--ours", "--", CHANGELOG)
        git.run("-C", wt, "add", "--", CHANGELOG)
        git.run("-C", wt, "commit", "--no-edit")
    print(f"Merged {base} into {branch}.")


def _conflicted_paths(wt: str) -> list[str]:
    """The paths left unmerged by a failed merge, sorted."""
    out = git.read_output("-C", wt, "diff", "--name-only", "--diff-filter=U")
    return sorted(line for line in out.splitlines() if line)


def _manual_resolution(conflicted: list[str], base: str, *, branch: str, worktree: Path) -> str:
    paths = "\n".join(f"  {p}" for p in conflicted)
    return (
        f"The release PR conflicts with {_BASE} beyond {CHANGELOG}, so it was not "
        f"resolved automatically (the merge was aborted; {branch} is unchanged). "
        f"Conflicted paths:\n{paths}\n"
        f"Resolve by hand, then resume:\n"
        f"  cd {worktree}\n"
        f"  git fetch origin {_BASE}\n"
        f"  git merge {base}\n"
        f"  # resolve the conflicts, git add them, then git commit\n"
        f"  git push origin {branch}\n"
        f"  vrg-release --resume"
    )
