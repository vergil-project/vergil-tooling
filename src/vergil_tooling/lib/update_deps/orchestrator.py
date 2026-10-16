"""Declarative stage pipeline for vrg-update-deps.

Validation, merge and updater failures are recorded on the context rather than
raised (spec §5.2): the labelled PR stays open, a fix task is filed, cleanup
runs, and the ``status`` stage then surfaces the failure as a non-zero exit.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.update_deps import pr, report, validate
from vergil_tooling.lib.update_deps.context import DepsFailure, UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.failure import (
    combined_output,
    command_text,
    failure_summary,
    tail_output,
)
from vergil_tooling.lib.update_deps.preflight import preflight
from vergil_tooling.lib.update_deps.updater import (
    Updater,
    applicable_updaters,
    select_updaters,
)
from vergil_tooling.lib.update_deps.updaters.images import ImagesUpdater
from vergil_tooling.lib.update_deps.updaters.python_uv import PythonUvUpdater
from vergil_tooling.lib.update_deps.updaters.repo_hook import RepoHookUpdater
from vergil_tooling.lib.update_deps.updaters.vergil_eco import VergilUpdater

if TYPE_CHECKING:
    from pathlib import Path

# The repo hook is always last: it sees what the built-in updaters produced (spec §6.2).
DEFAULT_REGISTRY: list[Updater] = [
    PythonUvUpdater(),
    VergilUpdater(),
    ImagesUpdater(),
    RepoHookUpdater(),
]


@dataclass
class UpdateDepsState:
    """Pipeline state; ``ctx`` is populated by the preflight stage."""

    repo_root: Path
    ctx: UpdateDepsContext | None = None
    registry: list[Updater] = field(default_factory=lambda: list(DEFAULT_REGISTRY))
    only: list[str] | None = None
    skip: list[str] | None = None
    vergil_bump: str | None = None


def _require_ctx(state: UpdateDepsState) -> UpdateDepsContext:
    if state.ctx is None:
        raise UpdateDepsError(
            phase="update",
            command="update_deps",
            message="Pipeline context missing — preflight did not run.",
        )
    return state.ctx


def preflight_stage(state: UpdateDepsState) -> None:
    state.ctx = preflight(repo_root=state.repo_root)
    state.ctx.vergil_bump = state.vergil_bump


def run_updaters_stage(state: UpdateDepsState) -> None:
    """Run each applicable updater; record (don't raise) the first failure (spec §5.2).

    A failing updater stops the run: later updaters would build on a half-applied
    tree. Nothing is pushed for an ``update`` failure, so no PR is opened.
    """
    ctx = _require_ctx(state)
    selected = select_updaters(state.registry, only=state.only, skip=state.skip)
    for updater in applicable_updaters(ctx, registry=selected):
        try:
            result = updater.apply(ctx)
        except UpdateDepsError as exc:
            ctx.failure = DepsFailure(
                kind="update",
                message=f"{updater.name}: {exc}",
                output_tail=tail_output(exc.detail or ""),
                reproduce=exc.command,
            )
            return
        except subprocess.CalledProcessError as exc:
            ctx.failure = DepsFailure(
                kind="update",
                message=f"{updater.name}: {exc}",
                output_tail=tail_output(combined_output(exc)),
                reproduce=command_text(exc.cmd),
            )
            return
        ctx.results.append(result)
        if result.changed:
            ctx.any_changes = True
            git.run("add", "-A")
            git.run("commit", "-m", result.commit_message)


def validate_stage(state: UpdateDepsState) -> None:
    """Run the single validation; a red result is recorded, and the PR still opens."""
    ctx = _require_ctx(state)
    if not ctx.has_work:
        print("No dependency changes to validate — skipping validation.")
        return
    try:
        validate.run_validation()
    except UpdateDepsError as exc:
        ctx.failure = DepsFailure(
            kind="validation",
            message=str(exc),
            output_tail=tail_output(exc.detail or ""),
            reproduce=exc.command,
        )


def prepare_pr_stage(state: UpdateDepsState) -> None:
    """Open the labelled PR whenever there is work, including after a red validation."""
    ctx = _require_ctx(state)
    if ctx.has_work:
        pr.prepare_pr(ctx)


def merge_stage(state: UpdateDepsState) -> None:
    """Merge only a green update; a red one stays open for its fix task."""
    ctx = _require_ctx(state)
    if ctx.has_work and ctx.failure is None:
        pr.merge_pr(ctx)


def fix_task_stage(state: UpdateDepsState) -> None:
    """File the fix task for a validation or merge failure (an open PR exists)."""
    ctx = _require_ctx(state)
    if ctx.failure is not None and ctx.failure.kind != "update":
        pr.file_fix_task(ctx)


def finalize_stage(state: UpdateDepsState) -> None:
    """Print the held-back report (every run, spec §7.1) and remove the worktree."""
    ctx = _require_ctx(state)
    print("Held back by declared bounds:")
    print(report.render_markdown(report.collect(ctx.results)))
    if ctx.failure is not None:
        print(f"Dependency update did not land ({ctx.failure.kind}).")
    elif not ctx.any_changes:
        print("No updates found — removing worktree.")
    else:
        print(f"Dependency update merged: {ctx.pr_url}")
    pr.cleanup_worktree(ctx)


def status_stage(state: UpdateDepsState) -> None:
    """Surface a recorded failure as a non-zero exit, after cleanup has run."""
    ctx = _require_ctx(state)
    if ctx.failure is None:
        return
    raise UpdateDepsError(
        phase="status",
        command="vrg-update-deps",
        message=failure_summary(ctx),
        detail=ctx.failure.output_tail,
    )


def build_stages() -> list[Stage]:
    """The vrg-update-deps pipeline, in execution order."""
    return [
        Stage("preflight", preflight_stage, mode="fail_fast"),
        Stage("update", run_updaters_stage, mode="fail_fast"),
        Stage("validate", validate_stage, mode="fail_fast"),
        Stage("prepare-pr", prepare_pr_stage, mode="fail_fast"),
        Stage("merge", merge_stage, mode="fail_fast"),
        Stage("fix-task", fix_task_stage, mode="fail_fast"),
        Stage("finalize", finalize_stage, mode="fail_defer"),
        Stage("status", status_stage, mode="fail_defer"),
    ]


def run_update(*, repo_root: Path, registry: list[Updater] | None = None) -> UpdateDepsContext:
    """Run the pipeline as plain calls, for callers already inside a pipeline.

    vrg-release's update-deps stage uses this rather than nesting
    ``progress.run_pipeline``, which holds one module-global session. Cleanup
    always runs once preflight has created the worktree. A recorded failure is
    returned on the context, not raised: the caller decides how to surface it.
    Preflight errors and unexpected exceptions propagate.
    """
    state = UpdateDepsState(repo_root=repo_root)
    if registry is not None:
        state.registry = list(registry)
    preflight_stage(state)
    ctx = _require_ctx(state)
    try:
        for stage_fn in (
            run_updaters_stage,
            validate_stage,
            prepare_pr_stage,
            merge_stage,
            fix_task_stage,
        ):
            stage_fn(state)
    finally:
        finalize_stage(state)
    return ctx
