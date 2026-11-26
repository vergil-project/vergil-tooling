"""Tests for the vrg-release ``update-deps`` stage (epic vergil-project/.github#381)."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_path import ArmResult
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.handoff import holds
from vergil_tooling.lib.release.orchestrator import ReleaseState, _preflight_stage, build_stages
from vergil_tooling.lib.release.resume import first_undone
from vergil_tooling.lib.release.state import StateBlock, initial_state
from vergil_tooling.lib.release.update_deps_stage import update_deps
from vergil_tooling.lib.update_deps.context import DepsFailure, UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.report import EMPTY_REPORT, HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

_MOD = "vergil_tooling.lib.release.update_deps_stage"


def _rctx() -> ReleaseContext:
    return ReleaseContext(
        repo="o/r",
        version="2.1.242",
        repo_root=Path("/r"),
        version_override=None,
    )


def _deps(
    *,
    pr_url: str | None = None,
    fix_issue_url: str | None = None,
    any_changes: bool = False,
    held_back: bool = True,
) -> UpdateDepsContext:
    d = UpdateDepsContext(repo="o/r", repo_root=Path("/r"), version="2.1.243")
    entries = [HeldBack(source="python", name="p", current="1", latest="2")] if held_back else []
    d.results = [
        UpdateResult(
            updater="python",
            changed=any_changes,
            summary="s",
            commit_message="c",
            held_back=entries,
        )
    ]
    d.pr_url = pr_url
    d.fix_issue_url = fix_issue_url
    d.any_changes = any_changes
    return d


class _Recorder:
    def __init__(self) -> None:
        self.gits: list[tuple[str, ...]] = []
        self.chdirs: list[Path] = []
        self.run_update_roots: list[Path] = []
        self.run_update_fast_forwards: list[bool] = []
        # Every lock, branch, git and run_update event, in order.
        self.events: list[str] = []


def _patch_lock(monkeypatch: pytest.MonkeyPatch, rec: _Recorder) -> None:
    @contextlib.contextmanager
    def fake_lock(main_root: Path, *, purpose: str) -> Generator[None]:
        rec.events.append(f"lock {main_root} {purpose}")
        try:
            yield
        finally:
            rec.events.append("unlock")

    monkeypatch.setattr(_MOD + ".checkout_lock", fake_lock)


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    deps: UpdateDepsContext | BaseException,
    *,
    branch: str = "develop",
) -> _Recorder:
    rec = _Recorder()
    _patch_lock(monkeypatch, rec)
    monkeypatch.setattr(_MOD + ".os.chdir", rec.chdirs.append)

    def fake_branch() -> str:
        rec.events.append("current-branch")
        return branch

    def fake_git(*a: str) -> None:
        rec.events.append(f"git {a[0]}")
        rec.gits.append(a)

    monkeypatch.setattr(_MOD + ".git.current_branch", fake_branch)
    monkeypatch.setattr(_MOD + ".git.run", fake_git)

    def fake_run_update(*, repo_root: Path, fast_forward: bool) -> UpdateDepsContext:
        rec.events.append("run_update")
        rec.run_update_roots.append(repo_root)
        rec.run_update_fast_forwards.append(fast_forward)
        if isinstance(deps, BaseException):
            raise deps
        return deps

    monkeypatch.setattr(_MOD + ".run_update", fake_run_update)
    return rec


def test_update_deps_fast_forward_takes_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The branch check and the fast-forward hold the lock; run_update runs after it."""
    rec = _patch(monkeypatch, _deps(pr_url="https://x/pull/9", any_changes=True))
    update_deps(_rctx())
    assert rec.events == [
        "lock /r vrg-release update-deps",
        "current-branch",
        "git fetch",
        "git merge",
        "unlock",
        "run_update",
    ]


def test_update_deps_lock_released_on_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _patch(monkeypatch, AssertionError("must not run"), branch="feature/1-x")
    with pytest.raises(ReleaseError):
        update_deps(_rctx())
    assert rec.events == ["lock /r vrg-release update-deps", "current-branch", "unlock"]


