from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import (
    TAIL_HEADING,
    DepsFailure,
    FailureKind,
    UpdateDepsContext,
    UpdateDepsError,
)
from vergil_tooling.lib.update_deps.orchestrator import (
    DEFAULT_REGISTRY,
    UpdateDepsState,
    build_stages,
    finalize_stage,
    fix_task_stage,
    merge_stage,
    preflight_stage,
    prepare_pr_stage,
    run_update,
    run_updaters_stage,
    status_stage,
    validate_stage,
)
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updater import Updater, UpdateResult


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


def test_default_registry_names() -> None:
    assert [u.name for u in DEFAULT_REGISTRY] == ["python", "vergil", "images", "hook"]


def test_require_ctx_raises_when_missing() -> None:
    state = UpdateDepsState(repo_root=Path("/tmp/r"))  # noqa: S108
    with pytest.raises(UpdateDepsError, match="context missing"):
        run_updaters_stage(state)


def test_preflight_stage_populates_ctx_and_bump(monkeypatch) -> None:
    sentinel = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(_MOD + ".preflight", lambda *, repo_root: sentinel)  # noqa: ARG005
    state = UpdateDepsState(repo_root=Path("/tmp/r"), vergil_bump="v2.2")  # noqa: S108
    preflight_stage(state)
    assert state.ctx is sentinel
    assert sentinel.vergil_bump == "v2.2"


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


def test_build_stages_names_and_modes() -> None:
    assert [(s.name, s.mode) for s in build_stages()] == [
        ("preflight", "fail_fast"),
        ("update", "fail_fast"),
        ("validate", "fail_fast"),
        ("prepare-pr", "fail_fast"),
        ("merge", "fail_fast"),
        ("fix-task", "fail_fast"),
        ("finalize", "fail_defer"),
        ("status", "fail_defer"),
    ]


class _Boom:
    name = "boom"

    def applies(self, ctx: UpdateDepsContext) -> bool:  # noqa: ARG002
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:  # noqa: ARG002
        raise UpdateDepsError(
            phase="update", command="hook-cmd", message="hook failed", detail="trace"
        )


class _ProcBoom:
    name = "procboom"

    def applies(self, ctx: UpdateDepsContext) -> bool:  # noqa: ARG002
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:  # noqa: ARG002
        raise subprocess.CalledProcessError(
            1, ("uv", "lock", "--upgrade"), output="resolving", stderr="no solution"
        )


def _failure(kind: FailureKind) -> DepsFailure:
    return DepsFailure(kind=kind, message="m", output_tail="t", reproduce="r")


