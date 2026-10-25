"""Tests for the vrg-release ``update-deps`` stage (epic vergil-project/.github#381)."""

from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.update_deps_stage import update_deps
from vergil_tooling.lib.update_deps.context import DepsFailure, UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.report import EMPTY_REPORT, HeldBack
from vergil_tooling.lib.update_deps.updater import UpdateResult

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


def _patch(
    monkeypatch: pytest.MonkeyPatch,
    deps: UpdateDepsContext | BaseException,
    *,
    branch: str = "develop",
) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(_MOD + ".os.chdir", rec.chdirs.append)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: branch)
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: rec.gits.append(a))

    def fake_run_update(*, repo_root: Path) -> UpdateDepsContext:
        rec.run_update_roots.append(repo_root)
        if isinstance(deps, BaseException):
            raise deps
        return deps

    monkeypatch.setattr(_MOD + ".run_update", fake_run_update)
    return rec


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
    monkeypatch.setattr(_MOD + ".os.chdir", lambda _p: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")

    def boom(*_a: str) -> None:
        msg = "not a fast-forward"
        raise RuntimeError(msg)

    monkeypatch.setattr(_MOD + ".git.run", boom)
    with pytest.raises(RuntimeError, match="fast-forward"):
        update_deps(_rctx())


def test_preflight_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """An update-deps preflight refusal (e.g. an in-flight branch) is raised, not hidden."""
    exc = UpdateDepsError(phase="preflight", command="git worktree add", message="in flight")
    _patch(monkeypatch, exc)
    ctx = _rctx()
    with pytest.raises(UpdateDepsError, match="in flight"):
        update_deps(ctx)
    assert ctx.deps_report is None
