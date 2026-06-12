"""Declarative stage list for the vrg-release pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import github
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.promote import promote
from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.confirm import confirm_develop, confirm_main
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.finalize import close_and_finalize, teardown_worktree
from vergil_tooling.lib.release.handoff import consumer_refresh
from vergil_tooling.lib.release.merge import wait_and_merge
from vergil_tooling.lib.release.preflight import preflight, run_audit
from vergil_tooling.lib.release.prepare import prepare
from vergil_tooling.lib.release.tracking import (
    comment_phase_complete,
    comment_phase_failed,
    ensure_checklist,
    tick_stage,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext


@dataclass
class ReleaseState:
    """Pipeline context for vrg-release; ctx is populated by the preflight stage."""

    version_override: str | None
    repo_root: Path
    promote: bool
    ctx: ReleaseContext | None = None
    resume: bool = False
    resume_version: str | None = None
    resume_issue_number: int | None = None


def _audit_stage(_state: ReleaseState) -> None:
    run_audit()


def _preflight_stage(state: ReleaseState) -> None:
    ctx = preflight(
        version_override=state.version_override,
        repo_root=state.repo_root,
        resume=state.resume,
        resume_version=state.resume_version,
        resume_issue_number=state.resume_issue_number,
    )
    ctx.promote = state.promote
    state.ctx = ctx


def _teardown_stage(state: ReleaseState) -> None:
    """Remove the release worktree and return to the root checkout (#1578).

    Runs once the last branch work (back-merge-bump) is done, before
    close-finalize hands off to vrg-finalize-pr, which must run from the
    main worktree.
    """
    if state.ctx is not None:
        teardown_worktree(state.ctx)


def _tracked(name: str, fn: Callable[[ReleaseContext], None]) -> Callable[[ReleaseState], None]:
    """Wrap a phase fn with tracking-issue comments (command-local, per spec)."""

    def stage(state: ReleaseState) -> None:
        ctx = state.ctx
        if ctx is None:
            raise ReleaseError(
                phase=name,
                command=name,
                message="release context missing — preflight did not run",
            )
        try:
            fn(ctx)
        except ReleaseError as exc:
            comment_phase_failed(ctx, name, exc)
            ctx.deferred_failures.append(name)
            raise
        except Exception as exc:
            wrapped = ReleaseError(
                phase=name,
                command=str(getattr(exc, "cmd", type(exc).__name__)),
                message=str(exc),
                detail=(getattr(exc, "stderr", None) or getattr(exc, "stdout", None)),
            )
            comment_phase_failed(ctx, name, wrapped)
            ctx.deferred_failures.append(name)
            raise wrapped from exc
        comment_phase_complete(ctx, name, _phase_details(ctx, name))
        names = _stage_names()
        ensure_checklist(ctx, names, checked=names[: names.index(name)])
        tick_stage(ctx, name)

    return stage


def _stage_names() -> list[str]:
    """The pipeline stage names, in execution order."""
    return [stage.name for stage in build_stages()]


def build_stages() -> list[Stage]:
    """The vrg-release pipeline, in execution order."""
    return [
        Stage("audit", _audit_stage, mode="fail_fast", skip_flag="skip_audit"),
        Stage("preflight", _preflight_stage, mode="fail_fast"),
        Stage("prepare", _tracked("prepare", prepare), mode="fail_fast"),
        Stage("merge-release", _tracked("merge-release", merge_release), mode="fail_fast"),
        Stage("confirm-main", _tracked("confirm-main", confirm_main), mode="fail_fast"),
        Stage(
            "back-merge-bump",
            _tracked("back-merge-bump", back_merge_and_bump),
            mode="fail_fast",
        ),
        Stage("teardown-worktree", _teardown_stage, mode="fail_defer"),
        Stage(
            "confirm-develop",
            _tracked("confirm-develop", confirm_develop),
            mode="fail_defer",
        ),
        Stage("promote", _tracked("promote", _promote_phase), mode="fail_defer"),
        Stage(
            "close-finalize",
            _tracked("close-finalize", close_and_finalize),
            mode="fail_defer",
        ),
        Stage(
            "consumer-refresh",
            _tracked("consumer-refresh", consumer_refresh),
            mode="fail_defer",
        ),
    ]


def merge_release(ctx: ReleaseContext) -> None:
    """Phase 2: merge the release PR.

    Resume-safe: if the PR is already merged (a prior run got this far), skip
    the merge and hydrate ``release_merge_sha`` rather than re-attempting it.
    """
    if ctx.release_pr_url is None:
        raise ReleaseError(
            phase="merge-release",
            command="merge_release",
            message=("release_pr_url is not set — prepare phase may not have run."),
        )
    if github.pr_state(ctx.release_pr_url) == "MERGED":
        print("Release PR already merged — skipping merge.")
        ctx.release_merge_sha = "merged"
        return
    wait_and_merge(ctx.release_pr_url, phase="merge-release")
    ctx.release_merge_sha = "merged"


def _promote_phase(ctx: ReleaseContext) -> None:
    """Phase 6: update the vX.Y rolling tag (unless --no-promote)."""
    if not ctx.promote:
        print("Skipping promote (--no-promote).")
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
