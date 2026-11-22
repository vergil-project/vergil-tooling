"""The vrg-release ``update-deps`` stage (epic vergil-project/.github#381, spec §5.1).

Runs after finalize, once the release is tagged, promoted and published, so
nothing about the release depends on it (mode fail_defer): its failure holds
back neither ``install`` nor anything before it. Develop is already on the next
patch version, so the update is the first change on it.

A failed update is deferred, not halted: the stage is not recorded done in the
state block, and ``close-tracking`` holds on it, so the tracking issue stays
open and a ``vrg-release --resume`` runs the update again (spec §4.4 of epic
vergil-project/.github#1440). ``--without-deps``, given on the first run or on a resume, is
recorded in the state block's ``without_deps`` and skips the stage on that run
and every later resume, which records the skip as the stage done (spec §4.5 of
epic vergil-project/.github#1440).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.checkout_lock import checkout_lock
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.update_deps import report
from vergil_tooling.lib.update_deps.failure import failure_markdown, failure_summary
from vergil_tooling.lib.update_deps.orchestrator import run_update

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def update_deps(ctx: ReleaseContext) -> None:
    """Run the vrg-update-deps pipeline and record its outcome on *ctx*.

    A recorded update failure (the PR stays open with a fix task filed, or no PR
    for a pre-validation failure) is raised as a ReleaseError, so the tracking
    issue gets the failure comment and publish-status repeats it.
    """
    if ctx.without_deps:
        # Given on this run or recorded in the state block by an earlier one.
        print("Skipping dependency update (--without-deps).")
        ctx.deps_report = "Skipped (--without-deps)."
        return
    # The back-merge just moved origin/develop; bring the root checkout's
    # develop up to it before vrg-update-deps' preflight checks it. A failed sync
    # raises and the stage fails (deferred) with nothing updated.
    os.chdir(ctx.repo_root)
    # The sync holds the checkout lock (spec §6.4), so it never races another
    # run's sync or cleanup of the same clone. It is released before
    # run_update: the lock is not re-entrant, and nothing nests under it.
    with checkout_lock(ctx.repo_root, purpose="vrg-release update-deps"):
        branch = git.current_branch()
        if branch != "develop":
            # A fast-forward merge would otherwise move whatever branch is checked out.
            raise ReleaseError(
                phase="update-deps",
                command="git rev-parse --abbrev-ref HEAD",
                message=(
                    "The root checkout must be on develop to sync it for the dependency "
                    f"update (currently on '{branch}')."
                ),
            )
        git.run("fetch", "origin", "develop")
        git.run("merge", "--ff-only", "origin/develop")
    # A merge can land between this sync and update-deps' own preflight fetch,
    # so that preflight fast-forwards too, under the lock (spec §6.6).
    deps = run_update(repo_root=ctx.repo_root, fast_forward=True)
    if deps.landed_pr_url is not None:
        # A re-run (e.g. --resume) after the update for this develop version
        # already merged: done, not a failure (#3330).
        ctx.deps_landed_pr_url = deps.landed_pr_url
        ctx.deps_pr_url = deps.landed_pr_url
        return
    ctx.deps_report = report.render_markdown(report.collect(deps.results))
    ctx.deps_pr_url = deps.pr_url
    ctx.deps_fix_issue_url = deps.fix_issue_url
    if deps.failure is not None:
        ctx.deps_failure = failure_summary(deps)
        # The tracking comment renders "**Detail:** <detail>"; the leading blank
        # line keeps the Markdown table and heading from starting mid-line.
        raise ReleaseError(
            phase="update-deps",
            command="vrg-update-deps",
            message=ctx.deps_failure,
            detail=f"\n\n{ctx.deps_report}\n\n{failure_markdown(deps.failure)}",
        )