def test_without_deps_skips_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _patch(monkeypatch, AssertionError("must not run"))
    ctx = _rctx()
    ctx.without_deps = True
    update_deps(ctx)
    assert rec.gits == []
    assert rec.chdirs == []
    assert rec.run_update_roots == []
    assert ctx.deps_report == "Skipped (--without-deps)."
    assert ctx.deps_pr_url is None
    assert ctx.deps_failure is None


def test_syncs_root_develop_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _patch(monkeypatch, _deps(pr_url="https://x/pull/9", any_changes=True))
    update_deps(_rctx())
    assert rec.chdirs == [Path("/r")]
    assert rec.gits == [("fetch", "origin", "develop"), ("merge", "--ff-only", "origin/develop")]
    assert rec.run_update_roots == [Path("/r")]
    assert rec.run_update_fast_forwards == [True]


def test_refuses_when_root_checkout_not_on_develop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fast-forward merge of origin/develop into any other branch would move it."""
    rec = _patch(monkeypatch, AssertionError("must not run"), branch="feature/1-x")
    with pytest.raises(ReleaseError, match="feature/1-x") as info:
        update_deps(_rctx())
    assert info.value.phase == "update-deps"
    assert rec.gits == []
    assert rec.run_update_roots == []


def test_success_records_report_and_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _deps(pr_url="https://x/pull/9", any_changes=True))
    ctx = _rctx()
    update_deps(ctx)
    assert "| python | p | 1 | 2 |" in (ctx.deps_report or "")
    assert ctx.deps_pr_url == "https://x/pull/9"
    assert ctx.deps_fix_issue_url is None
    assert ctx.deps_failure is None


class _Changed:
    name = "changed"

    def applies(self, ctx: UpdateDepsContext) -> bool:  # noqa: ARG002
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:  # noqa: ARG002
        return UpdateResult(
            updater="changed", changed=True, summary="s", commit_message="chore(deps): s"
        )


def test_release_deps_stage_uses_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stage's run_update arms its PR through the one PR path and only waits on it."""
    rec = _Recorder()
    _patch_lock(monkeypatch, rec)
    monkeypatch.setattr(_MOD + ".os.chdir", rec.chdirs.append)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)  # noqa: ARG005
    orch = "vergil_tooling.lib.update_deps.orchestrator"
    deps_pr = "vergil_tooling.lib.update_deps.pr"
    deps = UpdateDepsContext(
        repo="o/r", repo_root=Path("/r"), version="2.1.243", branch="deps/update-2.1.243"
    )

    def fake_preflight(*, repo_root: Path, fast_forward: bool) -> UpdateDepsContext:  # noqa: ARG001
        return deps

    monkeypatch.setattr(orch + ".preflight", fake_preflight)
    monkeypatch.setattr(orch + ".DEFAULT_REGISTRY", [_Changed()])
    monkeypatch.setattr(orch + ".git.run", lambda *a: None)  # noqa: ARG005
    monkeypatch.setattr(orch + ".validate.run_validation", lambda: None)
    monkeypatch.setattr(
        orch + ".pr.prepare_pr", lambda c: setattr(c, "pr_url", "https://github.com/o/r/pull/9")
    )
    monkeypatch.setattr(orch + ".pr.cleanup_worktree", lambda c: None)  # noqa: ARG005
    monkeypatch.setattr(orch + ".pr.delete_merged_branch", lambda c: None)  # noqa: ARG005
    monkeypatch.setattr(deps_pr + ".git.read_output", lambda *a: "a" * 40)  # noqa: ARG005
    armed: list[tuple[str, str | None]] = []

    def fake_arm(url: str, *, clone: Path | None = None, head: str | None = None) -> ArmResult:
        armed.append((url, head))
        return ArmResult(outcome="armed", pr_url=url, head="a" * 40, method="MERGE")

    monkeypatch.setattr(deps_pr + ".pr_path.arm", fake_arm)
    waited: list[str] = []
    monkeypatch.setattr(
        deps_pr + ".pr_merge.wait_until_merged",
        lambda url, *, strategy, deadline_secs: waited.append(url),  # noqa: ARG005
    )

    def no_merge(*a: object, **k: object) -> None:
        pytest.fail(f"merged directly: {a} {k}")

    monkeypatch.setattr(deps_pr + ".pr_merge.wait_and_merge", no_merge)
    ctx = _rctx()
    update_deps(ctx)
    assert armed == [("https://github.com/o/r/pull/9", "a" * 40)]
    assert waited == ["https://github.com/o/r/pull/9"]
    assert deps.merged is True
    assert ctx.deps_pr_url == "https://github.com/o/r/pull/9"
    assert ctx.deps_failure is None


