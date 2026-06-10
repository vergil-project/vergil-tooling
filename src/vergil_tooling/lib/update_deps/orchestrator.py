"""Declarative stage pipeline for vrg-update-deps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.update_deps import pr, validate
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.preflight import preflight
from vergil_tooling.lib.update_deps.updater import Updater, applicable_updaters
from vergil_tooling.lib.update_deps.updaters.python_uv import PythonUvUpdater

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_REGISTRY: list[Updater] = [PythonUvUpdater()]


@dataclass
class UpdateDepsState:
    """Pipeline state; ``ctx`` is populated by the preflight stage."""

    repo_root: Path
    ctx: UpdateDepsContext | None = None
    registry: list[Updater] = field(default_factory=lambda: list(DEFAULT_REGISTRY))


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


def run_updaters_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    for updater in applicable_updaters(ctx, registry=state.registry):
        result = updater.apply(ctx)
        ctx.results.append(result)
        if result.changed:
            ctx.any_changes = True
            git.run("add", "-A")
            git.run("commit", "-m", result.commit_message)


def validate_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    if not ctx.any_changes:
        print("No dependency changes — skipping validation.")
        return
    validate.run_validation()


def prepare_pr_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    if not ctx.any_changes:
        return
    pr.prepare_pr(ctx)


def merge_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    if not ctx.any_changes:
        return
    pr.merge_pr(ctx)


def finalize_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    if not ctx.any_changes:
        print("No updates found — removing worktree.")
    else:
        print(f"Dependency update merged: {ctx.pr_url}")
    pr.cleanup_worktree(ctx)


def build_stages() -> list[Stage]:
    """The vrg-update-deps pipeline, in execution order."""
    return [
        Stage("preflight", preflight_stage, mode="fail_fast"),
        Stage("update", run_updaters_stage, mode="fail_fast"),
        Stage("validate", validate_stage, mode="fail_fast"),
        Stage("prepare-pr", prepare_pr_stage, mode="fail_fast"),
        Stage("merge", merge_stage, mode="fail_fast"),
        Stage("finalize", finalize_stage, mode="fail_defer"),
    ]
