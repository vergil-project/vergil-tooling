"""Declarative stage list for the vrg-release pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib.process_errors import command_text, error_text
from vergil_tooling.lib.progress import Stage, mark_warn
from vergil_tooling.lib.promote import promote
from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.confirm import confirm_develop, confirm_main
from vergil_tooling.lib.release.conflict import merge_main_into_release
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.finalize import close_and_finalize, teardown_worktree
from vergil_tooling.lib.release.handoff import consumer_refresh
from vergil_tooling.lib.release.merge import (
    ReleaseConflictError,
    known_pr_state,
    sync_local_branch,
    wait_and_merge,
)
from vergil_tooling.lib.release.package_index import wait_for_index
from vergil_tooling.lib.release.preflight import preflight, run_audit
from vergil_tooling.lib.release.prepare import prepare
from vergil_tooling.lib.release.tracking import (
    comment_phase_complete,
    comment_phase_failed,
    ensure_checklist,
    publish_deferred_remedy,
    tick_stage,
)
from vergil_tooling.lib.release.update_deps_stage import update_deps

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext


# merge-release merges main into a conflicting release PR at most this many
# times per run, pausing after each push so GitHub recomputes mergeable (#3526).
_MAX_CONFLICT_HEALS = 3
_CONFLICT_SETTLE_SECS = 10


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
    without_deps: bool = False
    allow_pending_deps: bool = False


def _audit_stage(_state: ReleaseState) -> None:
    run_audit()


def _preflight_stage(state: ReleaseState) -> None:
    ctx = preflight(
        version_override=state.version_override,
        repo_root=state.repo_root,
        resume=state.resume,
        resume_version=state.resume_version,
        resume_issue_number=state.resume_issue_number,
        allow_pending_deps=state.allow_pending_deps,
    )
    ctx.promote = state.promote
    ctx.without_deps = state.without_deps
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
            _record_failure(ctx, name)
            raise
        except Exception as exc:
            wrapped = ReleaseError(
                phase=name,
                command=command_text(getattr(exc, "cmd", type(exc).__name__)),
                message=error_text(exc, output=False),
                detail=(getattr(exc, "stderr", None) or getattr(exc, "stdout", None)),
            )
            comment_phase_failed(ctx, name, wrapped)
            _record_failure(ctx, name)
            raise wrapped from exc
        comment_phase_complete(ctx, name, _phase_details(ctx, name))
        names = _stage_names()
        ensure_checklist(ctx, names, checked=names[: names.index(name)])
        tick_stage(ctx, name)

    return stage


def _record_failure(ctx: ReleaseContext, name: str) -> None:
    """Record a failed stage as halted, unless it deferred itself as a publish.

    A stage that recorded its own failure in ``deferred_publish_failures``
    (package-index) left the release complete, not halted: close-finalize must
    still post the deferral comment and leave the issue open, not treat it as
    a stage to fix and ``--resume`` (#3557).
    """
    if name not in ctx.deferred_publish_failures:
        ctx.deferred_failures.append(name)


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
        # Waits for the published package index before close-finalize, so a
        # miss (a deferred publish) leaves the tracking issue open with the
        # deferral comment (#3557); the consumer refresh then holds on it.
        Stage("package-index", _tracked("package-index", wait_for_index), mode="fail_defer"),
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
        # The first change on the new develop version, once the release is
        # fully out (epic vergil-project/.github#381, spec §5.1).
        Stage("update-deps", _tracked("update-deps", update_deps), mode="fail_defer"),
        Stage("publish-status", _publish_status_stage, mode="fail_defer"),
    ]


def merge_release(ctx: ReleaseContext) -> None:
    """Phase 2: merge the release PR.

    Resume-safe: if the PR is already merged (a prior run got this far), skip
    the merge and hydrate ``release_merge_sha`` rather than re-attempting it.

    A CONFLICTING release PR self-heals: main is merged into the release
    branch and pushed, a CHANGELOG-only conflict resolved automatically, and
    any other conflict refused with recovery steps (#3526, ``conflict``).
    """
    if ctx.release_pr_url is None:
        raise ReleaseError(
            phase="merge-release",
            command="merge_release",
            message=("release_pr_url is not set — prepare phase may not have run."),
        )
    # release_pr_url was just created or listed by prepare (#3137).
    if known_pr_state(ctx.release_pr_url) == "MERGED":
        print("Release PR already merged — skipping merge.")
        ctx.release_merge_sha = "merged"
        return
    updates: list[None] = []
    heals = 0
    while True:
        try:
            wait_and_merge(
                ctx.release_pr_url,
                phase="merge-release",
                on_branch_updated=lambda: updates.append(None),
            )
            break
        except ReleaseConflictError as exc:
            # main moved since the branch was cut: merge it in, resolving a
            # CHANGELOG-only conflict, and wait again (#3526). Bounded, since
            # main can keep moving and GitHub's mergeable can read stale.
            heals += 1
            if heals > _MAX_CONFLICT_HEALS:
                raise ReleaseError(
                    phase="merge-release",
                    command="merge_main_into_release",
                    message=(
                        f"{exc} The release PR still conflicts after merging main into "
                        f"it {_MAX_CONFLICT_HEALS} times; re-run: vrg-release --resume"
                    ),
                ) from exc
            _heal_conflict(ctx, ctx.release_pr_url)
            time.sleep(_CONFLICT_SETTLE_SECS)
    if updates:
        # GitHub added a merge commit the local release branch never saw;
        # bring the managed worktree up to what actually merged (#3175).
        _sync_release_branch(ctx, ctx.release_pr_url)
    ctx.release_merge_sha = "merged"


def _heal_conflict(ctx: ReleaseContext, pr_url: str) -> None:
    if ctx.release_branch is None or ctx.worktree_path is None:
        raise ReleaseError(
            phase="merge-release",
            command="merge_main_into_release",
            message=(
                "The release PR conflicts with main, but release_branch or "
                "worktree_path is not set, so main cannot be merged into it."
            ),
        )
    merge_main_into_release(
        pr_url, branch=ctx.release_branch, worktree=ctx.worktree_path, phase="merge-release"
    )


def _sync_release_branch(ctx: ReleaseContext, pr_url: str) -> None:
    if ctx.release_branch is None or ctx.worktree_path is None:
        raise ReleaseError(
            phase="merge-release",
            command="sync_local_branch",
            message=(
                "The release PR was updated on GitHub, but release_branch or "
                "worktree_path is not set, so the local release branch cannot be synced."
            ),
        )
    sync_local_branch(
        pr_url, branch=ctx.release_branch, worktree=ctx.worktree_path, phase="merge-release"
    )


def _promote_phase(ctx: ReleaseContext) -> None:
    """Phase 6: update the vX.Y rolling tag (unless --no-promote)."""
    if not ctx.promote:
        print("Skipping promote (--no-promote).")
        return
    promote(ctx.version)


def _publish_status_stage(state: ReleaseState) -> None:
    """Terminal fail_defer: surface deferred publish and dependency-update failures as exit 1.

    Runs last, after every bookkeeping stage. Raising here marks the run failed
    (fail_defer → exit 1 with the deferred summary) without aborting anything —
    the release is already complete. No-op when nothing deferred. A failed
    dependency update (spec §5.2) is repeated here so it is never lost in the
    stage list; it never affects the release itself.

    The update-deps stage has already failed (and counted) for a recorded deps
    failure, so on its own it is repeated as a warning on this row, not raised
    as a second failure (#3283): the summary counts one failure, and the
    warnings list still carries it. Alongside a deferred publish it rides in
    the raised message.
    """
    ctx = state.ctx
    if ctx is None:
        return
    if not ctx.deferred_publish_failures:
        if ctx.deps_failure:
            mark_warn(f"repeats the update-deps failure: {ctx.deps_failure}")
        return
    jobs = ", ".join(ctx.deferred_publish_failures)
    remedy = publish_deferred_remedy(ctx, ctx.deferred_publish_failures)
    problems = [
        f"Release v{ctx.version} is published (tag + GitHub Release), but "
        f"artifact publish was deferred ({jobs}). To finish delivery, {remedy} — "
        "do NOT use vrg-release --resume (the version is already tagged)."
    ]
    if ctx.deps_failure:
        problems.append(ctx.deps_failure)
    raise ReleaseError(
        phase="publish-status",
        command="publish-status",
        message=" ".join(problems),
    )


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
        if ctx.cleared_deps_prs:
            # #3286: closing a landed dependency-update PR is recorded, never silent.
            prs = ", ".join(ctx.cleared_deps_prs)
            lines.append(f"**Closed landed dependency-update PR(s)** (fix task completed): {prs}")
        if ctx.undeleted_deps_branches:
            # #3332: a closed PR's branch is never auto-deleted; a failed delete is named.
            branches = ", ".join(f"`{b}`" for b in ctx.undeleted_deps_branches)
            lines.append(
                f"**Could not delete the branch(es) of those closed PR(s)** — delete by "
                f"hand: {branches}"
            )
        if ctx.bypassed_deps_prs:
            # spec §5.3: bypassing the dependency-update gate is never silent.
            prs = ", ".join(ctx.bypassed_deps_prs)
            lines.append(
                f"**Bypassed pending dependency-update PR(s)** with `--allow-pending-deps`: {prs}"
            )
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
    elif phase == "package-index":
        if ctx.package_index_url:
            lines.append(f"Package visible in index: {ctx.package_index_url}")
        else:
            lines.append("No [package] block; no package index to wait for.")
    elif phase == "consumer-refresh":
        lines.append("Consumer refresh instructions displayed.")
    elif phase == "update-deps":
        if ctx.without_deps:
            lines.append("Dependency update skipped (--without-deps).")
        elif ctx.deps_landed_pr_url:
            lines.append(
                "The dependency update for this develop version had already merged: "
                f"{ctx.deps_landed_pr_url}. Nothing to update."
            )
        else:
            lines.append("**Held back by declared bounds**")
            lines.append("")
            lines.append(ctx.deps_report or "")
            lines.append("")
            if ctx.deps_pr_url:
                lines.append(f"Dependency-update PR: {ctx.deps_pr_url}")
            else:
                lines.append("No dependency changes; no PR opened.")
    return "\n".join(lines)
