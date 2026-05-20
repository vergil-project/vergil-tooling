"""Sequential phase runner for vrg-release."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.release.bump import merge_bump
from vergil_tooling.lib.release.confirm import confirm_publish
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.finalize import close_and_finalize
from vergil_tooling.lib.release.handoff import consumer_refresh
from vergil_tooling.lib.release.merge import wait_and_merge
from vergil_tooling.lib.release.prepare import prepare
from vergil_tooling.lib.release.tracking import (
    comment_phase_complete,
    comment_phase_failed,
)

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def merge_release(ctx: ReleaseContext) -> None:
    """Phase 2: merge the release PR."""
    assert ctx.release_pr_url is not None
    wait_and_merge(ctx.release_pr_url, phase="merge-release")
    ctx.release_merge_sha = "merged"


def _phase_details(ctx: ReleaseContext, phase: str) -> str:
    """Build human-readable details from ctx for a completed phase."""
    lines: list[str] = []
    if phase == "prepare":
        if ctx.release_branch:
            lines.append(f"Branch: `{ctx.release_branch}`")
        if ctx.release_pr_url:
            lines.append(f"PR: {ctx.release_pr_url}")
        if ctx.issue_url:
            lines.append(f"Tracking issue: {ctx.issue_url}")
    elif phase == "merge-release":
        if ctx.release_pr_url:
            lines.append(f"Merged: {ctx.release_pr_url}")
    elif phase == "merge-bump":
        if ctx.bump_pr_url:
            lines.append(f"Bump PR: {ctx.bump_pr_url}")
        if ctx.next_version:
            lines.append(f"Next version: {ctx.next_version}")
    elif phase == "confirm-publish":
        if ctx.tag:
            lines.append(f"Tag: `{ctx.tag}`")
        if ctx.release_url:
            lines.append(f"Release: {ctx.release_url}")
        if ctx.publish_run_url:
            lines.append(f"publish.yml: {ctx.publish_run_url}")
        if ctx.docs_run_url:
            lines.append(f"docs workflow: {ctx.docs_run_url}")
    elif phase == "close-finalize":
        lines.append("Tracking issue closed. Repository finalized.")
    elif phase == "consumer-refresh":
        lines.append("Consumer refresh instructions displayed.")
    return "\n".join(lines)


def run_release(ctx: ReleaseContext) -> None:
    """Execute the release workflow phase by phase."""
    phases: list[tuple[str, object]] = [
        ("prepare", prepare),
        ("merge-release", merge_release),
        ("merge-bump", merge_bump),
        ("confirm-publish", confirm_publish),
        ("close-finalize", close_and_finalize),
        ("consumer-refresh", consumer_refresh),
    ]

    for phase_name, phase_fn in phases:
        try:
            phase_fn(ctx)  # type: ignore[operator]
            comment_phase_complete(ctx, phase_name, _phase_details(ctx, phase_name))
        except ReleaseError as exc:
            comment_phase_failed(ctx, phase_name, exc)
            raise
        except Exception as exc:
            wrapped = ReleaseError(
                phase=phase_name,
                command=str(getattr(exc, "cmd", type(exc).__name__)),
                message=str(exc),
                detail=getattr(exc, "stderr", None) or getattr(exc, "stdout", None),
            )
            comment_phase_failed(ctx, phase_name, wrapped)
            raise wrapped from exc
