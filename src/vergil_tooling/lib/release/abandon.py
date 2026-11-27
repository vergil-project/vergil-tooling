"""The abandon step of a stray merge (spec §5.4 and §5.8 of epic vergil-project/.github#1664).

A PR merged into ``develop`` after the cut, other than the holder's own, and
found before ``merge-release`` (:func:`~vergil_tooling.lib.release.stray.stray_merges`)
is a hard error: nothing is tagged or published yet, so the release is
abandoned and a fresh ``vrg-release`` cuts again with the PR included.
:func:`abandon_release` undoes everything ``prepare`` made, in this order:

1. closes the release PR into ``main``: the one recorded, or, on a resume
   at ``prepare`` that records none yet, the release branch's open PR, if
   prepare opened one;
2. removes the ``release/X.Y.Z`` branch (local, and on origin) and the
   release worktree. This removal is sanctioned, and logged as every
   worktree removal is (spec §5.8);
3. closes the tracking issue as not planned, with an ``abandoned`` comment
   naming the reason;
4. only then runs the full unlock path: the lock is deleted, then the
   re-arm sweep offers every disarmed PR to the one PR path, and what it
   left unarmed is listed on the tracking issue.

Every step raises :class:`ReleaseError` when it fails, naming what is left.
Up to the tracking issue's close, the release is still open, its lock still
held, so ``vrg-release --resume`` runs the stray-merge check again and
abandons it again; each step is skipped when it is already done. The
orchestrator raises :class:`ReleaseAbandoned` once it is complete.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import github
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release.branch_pr import branch_prs
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.lock_guard import rearm, rearm_lines, release_lock
from vergil_tooling.lib.release.merge import known_pr_state
from vergil_tooling.lib.release.supersede import remove_release_branch
from vergil_tooling.lib.release.tracking import comment_phase_complete

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

__all__ = ["ReleaseAbandoned", "abandon_release"]

# The abandon step runs inside the merge-release stage, before the merge.
_PHASE = "merge-release"
_LABEL = "abandoned release"


class ReleaseAbandoned(ReleaseError):  # noqa: N818 - it names an outcome, not a fault
    """The release was abandoned on a stray merge; a fresh ``vrg-release`` cuts again.

    The tracking issue is closed and the lock is gone, so there is nothing
    for ``vrg-release --resume`` to finish.
    """

    def __init__(self, version: str, reason: str) -> None:
        super().__init__(
            phase=_PHASE,
            command="stray-merge check",
            message=(
                f"Release {version} is abandoned: {reason}. Nothing was tagged or published,"
                " the release PR, branch, worktree and tracking issue are closed or removed,"
                " and the release lock is released. Run a fresh `vrg-release` to cut again;"
                " the new release includes it."
            ),
        )
        self.reason = reason


def _gh_failed(command: str, message: str, exc: subprocess.CalledProcessError) -> ReleaseError:
    return ReleaseError(phase=_PHASE, command=command, message=message, detail=error_text(exc))


def _release_pr(ctx: ReleaseContext) -> str | None:
    """The release PR to close: the one recorded, or else the release branch's.

    A resume at ``prepare`` that re-took the lock (spec §5.3 of epic
    vergil-project/.github#1664) runs before the state block records the
    release PR, so it is found by the branch, through the trust layer. A
    merged one there means the release is published: refused. ``None`` when
    the branch has no open PR: prepare stopped before it opened one.
    """
    if ctx.release_pr_url is not None:
        return ctx.release_pr_url
    branch = ctx.release_branch or f"release/{ctx.version}"
    prs = branch_prs(ctx.repo, branch, phase=_PHASE)
    if prs.open_pr is not None:
        ctx.release_pr_url = prs.open_pr.url
        return ctx.release_pr_url
    merged = prs.latest_closed
    if merged is not None and merged.state == "MERGED":
        raise ReleaseError(
            phase=_PHASE,
            command=f"prs_for_branch {ctx.repo} {branch}",
            message=(
                f"The release PR {merged.url} is already merged into main, so the release is"
                " published and cannot be abandoned."
            ),
        )
    print(f"No release PR is open on '{branch}': there is none to close.")
    return None


def _close_release_pr(ctx: ReleaseContext, reason: str) -> None:
    pr_url = _release_pr(ctx)
    if pr_url is None:
        return
    state = known_pr_state(pr_url)
    if state == "MERGED":
        raise ReleaseError(
            phase=_PHASE,
            command="abandon_release",
            message=(
                f"The release PR {pr_url} is already merged into main, so the release is"
                " published and cannot be abandoned."
            ),
        )
    if state != "OPEN":
        print(f"The release PR is already closed: {pr_url}")
        return
    comment = f"Release {ctx.version} is abandoned: {reason}."
    try:
        # No --delete-branch: the branch is checked out in the release
        # worktree, so remove_release_branch deletes it, local and on origin,
        # once the worktree is gone.
        github.run("pr", "close", pr_url, "--comment", comment)
    except subprocess.CalledProcessError as exc:
        raise _gh_failed(
            f"gh pr close {pr_url}",
            f"The release PR {pr_url} could not be closed. Re-run: vrg-release --resume",
            exc,
        ) from exc
    print(f"Closed the release PR: {pr_url}")


def _close_tracking_issue(ctx: ReleaseContext, reason: str) -> None:
    body = "\n".join(
        [
            "<!-- vrg-release:abandoned -->",
            "",
            f"**Release {ctx.version} abandoned.** {reason}.",
            "",
            "Nothing was tagged or published. The release PR is closed, and the release"
            " branch and its worktree are removed. The release lock is released next and"
            " the disarmed PRs re-armed. Run a fresh `vrg-release` to cut again: the new"
            " release includes what merged.",
        ]
    )
    number = str(ctx.issue_number)
    try:
        github.run(
            "issue",
            "close",
            number,
            "--repo",
            ctx.repo,
            "--reason",
            "not planned",
            "--comment",
            body,
        )
    except subprocess.CalledProcessError as exc:
        raise _gh_failed(
            f"gh issue close {number} --repo {ctx.repo}",
            f"The tracking issue #{number} could not be closed. Re-run: vrg-release --resume",
            exc,
        ) from exc
    print(f"Closed tracking issue #{number}: release {ctx.version} is abandoned.")


def abandon_release(ctx: ReleaseContext, *, reason: str) -> None:
    """Abandon the release on *reason*: undo what ``prepare`` made, then unlock.

    See the module docstring for the order. Refuses, changing nothing, when
    the release PR is already merged. Any failure raises
    :class:`ReleaseError`.
    """
    print(f"Abandoning release {ctx.version}: {reason}")
    _close_release_pr(ctx, reason)
    # The worktree is about to go: leave it for the root checkout first.
    os.chdir(ctx.repo_root)
    remove_release_branch(
        ctx.repo_root,
        ctx.release_branch or f"release/{ctx.version}",
        phase=_PHASE,
        reason=f"the release was abandoned: {reason}",
        label=_LABEL,
    )
    ctx.worktree_path = None
    _close_tracking_issue(ctx, reason)
    # The tracking issue is closed, so --resume can no longer find the release.
    release_lock(
        ctx,
        phase=_PHASE,
        remedy=(
            "The release is abandoned and its tracking issue closed. Check the lock with"
            " `vrg-release-lock status`; if it is still held, remove it with"
            ' `vrg-release-lock break --reason "<why>"`, which also re-arms the PRs.'
        ),
    )
    result = rearm(
        ctx,
        phase=_PHASE,
        remedy=(
            "The release is abandoned and its lock released: turn auto-merge back on for"
            " each such PR (an agent's watch re-arms its own PR once the lock is gone)."
        ),
    )
    comment_phase_complete(ctx, "abandon", "\n".join(f"- {line}" for line in rearm_lines(result)))