def test_updater_error_is_recorded_and_stops_updaters(monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    state, ctx = _state()
    state.registry = [_Changed(), _Boom(), _NoChange()]
    run_updaters_stage(state)
    assert ctx.failure is not None
    assert ctx.failure.kind == "update"
    assert ctx.failure.message == "boom: hook failed"
    assert ctx.failure.output_tail == "trace"
    assert ctx.failure.reproduce == "hook-cmd"
    assert [r.updater for r in ctx.results] == ["changed"]
    assert ctx.any_changes is True
    assert ctx.has_work is False


def test_updater_error_without_detail(monkeypatch) -> None:
    class _Bare(_Boom):
        def apply(self, ctx: UpdateDepsContext) -> UpdateResult:  # noqa: ARG002
            raise UpdateDepsError(phase="update", command="c", message="bare")

    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    state, ctx = _state()
    state.registry = [_Bare()]
    run_updaters_stage(state)
    assert ctx.failure is not None
    assert ctx.failure.output_tail == ""
    assert ctx.failure.output_heading == TAIL_HEADING


def test_updater_process_error_is_recorded(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    state, ctx = _state()
    state.registry = [_ProcBoom(), _Changed()]
    run_updaters_stage(state)
    assert ctx.failure is not None
    assert ctx.failure.kind == "update"
    assert ctx.failure.message.startswith("procboom: ")
    assert ctx.failure.output_tail == "resolving\nno solution"
    assert ctx.failure.reproduce == "uv lock --upgrade"
    assert ctx.results == []


def test_validation_failure_is_recorded_not_raised(monkeypatch) -> None:
    def _red() -> None:
        raise UpdateDepsError(
            phase="validate",
            command="vrg-container-run -- vrg-validate",
            message="Validation failed.",
            detail="E assert 1 == 2",
            detail_heading="Output of the failing vrg-validate stage (test):",
        )

    monkeypatch.setattr(_MOD + ".validate.run_validation", _red)
    state, ctx = _state()
    ctx.any_changes = True
    validate_stage(state)
    assert ctx.failure is not None
    assert ctx.failure.kind == "validation"
    assert ctx.failure.message == "Validation failed."
    assert "E assert 1 == 2" in ctx.failure.output_tail
    assert ctx.failure.output_heading == "Output of the failing vrg-validate stage (test):"
    assert ctx.failure.reproduce == "vrg-container-run -- vrg-validate"


def test_validation_failure_without_detail(monkeypatch) -> None:
    def _red() -> None:
        raise UpdateDepsError(phase="validate", command="v", message="Validation failed.")

    monkeypatch.setattr(_MOD + ".validate.run_validation", _red)
    state, ctx = _state()
    ctx.any_changes = True
    validate_stage(state)
    assert ctx.failure is not None
    assert ctx.failure.output_tail == ""
    assert ctx.failure.output_heading == TAIL_HEADING


def test_validate_stage_runs_on_green(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_MOD + ".validate.run_validation", lambda: calls.append("v"))
    state, ctx = _state()
    ctx.any_changes = True
    validate_stage(state)
    assert calls == ["v"]
    assert ctx.failure is None


def test_validation_failure_still_opens_pr_but_skips_merge(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_MOD + ".pr.prepare_pr", lambda ctx: calls.append("prepare"))  # noqa: ARG005
    monkeypatch.setattr(_MOD + ".pr.merge_pr", lambda ctx: calls.append("merge"))  # noqa: ARG005
    monkeypatch.setattr(_MOD + ".pr.file_fix_task", lambda ctx: calls.append("fix"))  # noqa: ARG005
    state, ctx = _state()
    ctx.any_changes = True
    ctx.failure = _failure("validation")
    prepare_pr_stage(state)
    merge_stage(state)
    fix_task_stage(state)
    assert calls == ["prepare", "fix"]


def test_merge_failure_files_fix_task(monkeypatch) -> None:
    calls: list[str] = []

    def _merge(ctx: UpdateDepsContext) -> None:
        calls.append("merge")
        ctx.failure = _failure("merge")

    monkeypatch.setattr(_MOD + ".pr.merge_pr", _merge)
    monkeypatch.setattr(_MOD + ".pr.file_fix_task", lambda ctx: calls.append("fix"))  # noqa: ARG005
    state, ctx = _state()
    ctx.any_changes = True
    merge_stage(state)
    fix_task_stage(state)
    assert calls == ["merge", "fix"]


def test_fix_task_stage_noop_on_success(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_MOD + ".pr.file_fix_task", lambda ctx: calls.append("fix"))  # noqa: ARG005
    state, ctx = _state()
    ctx.any_changes = True
    fix_task_stage(state)
    assert calls == []


def test_update_failure_opens_no_pr_and_files_no_task(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(_MOD + ".pr.prepare_pr", lambda ctx: calls.append("prepare"))  # noqa: ARG005
    monkeypatch.setattr(_MOD + ".pr.merge_pr", lambda ctx: calls.append("merge"))  # noqa: ARG005
    monkeypatch.setattr(_MOD + ".pr.file_fix_task", lambda ctx: calls.append("fix"))  # noqa: ARG005
    monkeypatch.setattr(_MOD + ".validate.run_validation", lambda: calls.append("validate"))
    state, ctx = _state()
    ctx.any_changes = True
    ctx.failure = _failure("update")
    validate_stage(state)
    prepare_pr_stage(state)
    merge_stage(state)
    fix_task_stage(state)
    assert calls == []


def test_finalize_prints_report_on_noop(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_MOD + ".pr.cleanup_worktree", lambda ctx: None)  # noqa: ARG005
    state, ctx = _state()
    ctx.results = [
        UpdateResult(
            updater="python",
            changed=False,
            summary="",
            commit_message="",
            held_back=[HeldBack(source="python", name="p", current="1", latest="2")],
        )
    ]
    finalize_stage(state)
    out = capsys.readouterr().out
    assert "Held back by declared bounds:" in out
    assert "| python | p | 1 | 2 |" in out
    assert "No updates found" in out


def test_finalize_prints_empty_report(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_MOD + ".pr.cleanup_worktree", lambda ctx: None)  # noqa: ARG005
    state, _ = _state()
    finalize_stage(state)
    assert "No dependencies are held back" in capsys.readouterr().out


def test_finalize_reports_failure_and_still_cleans_up(monkeypatch, capsys) -> None:
    cleaned: list[bool] = []
    monkeypatch.setattr(_MOD + ".pr.cleanup_worktree", lambda ctx: cleaned.append(True))  # noqa: ARG005
    state, ctx = _state()
    ctx.any_changes = True
    ctx.failure = _failure("validation")
    finalize_stage(state)
    out = capsys.readouterr().out
    assert "did not land (validation)" in out
    assert "merged" not in out
    assert cleaned == [True]


def test_status_stage_raises_on_recorded_failure() -> None:
    state, ctx = _state()
    ctx.version = "2.1.243"
    ctx.failure = DepsFailure(kind="update", message="hook failed", output_tail="t", reproduce="r")
    with pytest.raises(UpdateDepsError, match=r"2\.1\.243") as info:
        status_stage(state)
    assert info.value.phase == "status"
    assert info.value.detail == "t"


def test_status_stage_quiet_on_success() -> None:
    state, _ = _state()
    status_stage(state)  # no raise


def test_run_update_runs_stages_in_order_and_always_cleans_up(monkeypatch) -> None:
    order: list[str] = []
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(_MOD + ".preflight", lambda *, repo_root: ctx)  # noqa: ARG005
    for name in (
        "run_updaters_stage",
        "validate_stage",
        "prepare_pr_stage",
        "merge_stage",
        "fix_task_stage",
    ):
        monkeypatch.setattr(f"{_MOD}.{name}", lambda s, n=name: order.append(n))  # noqa: ARG005
    monkeypatch.setattr(_MOD + ".finalize_stage", lambda s: order.append("finalize"))  # noqa: ARG005
    assert run_update(repo_root=Path("/tmp/r")) is ctx  # noqa: S108
    assert order == [
        "run_updaters_stage",
        "validate_stage",
        "prepare_pr_stage",
        "merge_stage",
        "fix_task_stage",
        "finalize",
    ]


def test_run_update_uses_given_registry(monkeypatch) -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    seen: list[list[object]] = []
    monkeypatch.setattr(_MOD + ".preflight", lambda *, repo_root: ctx)  # noqa: ARG005
    monkeypatch.setattr(_MOD + ".run_updaters_stage", lambda s: seen.append(list(s.registry)))
    for name in ("validate_stage", "prepare_pr_stage", "merge_stage", "fix_task_stage"):
        monkeypatch.setattr(f"{_MOD}.{name}", lambda s: None)  # noqa: ARG005
    monkeypatch.setattr(_MOD + ".finalize_stage", lambda s: None)  # noqa: ARG005
    registry: list[Updater] = [_NoChange()]
    run_update(repo_root=Path("/tmp/r"), registry=registry)  # noqa: S108
    assert seen == [registry]
    run_update(repo_root=Path("/tmp/r"))  # noqa: S108
    assert seen[1] == list(DEFAULT_REGISTRY)


def test_run_update_returns_recorded_failure(monkeypatch) -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(_MOD + ".preflight", lambda *, repo_root: ctx)  # noqa: ARG005

    def _fail(state: UpdateDepsState) -> None:
        assert state.ctx is not None
        state.ctx.failure = _failure("update")

    monkeypatch.setattr(_MOD + ".run_updaters_stage", _fail)
    monkeypatch.setattr(_MOD + ".pr.cleanup_worktree", lambda c: None)  # noqa: ARG005
    result = run_update(repo_root=Path("/tmp/r"))  # noqa: S108
    assert result.failure is not None
    assert result.failure.kind == "update"


def test_run_update_cleans_up_when_a_stage_raises(monkeypatch) -> None:
    order: list[str] = []
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(_MOD + ".preflight", lambda *, repo_root: ctx)  # noqa: ARG005

    def boom(state: UpdateDepsState) -> None:  # noqa: ARG001
        msg = "git commit failed"
        raise RuntimeError(msg)

    monkeypatch.setattr(_MOD + ".run_updaters_stage", boom)
    monkeypatch.setattr(_MOD + ".finalize_stage", lambda s: order.append("finalize"))  # noqa: ARG005
    with pytest.raises(RuntimeError, match="git commit failed"):
        run_update(repo_root=Path("/tmp/r"))  # noqa: S108
    assert order == ["finalize"]


def test_run_update_preflight_error_propagates_without_cleanup(monkeypatch) -> None:
    cleaned: list[bool] = []

    def _bad(*, repo_root: Path) -> UpdateDepsContext:  # noqa: ARG001
        raise UpdateDepsError(phase="preflight", command="c", message="dirty")

    monkeypatch.setattr(_MOD + ".preflight", _bad)
    monkeypatch.setattr(_MOD + ".finalize_stage", lambda s: cleaned.append(True))  # noqa: ARG005
    with pytest.raises(UpdateDepsError, match="dirty"):
        run_update(repo_root=Path("/tmp/r"))  # noqa: S108
    assert cleaned == []