def test_already_merged_update_is_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """On --resume after the update for this version merged, the stage succeeds (#3330)."""
    deps = UpdateDepsContext(
        repo="o/r",
        repo_root=Path("/r"),
        version="2.1.243",
        landed_pr_url="https://github.com/o/r/pull/7",
    )
    _patch(monkeypatch, deps)
    ctx = _rctx()
    update_deps(ctx)  # no raise
    assert ctx.deps_landed_pr_url == "https://github.com/o/r/pull/7"
    assert ctx.deps_pr_url == "https://github.com/o/r/pull/7"
    assert ctx.deps_failure is None
    assert ctx.deps_report is None


def test_noop_records_report_without_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _deps(held_back=False))
    ctx = _rctx()
    update_deps(ctx)
    assert ctx.deps_pr_url is None
    assert ctx.deps_report == EMPTY_REPORT
    assert ctx.deps_failure is None


def test_failure_raises_release_error_with_report_and_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = _deps(pr_url="https://x/pull/9", fix_issue_url="https://x/issues/10", any_changes=True)
    d.failure = DepsFailure(
        kind="validation",
        message="Validation failed.",
        output_tail="E boom",
        reproduce="vrg-container-run -- vrg-validate",
    )
    _patch(monkeypatch, d)
    ctx = _rctx()
    with pytest.raises(ReleaseError, match="2.1.243") as info:
        update_deps(ctx)
    assert info.value.phase == "update-deps"
    assert info.value.command == "vrg-update-deps"
    detail = info.value.detail or ""
    assert "E boom" in detail
    assert "| python | p |" in detail
    # The tracking comment renders "**Detail:** <detail>": a leading blank line
    # keeps the Markdown table from starting mid-line.
    assert detail.startswith("\n\n")
    assert ctx.deps_pr_url == "https://x/pull/9"
    assert ctx.deps_fix_issue_url == "https://x/issues/10"
    assert ctx.deps_failure is not None
    assert "issues/10" in ctx.deps_failure
    assert str(info.value) == ctx.deps_failure


def test_update_failure_records_no_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    d = _deps(fix_issue_url="https://x/issues/11")
    d.failure = DepsFailure(
        kind="update", message="python: boom", output_tail="", reproduce="uv lock"
    )
    _patch(monkeypatch, d)
    ctx = _rctx()
    with pytest.raises(ReleaseError, match="No PR was opened") as info:
        update_deps(ctx)
    assert ctx.deps_pr_url is None
    assert ctx.deps_fix_issue_url == "https://x/issues/11"
    assert ctx.deps_failure is not None
    # The tracking issue names the standalone fix task (#3550).
    assert "Fix task: https://x/issues/11" in ctx.deps_failure
    assert "Reproduce on `develop`: `uv lock`" in str(info.value.detail)


