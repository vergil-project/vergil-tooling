from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.orchestrator import (
    UpdateDepsState,
    build_stages,
    finalize_stage,
    run_updaters_stage,
    validate_stage,
)
from vergil_tooling.lib.update_deps.updater import UpdateResult

_MOD = "vergil_tooling.lib.update_deps.orchestrator"


class _Changed:
    name = "changed"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        return UpdateResult(
            updater="changed", changed=True, summary="s", commit_message="chore(deps): s"
        )


class _NoChange:
    name = "nochange"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        return UpdateResult(updater="nochange", changed=False, summary="", commit_message="")


def _state() -> UpdateDepsState:
    state = UpdateDepsState(repo_root=Path("/tmp/r"))  # noqa: S108
    state.ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    state.ctx.branch = "chore/dep-update-20260610"
    state.ctx.worktree_path = Path("/tmp/r/.worktrees/chore-dep-update-20260610")  # noqa: S108
    return state


def test_run_updaters_commits_changed_only(monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    state = _state()
    state.registry = [_Changed(), _NoChange()]
    run_updaters_stage(state)
    assert state.ctx.any_changes is True
    assert ("commit", "-m", "chore(deps): s") in runs
    assert sum(1 for r in runs if r[0] == "commit") == 1


def test_run_updaters_no_changes_sets_flag_false(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    state = _state()
    state.registry = [_NoChange()]
    run_updaters_stage(state)
    assert state.ctx.any_changes is False


def test_validate_stage_skips_when_no_changes(monkeypatch) -> None:
    called = {"ran": False}
    monkeypatch.setattr(_MOD + ".validate.run_validation", lambda: called.update(ran=True))
    state = _state()
    state.ctx.any_changes = False
    validate_stage(state)
    assert called["ran"] is False


def test_validate_stage_aborts_on_red(monkeypatch) -> None:
    def _boom() -> None:
        raise UpdateDepsError(
            phase="validate", command="vrg-validate", message="Validation failed."
        )

    monkeypatch.setattr(_MOD + ".validate.run_validation", _boom)
    state = _state()
    state.ctx.any_changes = True
    with pytest.raises(UpdateDepsError, match="Validation failed"):
        validate_stage(state)


def test_finalize_removes_worktree_on_noop(monkeypatch) -> None:
    cleaned = {"done": False}
    monkeypatch.setattr(_MOD + ".pr.cleanup_worktree", lambda ctx: cleaned.update(done=True))
    state = _state()
    state.ctx.any_changes = False
    finalize_stage(state)
    assert cleaned["done"] is True


def test_build_stages_order_and_modes() -> None:
    stages = build_stages()
    assert [s.name for s in stages] == [
        "preflight",
        "update",
        "validate",
        "prepare-pr",
        "merge",
        "finalize",
    ]
    modes = {s.name: s.mode for s in stages}
    assert modes["validate"] == "fail_fast"
    assert modes["finalize"] == "fail_defer"
