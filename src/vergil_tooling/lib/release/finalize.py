"""Phase 5: Close tracking issue and run vrg-finalize-pr."""

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


def teardown_worktree(ctx: ReleaseContext) -> None:
    """Return to the root checkout and remove the release worktree (#1578).

    Must run before ``close-finalize``: ``vrg-finalize-pr`` refuses to run
    outside the main worktree, and its cleanup stage syncs ``develop`` and
    prunes branches in the root checkout. The release branches
    (``release/<v>``, ``release/post-<v>``) are left as refs for
    ``vrg-finalize-pr`` to prune as merged — exactly as in the pre-worktree
    flow, where they were also left behind in the root checkout.
    """
    if ctx.worktree_path is None:
        return
    os.chdir(ctx.repo_root)
    if ctx.worktree_path.exists():
        print(f"Removing release worktree: {ctx.worktree_path}")
        remove_worktree(ctx.worktree_path)
    ctx.worktree_path = None


def close_and_finalize(ctx: ReleaseContext) -> None:
    """Close the tracking issue with a summary, then finalize the repo."""
    summary = _build_summary(ctx)
    close_tracking_issue(ctx, summary)
    print("Tracking issue closed.")

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
    try:
        progress.run(
            ("vrg-finalize-pr", "--cleanup-only", "--output-format", "plain"),  # noqa: S607
            stdin=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as exc:
        raise ReleaseError(
            phase="close-finalize",
            command="vrg-finalize-pr --cleanup-only --output-format plain",
            message="vrg-finalize-pr failed.",
            detail=exc.stderr,
        ) from exc
    print("Finalization complete.")


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
    if ctx.develop_tag:
        lines.append(f"- Develop boundary tag: `{ctx.develop_tag}`")
    lines.append("")
    lines.append("### Artifacts")
    if ctx.release_url:
        lines.append(f"- GitHub Release: {ctx.release_url}")
    if ctx.cd_run_url:
        lines.append(f"- CD workflow (main): {ctx.cd_run_url}")
    if ctx.develop_cd_run_url:
        lines.append(f"- Develop CD workflow: {ctx.develop_cd_run_url}")
    return "\n".join(lines)