def test_sync_failure_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder()
    _patch_lock(monkeypatch, rec)
    monkeypatch.setattr(_MOD + ".os.chdir", lambda _p: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")

    def boom(*_a: str) -> None:
        msg = "not a fast-forward"
        raise RuntimeError(msg)

    monkeypatch.setattr(_MOD + ".git.run", boom)
    with pytest.raises(RuntimeError, match="fast-forward"):
        update_deps(_rctx())
    assert rec.events == ["lock /r vrg-release update-deps", "unlock"]


def test_preflight_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """An update-deps preflight refusal (e.g. an in-flight branch) is raised, not hidden."""
    exc = UpdateDepsError(phase="preflight", command="git worktree add", message="in flight")
    _patch(monkeypatch, exc)
    ctx = _rctx()
    with pytest.raises(UpdateDepsError, match="in flight"):
        update_deps(ctx)
    assert ctx.deps_report is None


# --- resumable through the state block (spec §4.5 of epic vergil-project/.github#1440) ---

_ORCH = "vergil_tooling.lib.release.orchestrator"
_BEFORE_DEPS = (
    "prepare",
    "merge-release",
    "confirm-main",
    "back-merge-bump",
    "teardown-worktree",
    "confirm-develop",
    "cd-publish",
    "package-index",
    "promote",
    "finalize",
)


def _block(*, done: tuple[str, ...] = (), without_deps: bool = False) -> StateBlock:
    initial = initial_state(version="2.1.242", cut="c" * 40, issues=(), without_deps=without_deps)
    return replace(initial, done=done)


@pytest.fixture
def written() -> Iterator[list[StateBlock]]:
    """Stand in for the tracking-issue state write; record each block written."""
    blocks: list[StateBlock] = []

    def fake(ctx: ReleaseContext, new: StateBlock) -> None:
        blocks.append(new)
        ctx.state = new

    with patch(_ORCH + ".write_state", side_effect=fake):
        yield blocks


def _run_stage(ctx: ReleaseContext) -> None:
    """Run the tracked update-deps stage as the pipeline does."""
    state = ReleaseState(version_override=None, repo_root=Path("/r"))
    state.ctx = ctx
    with (
        patch(_ORCH + ".comment_phase_complete"),
        patch(_ORCH + ".comment_phase_failed"),
        patch(_ORCH + ".ensure_checklist"),
        patch(_ORCH + ".tick_stage"),
    ):
        stage = next(s for s in build_stages() if s.name == "update-deps")
        stage.fn(state)


def _resume(monkeypatch: pytest.MonkeyPatch, block: StateBlock, *, given: bool) -> ReleaseContext:
    """Run the preflight stage of a resume whose tracking issue holds *block*."""
    ctx = _rctx()
    ctx.issue_number = 42
    ctx.state = block
    state = ReleaseState(
        version_override=None, repo_root=Path("/r"), resume=True, without_deps=given
    )
    monkeypatch.setattr(_ORCH + ".preflight", lambda **_kw: ctx)
    _preflight_stage(state)
    return ctx


def test_update_deps_failure_recorded_in_state(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    """A failed update is not recorded done, so a resume runs it again; it defers, never halts."""
    d = _deps(fix_issue_url="https://x/issues/11")
    d.failure = DepsFailure(
        kind="update", message="python: boom", output_tail="", reproduce="uv lock"
    )
    _patch(monkeypatch, d)
    ctx = _rctx()
    ctx.issue_number = 42
    ctx.state = _block(done=_BEFORE_DEPS)
    with pytest.raises(ReleaseError, match="No PR was opened"):
        _run_stage(ctx)
    assert written == []
    assert ctx.state.done == _BEFORE_DEPS
    assert "update-deps" not in ctx.state.done
    assert first_undone(ctx.state, [s.name for s in build_stages()]) == "update-deps"
    # Deferred: it holds back neither install nor anything before it, only
    # close-tracking, so the tracking issue stays open for a resume.
    assert ctx.deferred_failures == ["update-deps"]
    assert holds("install", ctx.deferred_failures) == []
    assert holds("close-tracking", ctx.deferred_failures) == ["update-deps"]
    assert ctx.deferred_publish_failures == []


def test_failed_merged_branch_deletion_leaves_the_stage_not_done(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    """#4952: the PR merged but its branch could not be deleted; a resume retries."""
    exc = UpdateDepsError(
        phase="finalize",
        command="gh api -X DELETE repos/o/r/git/refs/heads/deps/update-2.1.243",
        message="The dependency-update PR https://x/pull/9 merged, but deleting its branch failed",
    )
    _patch(monkeypatch, exc)
    ctx = _rctx()
    ctx.issue_number = 42
    ctx.state = _block(done=_BEFORE_DEPS)
    with pytest.raises(ReleaseError, match="merged, but deleting its branch failed"):
        _run_stage(ctx)
    assert written == []
    assert "update-deps" not in ctx.state.done
    assert ctx.deferred_failures == ["update-deps"]
    assert holds("close-tracking", ctx.deferred_failures) == ["update-deps"]


def test_update_deps_success_recorded_done(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    _patch(monkeypatch, _deps(pr_url="https://x/pull/9", any_changes=True))
    ctx = _rctx()
    ctx.issue_number = 42
    ctx.state = _block(done=_BEFORE_DEPS)
    _run_stage(ctx)
    assert [b.done for b in written] == [(*_BEFORE_DEPS, "update-deps")]


def test_resume_reruns_failed_update(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    rec = _patch(monkeypatch, _deps(pr_url="https://x/pull/9", any_changes=True))
    ctx = _resume(monkeypatch, _block(done=_BEFORE_DEPS), given=False)
    assert ctx.without_deps is False
    _run_stage(ctx)
    assert rec.run_update_roots == [Path("/r")]
    assert ctx.state is not None
    assert ctx.state.done[-1] == "update-deps"


def test_resume_without_deps_skips_failed_update(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    """--without-deps on a resume is recorded, then skips the failed update and records it done."""
    rec = _patch(monkeypatch, AssertionError("must not run"))
    ctx = _resume(monkeypatch, _block(done=_BEFORE_DEPS), given=True)
    assert ctx.without_deps is True
    assert [b.without_deps for b in written] == [True]
    assert written[0].done == _BEFORE_DEPS
    _run_stage(ctx)
    assert rec.events == []
    assert ctx.deps_report == "Skipped (--without-deps)."
    assert ctx.state is not None
    assert ctx.state.without_deps is True
    assert ctx.state.done == (*_BEFORE_DEPS, "update-deps")


def test_without_deps_persists_across_resume(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    """A resume without the flag still skips the update the state records as skipped."""
    rec = _patch(monkeypatch, AssertionError("must not run"))
    ctx = _resume(monkeypatch, _block(done=_BEFORE_DEPS, without_deps=True), given=False)
    assert ctx.without_deps is True
    assert written == []
    _run_stage(ctx)
    assert rec.events == []
    assert ctx.state is not None
    assert ctx.state.done == (*_BEFORE_DEPS, "update-deps")


def test_without_deps_already_recorded_is_not_rewritten(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    ctx = _resume(monkeypatch, _block(without_deps=True), given=True)
    assert ctx.without_deps is True
    assert written == []


def test_fresh_start_takes_without_deps_from_the_flag(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    """A fresh start has no state block yet: prepare creates it from the flag."""
    ctx = _rctx()
    state = ReleaseState(version_override=None, repo_root=Path("/r"), without_deps=True)
    monkeypatch.setattr(_ORCH + ".preflight", lambda **_kw: ctx)
    _preflight_stage(state)
    assert ctx.without_deps is True
    assert ctx.resume is False
    assert written == []


def test_resume_without_a_state_block_refuses(
    monkeypatch: pytest.MonkeyPatch, written: list[StateBlock]
) -> None:
    ctx = _rctx()
    state = ReleaseState(
        version_override=None, repo_root=Path("/r"), resume=True, without_deps=True
    )
    monkeypatch.setattr(_ORCH + ".preflight", lambda **_kw: ctx)
    with pytest.raises(ReleaseError, match="no release state block"):
        _preflight_stage(state)
    assert written == []
