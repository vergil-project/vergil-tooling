from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.orchestrator import (
    UpdateDepsState,
    build_stages,
    finalize_stage,
    merge_stage,
    preflight_stage,
    prepare_pr_stage,
    run_updaters_stage,
    validate_stage,
)
from vergil_tooling.lib.update_deps.updater import UpdateResult


class _Changed:
    name = "changed"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:  # noqa: ARG002
        return UpdateResult(
            updater="changed", changed=True, summary="s", commit_message="chore(deps): s"
        )


class _NoChange:
    name = "nochange"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:  # noqa: ARG002
        return UpdateResult(updater="nochange", changed=False, summary="", commit_message="")


_MOD = "vergil_tooling.lib.update_deps.orchestrator"


def _state() -> tuple[UpdateDepsState, UpdateDepsContext]:
    state = UpdateDepsState(repo_root=Path("/tmp/r"))  # noqa: S108
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    ctx.branch = "chore/dep-update-20260610"
    ctx.worktree_path = Path("/tmp/r/.worktrees/chore-dep-update-20260610")  # noqa: S108
    state.ctx = ctx
    return state, ctx


def test_require_ctx_raises_when_missing() -> None:
    state = UpdateDepsState(repo_root=Path("/tmp/r"))  # noqa: S108
    with pytest.raises(UpdateDepsError, match="context missing"):
        run_updaters_stage(state)


def test_preflight_stage_populates_ctx(monkeypatch) -> None:
    sentinel = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(_MOD + ".preflight", lambda *, repo_root: sentinel)  # noqa: ARG005
    state = UpdateDepsState(repo_root=Path("/tmp/r"))  # noqa: S108
    preflight_stage(state)
    assert state.ctx is sentinel


def test_run_updaters_commits_changed_only(monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    state, ctx = _state()
    state.registry = [_Changed(), _NoChange()]
    run_updaters_stage(state)
    assert ctx.any_changes is True
    assert ("commit", "-m", "chore(deps): s") in runs
    assert sum(1 for r in runs if r[0] == "commit") == 1


def test_run_updaters_no_changes_sets_flag_false(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    state, ctx = _state()
    state.registry = [_NoChange()]
    run_updaters_stage(state)
    assert ctx.any_changes is False


def test_run_updaters_honors_skip(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    state, ctx = _state()
    state.registry = [_Changed(), _NoChange()]
    state.skip = ["changed"]
    run_updaters_stage(state)
    assert [r.updater for r in ctx.results] == ["nochange"]
    assert ctx.any_changes is False


def test_run_updaters_honors_only(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    state, ctx = _state()
    state.registry = [_Changed(), _NoChange()]
    state.only = ["changed"]
    run_updaters_stage(state)
    assert [r.updater for r in ctx.results] == ["changed"]
    assert ctx.any_changes is True


def test_validate_stage_skips_when_no_changes(monkeypatch) -> None:
    called = {"ran": False}
    monkeypatch.setattr(_MOD + ".validate.run_validation", lambda: called.update(ran=True))
    state, ctx = _state()
    ctx.any_changes = False
    validate_stage(state)
    assert called["ran"] is False


def test_validate_stage_aborts_on_red(monkeypatch) -> None:
    def _boom() -> None:
        raise UpdateDepsError(
            phase="validate", command="vrg-validate", message="Validation failed."
        )

    monkeypatch.setattr(_MOD + ".validate.run_validation", _boom)
    state, ctx = _state()
    ctx.any_changes = True
    with pytest.raises(UpdateDepsError, match="Validation failed"):
        validate_stage(state)


def test_prepare_pr_stage_runs_only_on_changes(monkeypatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(_MOD + ".pr.prepare_pr", lambda ctx: calls.update(n=calls["n"] + 1))  # noqa: ARG005
    state, ctx = _state()
    prepare_pr_stage(state)  # no changes -> skip
    ctx.any_changes = True
    prepare_pr_stage(state)  # changes -> run
    assert calls["n"] == 1


def test_merge_stage_runs_only_on_changes(monkeypatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(_MOD + ".pr.merge_pr", lambda ctx: calls.update(n=calls["n"] + 1))  # noqa: ARG005
    state, ctx = _state()
    merge_stage(state)  # no changes -> skip
    ctx.any_changes = True
    merge_stage(state)  # changes -> run
    assert calls["n"] == 1


def test_finalize_removes_worktree_on_noop(monkeypatch) -> None:
    cleaned = {"done": False}
    monkeypatch.setattr(_MOD + ".pr.cleanup_worktree", lambda ctx: cleaned.update(done=True))  # noqa: ARG005
    state, ctx = _state()
    ctx.any_changes = False
    finalize_stage(state)
    assert cleaned["done"] is True


def test_finalize_removes_worktree_on_success(monkeypatch) -> None:
    cleaned = {"done": False}
    monkeypatch.setattr(_MOD + ".pr.cleanup_worktree", lambda ctx: cleaned.update(done=True))  # noqa: ARG005
    state, ctx = _state()
    ctx.any_changes = True
    ctx.pr_url = "https://x/pr/1"
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
