"""Sequential phase runner for vrg-release."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.promote import promote
from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.confirm import confirm_develop, confirm_main
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
    from collections.abc import Callable

    from vergil_tooling.lib.release.context import ReleaseContext


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs:02d}s"


def merge_release(ctx: ReleaseContext) -> None:
    """Phase 2: merge the release PR."""
    if ctx.release_pr_url is None:
        raise ReleaseError(
            phase="merge-release",
            command="merge_release",
            message=("release_pr_url is not set — prepare phase may not have run."),
        )
    wait_and_merge(
        ctx.release_pr_url,
        phase="merge-release",
        verbose=ctx.verbose,
    )
    ctx.release_merge_sha = "merged"


def _promote_phase(ctx: ReleaseContext) -> None:
    """Phase 6: update the vX.Y rolling tag (unless --no-promote)."""
    if not ctx.promote:
        print("Skipping promote (--no-promote).")
        return
    if ctx.skip_cd:
        tag = f"v{ctx.version}"
        if not git.ref_exists(tag):
            print(f"Skipping promote — tag {tag} not found (CD may not have completed).")
            return
    promote(ctx.version)


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
    elif phase == "confirm-main":
        if ctx.skip_cd:
            if ctx.tag:
                lines.append(f"CD skipped. Tag `{ctx.tag}` found.")
            else:
                lines.append(f"CD skipped. Tag v{ctx.version} not yet available.")
        else:
            if ctx.tag:
                lines.append(f"Tag: `{ctx.tag}`")
            if ctx.release_url:
                lines.append(f"Release: {ctx.release_url}")
            if ctx.cd_run_url:
                lines.append(f"CD workflow: {ctx.cd_run_url}")
    elif phase == "back-merge-bump":
        if ctx.bump_pr_url:
            lines.append(f"Back-merge PR: {ctx.bump_pr_url}")
        if ctx.next_version:
            lines.append(f"Next version: {ctx.next_version}")
    elif phase == "confirm-develop":
        if ctx.develop_cd_run_url:
            lines.append(f"Develop CD: {ctx.develop_cd_run_url}")
    elif phase == "promote":
        if ctx.promote:
            major_minor = ".".join(ctx.version.split(".")[:2])
            lines.append(f"Promoted v{major_minor} -> v{ctx.version}")
        else:
            lines.append("Promote skipped (--no-promote).")
    elif phase == "close-finalize":
        lines.append("Tracking issue closed. Repository finalized.")
    elif phase == "consumer-refresh":
        lines.append("Consumer refresh instructions displayed.")
    return "\n".join(lines)


def _confirm_main_phase(ctx: ReleaseContext) -> None:
    if ctx.skip_cd:
        print("Skipping CD verification (--skip-cd).")
        git.run("fetch", "--tags", "--force", "origin")
        tag = f"v{ctx.version}"
        if git.ref_exists(tag):
            ctx.tag = tag
            print(f"  Tag {tag} found.")
        else:
            print(f"  Tag {tag} not yet available.")
        return
    confirm_main(ctx)


def _confirm_develop_phase(ctx: ReleaseContext) -> None:
    if ctx.skip_cd:
        print("Skipping develop CD verification (--skip-cd).")
        return
    confirm_develop(ctx)


def run_release(ctx: ReleaseContext) -> None:
    """Execute the release workflow phase by phase."""
    phases: list[tuple[str, Callable[[ReleaseContext], None]]] = [
        ("prepare", prepare),
        ("merge-release", merge_release),
        ("confirm-main", _confirm_main_phase),
        ("back-merge-bump", back_merge_and_bump),
        ("confirm-develop", _confirm_develop_phase),
        ("promote", _promote_phase),
        ("close-finalize", close_and_finalize),
        ("consumer-refresh", consumer_refresh),
    ]

    for phase_name, phase_fn in phases:
        print(f"\n=== Phase: {phase_name} ===")
        start = time.monotonic()
        try:
            phase_fn(ctx)
        except ReleaseError as exc:
            elapsed = time.monotonic() - start
            print(f"=== {phase_name}: FAILED ({_format_elapsed(elapsed)}) ===")
            comment_phase_failed(ctx, phase_name, exc)
            raise
        except Exception as exc:
            elapsed = time.monotonic() - start
            print(f"=== {phase_name}: FAILED ({_format_elapsed(elapsed)}) ===")
            wrapped = ReleaseError(
                phase=phase_name,
                command=str(getattr(exc, "cmd", type(exc).__name__)),
                message=str(exc),
                detail=(getattr(exc, "stderr", None) or getattr(exc, "stdout", None)),
            )
            comment_phase_failed(ctx, phase_name, wrapped)
            raise wrapped from exc
        elapsed = time.monotonic() - start
        print(f"=== {phase_name}: done ({_format_elapsed(elapsed)}) ===")
        try:
            comment_phase_complete(
                ctx,
                phase_name,
                _phase_details(ctx, phase_name),
            )
        except Exception as exc:
            raise ReleaseError(
                phase=f"comment({phase_name})",
                command="comment_phase_complete",
                message=str(exc),
                detail=(getattr(exc, "stderr", None) or getattr(exc, "stdout", None)),
            ) from exc
