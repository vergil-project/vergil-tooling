"""The teardown-worktree, finalize and close-tracking stages of vrg-release."""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import progress
from vergil_tooling.lib.managed_worktree import remove_worktree
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import close_tracking_issue

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

# The stage that posts the records close-tracking waits for.
_RECORDS = "release-records"

_FINALIZE_CMD = (
    "vrg-finalize-pr",
    "--cleanup-only",
    "--output-format",
    "plain",
)


def teardown_worktree(ctx: ReleaseContext) -> None:
    """Return to the root checkout and remove the release worktree (#1578).

    Must run before ``finalize``: ``vrg-finalize-pr`` refuses to run
    outside the main worktree, and its cleanup stage syncs ``develop`` and
    prunes branches in the root checkout. The release branches
    (``release/<v>``, ``release/post-<v>``) are left as refs for
    ``vrg-finalize-pr`` to prune as merged — exactly as in the pre-worktree
    flow, where they were also left behind in the root checkout.

    With no worktree to remove (a resume past ``merge-release`` that found
    none, since no stage after it needs one: #4889), the stage does nothing
    and succeeds, so it is recorded done.
    """
    if ctx.worktree_path is None:
        print("No release worktree to remove.")
        return
    os.chdir(ctx.repo_root)
    if ctx.worktree_path.exists():
        print(f"Removing release worktree: {ctx.worktree_path}")
        remove_worktree(ctx.worktree_path)
    else:
        print(f"Release worktree already removed: {ctx.worktree_path}")
    ctx.worktree_path = None


def finalize(ctx: ReleaseContext) -> None:  # noqa: ARG001 - every stage takes the context
    """Finalize the repo with ``vrg-finalize-pr --cleanup-only``, pruning the release branches.

    The tracking issue stays open: ``close-tracking`` closes it as the
    release's last tracked stage (spec §4.4 of epic
    vergil-project/.github#1440). The hold rule runs this stage only once
    every stage through ``confirm-develop`` has succeeded, so every
    branch-work stage is done before its branch is pruned.
    """
    print("Running vrg-finalize-pr...")
    # --cleanup-only is the non-interactive release path: no PR
    # inference, no prompts (issue #1448). Output streams through the
    # progress session so the live display stays intact and the run log
    # captures the cleanup narration (issue #1470) — the child must not
    # inherit the TTY: raw writes under the live display strand stale
    # frames on screen. The child is itself progress-aware (issue #1479);
    # --output-format plain states the rendering contract explicitly
    # because two live displays cannot nest (TTY auto-detection on the
    # piped stdout is the backstop). stdin is closed so the child can
    # never block on a terminal read. Captured stderr rides on
    # CalledProcessError for ReleaseError.detail; the streamed lines
    # mean warnings are never silently swallowed.
    #
    # No --wait-cd: the release has already verified develop's CD, pinned to
    # the back-merge's merge commit, in confirm-develop (#3329). Finalize's
    # cleanup-only cd-check has no merge to pin to, so it looks at develop's
    # current tip instead — on a resume that can be an unrelated commit (a
    # dependency-update or feature PR merged since), whose CD says nothing
    # about the release. Without --wait-cd it only reports that run and never
    # fails on it, so it cannot re-fail on a docs publish failure
    # confirm-develop deliberately deferred. The hold rule runs this stage only
    # once confirm-develop has succeeded, so not waiting a second time loses
    # no coverage.
    try:
        progress.run(_FINALIZE_CMD, stdin=subprocess.DEVNULL)
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="finalize",
            command=" ".join(_FINALIZE_CMD),
            message="vrg-finalize-pr failed.",
            detail=exc.stderr,
        ) from exc
    print("Finalization complete.")


def close_tracking(ctx: ReleaseContext) -> None:
    """Post the release summary on the tracking issue and close it, once every record is posted.

    The release's last tracked stage. The hold rule runs it only once no
    stage before it has failed, so a failed or deferred stage leaves the
    issue open for ``vrg-release --resume`` (spec §4.4 of epic
    vergil-project/.github#1440). It also refuses, leaving the issue open,
    unless the state block records ``release-records`` done and a record
    posted on every issue the release contains (spec §4.9).
    """
    _require_records(ctx)
    close_tracking_issue(ctx, _build_summary(ctx))
    print("Tracking issue closed.")


def _require_records(ctx: ReleaseContext) -> None:
    """Refuse unless the state block records every issue's ``Released:`` record posted."""
    current = ctx.state
    if current is None:
        raise ReleaseError(
            phase="close-tracking",
            command="close-tracking",
            message=(
                "the run holds no release state block, so whether every release record "
                "is posted is unknown; leaving the tracking issue open."
            ),
        )
    missing = [i.ref for i in current.issues if i.ref not in current.records_posted]
    if _RECORDS not in current.done or missing:
        what = f"no record on {', '.join(missing)}" if missing else f"{_RECORDS} is not done"
        raise ReleaseError(
            phase="close-tracking",
            command="close-tracking",
            message=(
                f"the state block records {what}, so the tracking issue stays open. "
                f"`vrg-release --resume {ctx.version}` posts the records, then closes it."
            ),
        )


def _build_summary(ctx: ReleaseContext) -> str:
    lines = [
        f"## Release {ctx.version} — Summary",
        "",
        "### Pull Requests",
        f"- Release PR: {ctx.release_pr_url}",
        f"- Back-merge PR: {ctx.bump_pr_url}",
        "",
        "### Tags",
    ]
    if ctx.tag:
        lines.append(f"- Release tag: `{ctx.tag}`")
    lines.append("")
    lines.append("### Artifacts")
    if ctx.release_url:
        lines.append(f"- GitHub Release: {ctx.release_url}")
    if ctx.cd_run_url:
        lines.append(f"- CD workflow (main): {ctx.cd_run_url}")
    if ctx.develop_cd_run_url:
        lines.append(f"- Develop CD workflow: {ctx.develop_cd_run_url}")
    lines.append("")
    lines.append("### Released: records")
    posted = ctx.state.records_posted if ctx.state is not None else ()
    if posted:
        lines.extend(f"- {ref}" for ref in posted)
    else:
        lines.append("- None: the release's PRs close no issues.")
    return "\n".join(lines)
