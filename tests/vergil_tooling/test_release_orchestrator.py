from __future__ import annotations

import argparse
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_stubs import defer_publish
from vergil_tooling.lib import progress
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.release import state as release_state
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.guard import TrackingIssueBreach
from vergil_tooling.lib.release.merge import ReleaseConflictError
from vergil_tooling.lib.release.orchestrator import (
    ReleaseState,
    _phase_details,
    _preflight_stage,
    _publish_status_stage,
    _tracked,
    build_stages,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_MOD = "vergil_tooling.lib.release.orchestrator"
# merge_release reads PR state via release.merge.known_pr_state (#3137).
_PR_STATE = "vergil_tooling.lib.release.merge.github.pr_state"
_FINALIZE = "vergil_tooling.lib.release.finalize"
_INSTALL = "vergil_tooling.lib.release.install"


def _initial(version: str, done: tuple[str, ...] = ()) -> release_state.StateBlock:
    initial = release_state.initial_state(
        version=version, cut="c" * 40, issues=(), without_deps=False
    )
    return replace(initial, done=done)


@pytest.fixture(autouse=True)
def write_state() -> Iterator[MagicMock]:
    """Stand in for the tracking-issue write every successful stage makes: keep it on *ctx*."""

    def fake(ctx: ReleaseContext, new: release_state.StateBlock) -> None:
        ctx.state = new

    with patch(_MOD + ".write_state", side_effect=fake) as m_write:
        yield m_write


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    ctx.state = _initial("2.1.0")
    return ctx


def test_stage_order_matches_spec() -> None:
    """The stage order of spec §4.4 of epic vergil-project/.github#1440."""
    stages = build_stages()
    assert [s.name for s in stages] == [
        "audit",
        "preflight",
        "prepare",
        "merge-release",
        "confirm-main",
        "back-merge-bump",
        "teardown-worktree",
        "confirm-develop",
        "promote",
        "cd-publish",
        "package-index",
        "finalize",
        "update-deps",
        "install",
        "release-records",
        "close-tracking",
        "publish-status",
    ]
    assert stages[0].skip_flag == "skip_audit"
    fail_fast = {s.name for s in stages if s.mode == "fail_fast"}
    assert fail_fast == {
        "audit",
        "preflight",
        "prepare",
        "merge-release",
        "confirm-main",
        "back-merge-bump",
        "teardown-worktree",
    }


def test_teardown_stage_runs_after_back_merge_and_fails_fast() -> None:
    """teardown-worktree sits between back-merge-bump and confirm-develop, so the
    worktree is gone before finalize runs vrg-finalize-pr from the main
    worktree (#1578). It is fail_fast, as every branch-work stage is: finalize
    prunes the release branches only once all of them have succeeded (spec §4.3
    of epic vergil-project/.github#1440)."""
    stages = build_stages()
    names = [s.name for s in stages]
    teardown = stages[names.index("teardown-worktree")]
    assert names.index("back-merge-bump") < names.index("teardown-worktree")
    assert names.index("teardown-worktree") < names.index("confirm-develop")
    assert names.index("teardown-worktree") < names.index("finalize")
    assert teardown.mode == "fail_fast"


def test_teardown_stage_is_tracked_and_calls_teardown_worktree() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    with (
        patch(_MOD + ".teardown_worktree") as m_teardown,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage") as m_tick,
    ):
        stage = next(s for s in build_stages() if s.name == "teardown-worktree")
        stage.fn(state)
    m_teardown.assert_called_once_with(state.ctx)
    m_tick.assert_called_once_with(state.ctx, "teardown-worktree")
    assert state.ctx.state is not None
    assert state.ctx.state.done == ("teardown-worktree",)


def test_failed_teardown_stops_the_run(tmp_path: Path) -> None:
    """A failed teardown stops the run: confirm-develop and finalize never run."""
    state = ReleaseState(version_override=None, repo_root=tmp_path)
    state.ctx = _ctx()
    args = argparse.Namespace(output_format="plain", output_window=None)
    names = ("teardown-worktree", "confirm-develop", "finalize")
    with (
        patch(_MOD + ".teardown_worktree", side_effect=OSError("busy")),
        patch(_MOD + ".confirm_develop") as m_develop,
        patch(_MOD + ".finalize") as m_finalize,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed") as m_failed,
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        stages = [s for s in build_stages() if s.name in names]
        rc = progress.run_pipeline(
            state, stages, command="vrg-release", label="vrg-release", args=args, repo_root=tmp_path
        )
    assert rc != 0
    m_failed.assert_called_once()
    m_develop.assert_not_called()
    m_finalize.assert_not_called()
    assert state.ctx.state is not None
    assert "teardown-worktree" not in state.ctx.state.done


def test_preflight_stage_populates_ctx() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    ctx = _ctx()
    with patch(_MOD + ".preflight", return_value=ctx) as m_preflight:
        _preflight_stage(state)
    m_preflight.assert_called_once_with(
        version_override=None,
        repo_root=Path("/tmp/repo"),  # noqa: S108
        resume=False,
        resume_version=None,
        resume_issue_number=None,
        allow_pending_deps=False,
        stages=[stage.name for stage in build_stages()],
        supersede=None,
    )
    assert state.ctx is ctx


def test_preflight_stage_renders_the_checklist_the_issue_is_created_with() -> None:
    # #4367: the stages before prepare are ticked; prepare onward are open.
    from vergil_tooling.lib.release import checklist

    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    ctx = _ctx()
    with patch(_MOD + ".preflight", return_value=ctx):
        _preflight_stage(state)
    assert ctx.progress_block is not None
    # publish-status reports only to the terminal and has no box (#4946).
    names = [stage.name for stage in build_stages() if stage.name != "publish-status"]
    pairs = checklist.parse(ctx.progress_block)
    assert [name for name, _ in pairs] == names
    assert [name for name, done in pairs if done] == ["audit", "preflight"]


def test_a_finished_release_leaves_no_unticked_stage() -> None:
    # #4946: every tracked stage ticks its own box, so once each has run, the
    # checklist close-tracking leaves on the closed issue has no open box.
    from vergil_tooling.lib.release import checklist
    from vergil_tooling.lib.release.resume import ALWAYS_RUN

    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    ctx = _ctx()
    with patch(_MOD + ".preflight", return_value=ctx):
        _preflight_stage(state)
    assert ctx.progress_block is not None
    body = ctx.progress_block
    for stage in build_stages():
        if stage.name not in ALWAYS_RUN:
            body = checklist.tick(body, stage.name)
    pairs = checklist.parse(body)
    assert pairs
    assert [name for name, done in pairs if not done] == []
    assert pairs[-1][0] == "close-tracking"


def test_preflight_stage_passes_allow_pending_deps() -> None:
    state = ReleaseState(
        version_override=None,
        repo_root=Path("/tmp/repo"),  # noqa: S108
        allow_pending_deps=True,
    )
    with patch(_MOD + ".preflight", return_value=_ctx()) as m_preflight:
        _preflight_stage(state)
    assert m_preflight.call_args.kwargs["allow_pending_deps"] is True


def test_preflight_stage_passes_supersede() -> None:
    state = ReleaseState(
        version_override=None,
        repo_root=Path("/tmp/repo"),  # noqa: S108
        supersede="2.1.0",
    )
    with patch(_MOD + ".preflight", return_value=_ctx()) as m_preflight:
        _preflight_stage(state)
    assert m_preflight.call_args.kwargs["supersede"] == "2.1.0"


def test_audit_stage_runs_audit() -> None:
    from vergil_tooling.lib.release.orchestrator import _audit_stage

    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    with patch(_MOD + ".run_audit") as m_audit:
        _audit_stage(state)
    m_audit.assert_called_once_with(Path("/tmp/repo"))  # noqa: S108


def test_tracked_stage_comments_on_success() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    fn = MagicMock()
    with (
        patch(_MOD + ".comment_phase_complete") as m_comment,
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        _tracked("prepare", fn)(state)
    fn.assert_called_once_with(state.ctx)
    m_comment.assert_called_once()


def test_tracked_stage_ticks_checklist_with_prior_stages_prechecked() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    with (
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist") as m_ensure,
        patch(_MOD + ".tick_stage") as m_tick,
    ):
        _tracked("prepare", MagicMock())(state)
    names = m_ensure.call_args.args[1]
    checked = m_ensure.call_args.kwargs["checked"]
    assert "publish-status" not in names
    assert names[: names.index("prepare")] == checked
    assert "prepare" not in checked
    m_tick.assert_called_once_with(state.ctx, "prepare")


@pytest.mark.parametrize(
    ("reported", "version", "commit"),
    [(("2.1.0", "f" * 40), "2.1.0", "f" * 40), (None, None, None)],
)
def test_tracked_install_records_what_it_reported(
    reported: tuple[str, str] | None, version: str | None, commit: str | None
) -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()

    def ran(ctx: ReleaseContext) -> None:
        ctx.install_commands = "vrg-tooling-install v2.1"
        ctx.install_reported = reported

    with (
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        _tracked("install", ran)(state)
    assert state.ctx.state is not None
    assert state.ctx.state.install_done is True
    assert state.ctx.state.install_version == version
    assert state.ctx.state.install_commit == commit


def test_stage_names_match_pipeline_order() -> None:
    from vergil_tooling.lib.release.orchestrator import _stage_names, build_stages

    assert _stage_names() == [stage.name for stage in build_stages()]


def test_tracked_stage_comments_release_error_on_failure() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    exc = ReleaseError(phase="prepare", command="git checkout", message="branch exists")
    fn = MagicMock(side_effect=exc)
    with (
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError, match="branch exists"),
    ):
        _tracked("prepare", fn)(state)
    m_failed.assert_called_once_with(state.ctx, "prepare", exc)
    assert state.ctx.deferred_failures == ["prepare"]


def test_tracked_stage_never_comments_a_breach() -> None:
    """A suspected breach stops the run before any write: no failure comment (spec §4.2)."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    fn = MagicMock(side_effect=TrackingIssueBreach("agent", "edited the body", number=42))
    with (
        patch(_MOD + ".comment_phase_failed") as m_failed,
        patch(_MOD + ".comment_phase_complete") as m_complete,
        patch(_MOD + ".tick_stage") as m_tick,
        pytest.raises(TrackingIssueBreach, match="suspected security breach on tracking issue #42"),
    ):
        _tracked("prepare", fn)(state)
    m_failed.assert_not_called()
    m_complete.assert_not_called()
    m_tick.assert_not_called()
    assert state.ctx.deferred_failures == []


def test_tracked_stage_wraps_and_comments_on_failure() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    fn = MagicMock(side_effect=RuntimeError("boom"))
    with (
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError, match="boom"),
    ):
        _tracked("prepare", fn)(state)
    m_failed.assert_called_once()


def test_tracked_stage_wraps_a_failed_command_from_its_fields() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    failed = subprocess.CalledProcessError(1, ["git", "push"], stderr="rejected")
    fn = MagicMock(side_effect=failed)
    with (
        patch(_MOD + ".comment_phase_failed"),
        pytest.raises(ReleaseError) as excinfo,
    ):
        _tracked("prepare", fn)(state)
    assert excinfo.value.command == "git push"
    assert str(excinfo.value) == "Command 'git push' returned non-zero exit status 1."
    assert excinfo.value.detail == "rejected"


def test_tracked_stage_requires_ctx() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    with pytest.raises(ReleaseError, match="preflight did not run"):
        _tracked("prepare", MagicMock())(state)


@pytest.mark.parametrize("name", ["package-index", "cd-publish"])
def test_tracked_stage_lists_a_deferred_publish_as_failed(name: str) -> None:
    """A deferred publish is a failed stage like any other: the hold rule lets
    promote, finalize and install run past it, but close-tracking holds on it,
    so the tracking issue stays open (spec §4.4, §4.5)."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    ctx = state.ctx = _ctx()

    def _defer(_ctx: ReleaseContext) -> None:
        defer_publish(ctx, "package-index" if name == "package-index" else "pypi / publish")
        raise ReleaseError(phase=name, command=name, message="still failing")

    with (
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError, match="still failing"),
    ):
        _tracked(name, _defer)(state)
    m_failed.assert_called_once()
    assert ctx.deferred_failures == [name]


def _held_run(name: str, ctx: ReleaseContext) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Run tracked stage *name* on *ctx*; return its fn, warn and comment mocks."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = ctx
    fn = MagicMock()
    with (
        patch(_MOD + ".mark_warn") as m_warn,
        patch(_MOD + ".comment_phase_complete") as m_complete,
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        _tracked(name, fn)(state)
    return fn, m_warn, m_complete


def test_held_stage_does_not_run_and_is_not_recorded_done(write_state: MagicMock) -> None:
    ctx = _ctx()
    ctx.deferred_failures = ["confirm-develop"]
    fn, m_warn, m_complete = _held_run("promote", ctx)
    fn.assert_not_called()
    m_warn.assert_called_once_with("held: confirm-develop failed")
    m_complete.assert_not_called()
    write_state.assert_not_called()


def test_promote_held_by_failed_confirm_develop(write_state: MagicMock) -> None:
    ctx = _ctx()
    ctx.deferred_failures = ["confirm-develop"]
    with patch(_MOD + ".promote") as m_promote:
        stage = next(s for s in build_stages() if s.name == "promote")
        state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
        state.ctx = ctx
        with patch(_MOD + ".mark_warn"):
            stage.fn(state)
    m_promote.assert_not_called()
    write_state.assert_not_called()


@pytest.mark.parametrize(
    ("name", "failed", "runs"),
    [
        ("promote", ["cd-publish", "package-index"], True),
        ("finalize", ["cd-publish", "package-index"], True),
        ("finalize", ["confirm-develop"], False),
        ("cd-publish", ["confirm-develop"], False),
        ("cd-publish", ["promote"], True),
        ("install", ["cd-publish", "package-index", "finalize", "update-deps"], True),
        ("install", ["promote"], False),
        ("close-tracking", ["update-deps"], False),
        ("close-tracking", ["package-index"], False),
        ("close-tracking", [], True),
        ("release-records", ["update-deps"], False),
        ("release-records", ["cd-publish"], False),
        ("release-records", [], True),
        ("close-tracking", ["release-records"], False),
    ],
)
def test_tracked_stage_applies_the_hold_rule(
    name: str,
    failed: list[str],
    runs: bool,  # noqa: FBT001
) -> None:
    ctx = _ctx()
    ctx.deferred_failures = list(failed)
    ctx.install_commands = None
    fn, _, _ = _held_run(name, ctx)
    assert fn.called is runs


def test_promote_phase_calls_promote() -> None:
    from vergil_tooling.lib.release.orchestrator import _promote_phase

    ctx = _ctx()
    with patch(_MOD + ".promote") as mock_promote:
        _promote_phase(ctx)
    mock_promote.assert_called_once_with(ctx.version)


def test_phase_details_prepare() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.release_branch = "release/2.1.0"
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    ctx.issue_url = "https://github.com/o/r/issues/42"
    details = _phase_details(ctx, "prepare")
    assert "release/2.1.0" in details
    assert "pull/100" in details
    assert "issues/42" in details
    assert "--allow-pending-deps" not in details


def test_phase_details_prepare_names_bypassed_deps_prs() -> None:
    ctx = _ctx()
    ctx.bypassed_deps_prs = ["https://github.com/o/r/pull/9", "https://github.com/o/r/pull/11"]
    details = _phase_details(ctx, "prepare")
    assert "--allow-pending-deps" in details
    assert "https://github.com/o/r/pull/9" in details
    assert "https://github.com/o/r/pull/11" in details


def test_phase_details_prepare_names_cleared_deps_prs() -> None:
    ctx = _ctx()
    ctx.cleared_deps_prs = ["https://github.com/o/r/pull/9"]
    details = _phase_details(ctx, "prepare")
    assert "Closed landed dependency-update PR(s)" in details
    assert "https://github.com/o/r/pull/9" in details
    assert "--allow-pending-deps" not in details


def test_phase_details_prepare_names_undeleted_deps_branches() -> None:
    """A closed deps PR whose branch could not be deleted is named (#3332)."""
    ctx = _ctx()
    ctx.cleared_deps_prs = ["https://github.com/o/r/pull/9"]
    ctx.undeleted_deps_branches = ["deps/update-2.1.0"]
    details = _phase_details(ctx, "prepare")
    assert "Could not delete the branch(es)" in details
    assert "`deps/update-2.1.0`" in details


def test_phase_details_prepare_omits_cleared_line_when_none() -> None:
    details = _phase_details(_ctx(), "prepare")
    assert "dependency-update" not in details


def test_phase_details_merge_release() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    details = _phase_details(ctx, "merge-release")
    assert "pull/100" in details


def test_phase_details_confirm_main() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.tag = "v2.1.0"
    ctx.release_url = "https://github.com/o/r/releases/tag/v2.1.0"
    ctx.cd_run_url = "https://github.com/o/r/actions/runs/123"
    details = _phase_details(ctx, "confirm-main")
    assert "v2.1.0" in details
    assert "runs/123" in details


def test_phase_details_back_merge_bump() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.bump_pr_url = "https://github.com/o/r/pull/101"
    ctx.next_version = "2.1.1"
    details = _phase_details(ctx, "back-merge-bump")
    assert "pull/101" in details
    assert "2.1.1" in details


def test_phase_details_confirm_develop() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.develop_cd_run_url = "https://github.com/o/r/actions/runs/456"
    details = _phase_details(ctx, "confirm-develop")
    assert "runs/456" in details


def test_phase_details_promote() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "promote")
    assert "v2.1" in details


def test_phase_details_cd_publish_says_nothing_is_left() -> None:
    assert _phase_details(_ctx(), "cd-publish") == "No failed CD publish job is left to deliver."


@pytest.mark.parametrize("deferred", ["package-index", "publish-pypi"])
def test_phase_details_promote_with_a_deferred_publish_still_promotes(deferred: str) -> None:
    ctx = _ctx()
    defer_publish(ctx, deferred)
    assert _phase_details(ctx, "promote") == "Promoted v2.1 -> v2.1.0"


def _run_promote_stage(ctx: ReleaseContext) -> MagicMock:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = ctx
    with (
        patch(_MOD + ".promote") as m_promote,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        stages = build_stages()
        stages[[s.name for s in stages].index("promote")].fn(state)
    return m_promote


def test_promote_stage_moves_the_tag() -> None:
    ctx = _ctx()
    _run_promote_stage(ctx).assert_called_once_with("2.1.0")
    assert ctx.state is not None
    assert "promote" in ctx.state.done


def test_promote_runs_with_index_deferred() -> None:
    """promote moves vX.Y once the tag and the Release exist, whatever the index
    shows (spec §4.6): a deferred index no longer holds it back."""
    ctx = _ctx()
    defer_publish(ctx, "package-index")
    ctx.deferred_failures = ["package-index"]
    _run_promote_stage(ctx).assert_called_once_with("2.1.0")
    assert ctx.state is not None
    assert "promote" in ctx.state.done


def test_phase_details_finalize() -> None:
    assert _phase_details(_ctx(), "finalize") == "Repository finalized."


def test_phase_details_close_tracking() -> None:
    assert _phase_details(_ctx(), "close-tracking") == "Tracking issue closed."


def test_phase_details_install_ran() -> None:
    ctx = _ctx()
    ctx.install_commands = "uv tool install pkg@v2.1.0"
    assert _phase_details(ctx, "install") == "Install commands for v2.1.0 ran."


def test_phase_details_install_none_configured() -> None:
    assert "None configured" in _phase_details(_ctx(), "install")


def test_stage_order_puts_install_after_finalize_and_update_deps() -> None:
    names = [s.name for s in build_stages()]
    for earlier in ("promote", "finalize", "update-deps"):
        assert names.index(earlier) < names.index("install")
    assert names.index("install") < names.index("release-records")
    assert names.index("release-records") < names.index("close-tracking")
    assert names.index("close-tracking") < names.index("publish-status")
    assert names[-1] == "publish-status"


_DEPS_FAILURE = (
    "Dependency update for 2.1.3 failed (validation): Validation failed after dependency "
    "updates. The PR stays open: https://example.test/pull/7."
)


def _fail_update_deps(ctx: ReleaseContext) -> None:
    ctx.deps_failure = _DEPS_FAILURE
    raise ReleaseError(phase="update-deps", command="vrg-update-deps", message=_DEPS_FAILURE)


_LINE_ON_RELEASE = "o\trefs/tags/v2.1\n" + "t" * 40 + "\trefs/tags/v2.1^{}"


def _run_tail(  # noqa: PLR0913
    tmp_path: Path,
    *,
    names: tuple[str, ...],
    cd_deferred: tuple[str, ...] = (),
    index_times_out: bool = False,
    promote_error: Exception | None = None,
    deps_fail: bool = False,
    line: str = _LINE_ON_RELEASE,
) -> tuple[int, ReleaseContext, dict[str, MagicMock]]:
    """Run the real stages in *names* through the pipeline, with GitHub stubbed.

    A CD publish job in *cd_deferred* is recorded as failed and cd-publish
    fails on it, as on a fresh run; *index_times_out* fails package-index;
    *promote_error* fails the tag move; *deps_fail* fails update-deps. The
    returned mocks are the ones each stage acts through, by name.
    """
    state = _state()
    ctx = state.ctx
    assert ctx is not None
    ctx.issue_number = 42
    ctx.tag = "v2.1.2"
    ctx.tag_commit = "t" * 40
    defer_publish(ctx, *cd_deferred)
    timeout = ReleaseError(phase="package-index", command="poll package index", message="slow")
    args = argparse.Namespace(output_format="plain", output_window=None)
    with (
        patch(
            "vergil_tooling.lib.release.package_index._wait",
            side_effect=timeout if index_times_out else None,
        ),
        patch("vergil_tooling.lib.release.cd_publish.github") as m_gh,
        patch(_MOD + ".comment_phase_complete") as m_complete,
        patch(_MOD + ".comment_phase_failed") as m_failed,
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
        patch(_FINALIZE + ".close_tracking_issue") as m_close,
        patch(_FINALIZE + ".progress") as m_progress,
        patch(_MOD + ".promote", side_effect=promote_error) as m_promote,
        patch(_MOD + ".update_deps", side_effect=_fail_update_deps if deps_fail else None),
        patch(_INSTALL + ".config.read_config") as m_config,
        patch(_INSTALL + ".git.read_output", return_value=line),
        patch(_INSTALL + ".run_install", return_value=(0, "")) as m_install,
    ):
        m_config.return_value.publish.install = "uv tool install pkg@v<VERSION>"
        stages = [s for s in build_stages() if s.name in names]
        assert [s.name for s in stages] == list(names)
        rc = progress.run_pipeline(
            state, stages, command="vrg-release", label="vrg-release", args=args, repo_root=tmp_path
        )
    mocks = {
        "gh": m_gh,
        "complete": m_complete,
        "failed": m_failed,
        "close": m_close,
        "finalize": m_progress.run,
        "promote": m_promote,
        "install": m_install,
    }
    return rc, ctx, mocks


_FULL_TAIL = (
    "promote",
    "cd-publish",
    "package-index",
    "finalize",
    "update-deps",
    "install",
    "release-records",
    "close-tracking",
    "publish-status",
)


def test_install_runs_after_promote(tmp_path: Path) -> None:
    rc, ctx, mocks = _run_tail(tmp_path, names=("promote", "install"))
    assert rc == 0
    mocks["install"].assert_called_once_with("uv tool install pkg@v2.1.2", cwd=ctx.repo_root)
    assert ctx.install_message is None
    assert ctx.state is not None
    assert "install" in ctx.state.done


def test_install_refuses_after_a_failed_promote(tmp_path: Path) -> None:
    """A failed promote leaves vX.Y on the previous release: install holds,
    never installing another release, and close-tracking holds too."""
    error = ReleaseError(phase="promote", command="git push", message="refused")
    rc, ctx, mocks = _run_tail(
        tmp_path, names=("promote", "install", "close-tracking"), promote_error=error
    )
    assert rc == 1
    mocks["install"].assert_not_called()
    mocks["close"].assert_not_called()
    assert ctx.deferred_failures == ["promote"]
    assert ctx.state is not None
    assert "install" not in ctx.state.done


def test_install_refuses_when_vx_y_names_another_release(tmp_path: Path) -> None:
    rc, ctx, mocks = _run_tail(
        tmp_path,
        names=("promote", "install", "close-tracking"),
        line="o\trefs/tags/v2.1\n" + "e" * 40 + "\trefs/tags/v2.1^{}",
    )
    assert rc == 1
    mocks["install"].assert_not_called()
    mocks["close"].assert_not_called()
    assert ctx.deferred_failures == ["install"]


def test_install_skipped_when_done(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A resume whose state block records the install done skips it (spec §4.6)."""
    state = _state()
    ctx = state.ctx
    assert ctx is not None
    ctx.state = _initial("2.1.2", done=("promote", "install"))
    with (
        patch(_INSTALL + ".run_install") as m_run,
        patch(_INSTALL + ".config.read_config") as m_config,
        patch(_MOD + ".tick_stage") as m_tick,
    ):
        stage = next(s for s in build_stages() if s.name == "install")
        stage.fn(state)
    m_run.assert_not_called()
    m_config.assert_not_called()
    m_tick.assert_not_called()
    assert "install: already done" in capsys.readouterr().out


def test_update_deps_failure_does_not_hold_install(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failed update-deps holds back only close-tracking: the install runs, the
    tracking issue stays open, and publish-status repeats the failure."""
    rc, ctx, mocks = _run_tail(
        tmp_path,
        names=("promote", "update-deps", "install", "close-tracking", "publish-status"),
        deps_fail=True,
    )
    assert rc == 1
    assert ctx.deferred_failures == ["update-deps"]
    mocks["install"].assert_called_once_with("uv tool install pkg@v2.1.2", cwd=ctx.repo_root)
    mocks["close"].assert_not_called()
    out = capsys.readouterr().out
    assert "✗ update-deps" in out
    assert "✓ install" in out
    assert _DEPS_FAILURE in _publish_status(out)


def _run_develop_then_publish(
    tmp_path: Path, state: ReleaseState, confirm: MagicMock
) -> tuple[int, MagicMock]:
    """Run the real confirm-develop, cd-publish and close-tracking stages on *state*."""
    names = ("confirm-develop", "cd-publish", "close-tracking")
    args = argparse.Namespace(output_format="plain", output_window=None)
    with (
        patch(_MOD + ".confirm_develop", confirm),
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
        patch(_FINALIZE + ".close_tracking_issue") as m_close,
    ):
        stages = [s for s in build_stages() if s.name in names]
        rc = progress.run_pipeline(
            state, stages, command="vrg-release", label="vrg-release", args=args, repo_root=tmp_path
        )
    return rc, m_close


def test_cd_publish_waits_for_confirm_develop_across_a_resume(tmp_path: Path) -> None:
    """A failed confirm-develop holds cd-publish, so a resume whose confirm-develop
    records a failed develop job still delivers it, and the issue stays open
    until it is delivered (merge review R1)."""
    state = _state()
    ctx = state.ctx
    assert ctx is not None
    ctx.issue_number = 42
    failed = ReleaseError(phase="confirm-develop", command="gh run watch", message="timed out")
    rc, m_close = _run_develop_then_publish(tmp_path, state, MagicMock(side_effect=failed))
    assert rc == 1
    m_close.assert_not_called()
    assert ctx.state is not None
    assert "cd-publish" not in ctx.state.done

    # The resume: confirm-develop succeeds and records develop's failed docs job.
    resumed = _state()
    rctx = resumed.ctx
    assert rctx is not None
    rctx.issue_number = 42
    rctx.resume = True
    rctx.state = ctx.state

    def record_docs_failure(c: ReleaseContext) -> None:
        defer_publish(c, "docs")

    with patch("vergil_tooling.lib.release.cd_publish._check_recorded_run") as m_check:
        m_check.side_effect = ReleaseError(phase="cd-publish", command="x", message="not yet")
        rc, m_close = _run_develop_then_publish(
            tmp_path, resumed, MagicMock(side_effect=record_docs_failure)
        )
    assert rc == 1
    m_check.assert_called_once()
    m_close.assert_not_called()
    assert rctx.deferred_failures == ["cd-publish"]


def test_finalize_and_install_run_after_failed_cd_publish(tmp_path: Path) -> None:
    """A failed CD publish job is deferred: promote, finalize and install run,
    and the run stops before close-tracking with the issue open (spec §4.5)."""
    rc, ctx, mocks = _run_tail(tmp_path, names=_FULL_TAIL, cd_deferred=("docker-publish",))
    assert rc == 1
    assert ctx.deferred_failures == ["cd-publish"]
    mocks["promote"].assert_called_once_with("2.1.2")
    mocks["finalize"].assert_called_once()
    mocks["install"].assert_called_once()
    mocks["close"].assert_not_called()
    assert ctx.state is not None
    for name in ("promote", "package-index", "finalize", "update-deps", "install"):
        assert name in ctx.state.done
    assert "close-tracking" not in ctx.state.done


def test_install_runs_with_index_deferred_through_the_pipeline(tmp_path: Path) -> None:
    rc, ctx, mocks = _run_tail(tmp_path, names=_FULL_TAIL, index_times_out=True)
    assert rc == 1
    assert ctx.deferred_failures == ["package-index"]
    mocks["promote"].assert_called_once_with("2.1.2")
    mocks["install"].assert_called_once()
    mocks["close"].assert_not_called()


def test_clean_tail_closes_the_tracking_issue_last(tmp_path: Path) -> None:
    rc, ctx, mocks = _run_tail(tmp_path, names=_FULL_TAIL)
    assert rc == 0
    mocks["close"].assert_called_once()
    assert ctx.state is not None
    assert "close-tracking" in ctx.state.done


_HAND_RUN = ("gh run rerun", "gh workflow run", "vrg-promote")


def test_no_output_names_hand_run_commands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run with every kind of deferral names only vrg-release --resume or
    --supersede, never a command to run by hand: not on the terminal, and not
    in any comment on the tracking issue."""
    _, _, mocks = _run_tail(
        tmp_path,
        names=_FULL_TAIL,
        cd_deferred=("docker-publish",),
        index_times_out=True,
        deps_fail=True,
    )
    captured = capsys.readouterr()
    posted = [
        str(call) for call in mocks["complete"].call_args_list + mocks["failed"].call_args_list
    ]
    for text in (captured.out, captured.err, *posted):
        for command in _HAND_RUN:
            assert command not in text
    status = _publish_status(captured.out)
    assert "vrg-release --resume 2.1.2" in status
    assert "vrg-release --supersede 2.1.2" in status


def test_package_index_and_cd_publish_follow_promote() -> None:
    """promote no longer waits on the publishes (spec §4.4): cd-publish and
    package-index follow it, and finalize follows them."""
    from vergil_tooling.lib.release.orchestrator import _stage_names

    names = _stage_names()
    assert names[names.index("confirm-develop") + 1] == "promote"
    assert names.index("promote") < names.index("cd-publish") < names.index("package-index")
    assert names.index("package-index") < names.index("finalize")
    stages = build_stages()
    for name in ("promote", "cd-publish", "package-index", "finalize"):
        assert stages[names.index(name)].mode == "fail_defer"


def test_package_index_stage_runs_wait_for_index_tracked() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    with (
        patch(_MOD + ".wait_for_index") as m_wait,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        stages = build_stages()
        stages[[s.name for s in stages].index("package-index")].fn(state)
    m_wait.assert_called_once_with(state.ctx)


def test_phase_details_package_index() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    assert "No [package]" in _phase_details(ctx, "package-index")
    ctx.package_index_urls = ("https://b/deb/dists/noble/main/binary-amd64/Packages",)
    noble = "https://b/deb/dists/noble/main/binary-amd64/Packages"
    assert _phase_details(ctx, "package-index") == f"Package visible in index: {noble}"


def test_phase_details_package_index_several_packages() -> None:
    """Each index file is listed once, even when two packages share it."""
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    noble = "https://b/deb/dists/noble/main/binary-amd64/Packages"
    el10 = "https://b/rpm/el10/x86_64/repodata/repomd.xml"
    ctx.package_index_urls = (noble, el10, noble)
    assert _phase_details(ctx, "package-index") == (
        f"Package visible in index: {noble}\nPackage visible in index: {el10}"
    )


def test_phase_details_unknown_phase() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "unknown-phase")
    assert details == ""


def test_phase_details_unset_fields_yield_empty() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()  # all URL/tag/version fields unset
    for phase in (
        "prepare",
        "merge-release",
        "confirm-main",
        "back-merge-bump",
        "confirm-develop",
    ):
        assert _phase_details(ctx, phase) == ""


def test_merge_release_raises_if_no_pr_url() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    with pytest.raises(ReleaseError, match="release_pr_url is not set"):
        merge_release(ctx)


def test_merge_release_calls_wait_and_merge() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    with (
        patch(_PR_STATE, return_value="OPEN"),
        patch(_MOD + ".wait_and_merge") as m_wm,
        patch(_MOD + ".sync_local_branch") as m_sync,
    ):
        merge_release(ctx)
    m_wm.assert_called_once()
    args, kwargs = m_wm.call_args
    assert args == ("https://github.com/o/r/pull/100",)
    assert kwargs["phase"] == "merge-release"
    assert callable(kwargs["on_branch_updated"])
    # No server-side update -> the local release branch is left alone (#3175).
    m_sync.assert_not_called()
    assert ctx.release_merge_sha == "merged"


def _updating_wait_and_merge(updates: int) -> MagicMock:
    """A wait_and_merge stand-in that reports *updates* server-side branch updates."""

    def fake(_pr: str, *, phase: str, on_branch_updated: Callable[[], None]) -> None:
        for _ in range(updates):
            on_branch_updated()

    return MagicMock(side_effect=fake)


def test_merge_release_syncs_local_branch_after_server_update() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    ctx.release_branch = "release/2.1.0"
    ctx.worktree_path = Path("/tmp/wt")  # noqa: S108
    with (
        patch(_PR_STATE, return_value="OPEN"),
        patch(_MOD + ".wait_and_merge", _updating_wait_and_merge(2)),
        patch(_MOD + ".sync_local_branch") as m_sync,
    ):
        merge_release(ctx)
    # Synced once, after the merge, however many updates happened.
    m_sync.assert_called_once_with(
        "https://github.com/o/r/pull/100",
        branch="release/2.1.0",
        worktree=Path("/tmp/wt"),  # noqa: S108
        phase="merge-release",
    )
    assert ctx.release_merge_sha == "merged"


def test_merge_release_sync_failure_propagates() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    ctx.release_branch = "release/2.1.0"
    ctx.worktree_path = Path("/tmp/wt")  # noqa: S108
    err = ReleaseError(phase="merge-release", command="sync_local_branch", message="diverged")
    with (
        patch(_PR_STATE, return_value="OPEN"),
        patch(_MOD + ".wait_and_merge", _updating_wait_and_merge(1)),
        patch(_MOD + ".sync_local_branch", side_effect=err),
        pytest.raises(ReleaseError, match="diverged"),
    ):
        merge_release(ctx)
    assert ctx.release_merge_sha is None


def test_merge_release_update_without_worktree_raises() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    with (
        patch(_PR_STATE, return_value="OPEN"),
        patch(_MOD + ".wait_and_merge", _updating_wait_and_merge(1)),
        patch(_MOD + ".sync_local_branch") as m_sync,
        pytest.raises(ReleaseError, match="cannot be synced"),
    ):
        merge_release(ctx)
    m_sync.assert_not_called()


def _conflict() -> ReleaseConflictError:
    return ReleaseConflictError(
        phase="merge-release", command="pr_merge.wait_and_merge", message="has merge conflicts."
    )


def _release_ctx() -> ReleaseContext:
    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    ctx.release_branch = "release/2.1.0"
    ctx.worktree_path = Path("/tmp/wt")  # noqa: S108
    return ctx


def test_merge_release_heals_a_conflicting_pr_then_merges() -> None:
    """A CONFLICTING release PR merges main in and waits again (#3526)."""
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _release_ctx()
    with (
        patch(_PR_STATE, return_value="OPEN"),
        patch(_MOD + ".wait_and_merge", side_effect=[_conflict(), None]) as m_wm,
        patch(_MOD + ".merge_main_into_release") as m_heal,
        patch(_MOD + ".time.sleep") as m_sleep,
    ):
        merge_release(ctx)
    assert m_wm.call_count == 2
    m_heal.assert_called_once_with(
        "https://github.com/o/r/pull/100",
        branch="release/2.1.0",
        worktree=Path("/tmp/wt"),  # noqa: S108
        phase="merge-release",
    )
    m_sleep.assert_called_once()
    assert ctx.release_merge_sha == "merged"


def test_merge_release_heals_are_bounded() -> None:
    from vergil_tooling.lib.release.orchestrator import _MAX_CONFLICT_HEALS, merge_release

    ctx = _release_ctx()
    with (
        patch(_PR_STATE, return_value="OPEN"),
        patch(_MOD + ".wait_and_merge", side_effect=_conflict()),
        patch(_MOD + ".merge_main_into_release") as m_heal,
        patch(_MOD + ".time.sleep"),
        pytest.raises(ReleaseError, match="still conflicts after merging main") as excinfo,
    ):
        merge_release(ctx)
    assert m_heal.call_count == _MAX_CONFLICT_HEALS
    assert not isinstance(excinfo.value, ReleaseConflictError)
    assert "vrg-release --resume" in str(excinfo.value)
    assert ctx.release_merge_sha is None


def test_merge_release_heal_refusal_propagates() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _release_ctx()
    refusal = ReleaseError(phase="merge-release", command="git merge", message="beyond CHANGELOG")
    with (
        patch(_PR_STATE, return_value="OPEN"),
        patch(_MOD + ".wait_and_merge", side_effect=_conflict()) as m_wm,
        patch(_MOD + ".merge_main_into_release", side_effect=refusal),
        pytest.raises(ReleaseError, match="beyond CHANGELOG"),
    ):
        merge_release(ctx)
    m_wm.assert_called_once()
    assert ctx.release_merge_sha is None


def test_merge_release_conflict_without_worktree_raises() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    with (
        patch(_PR_STATE, return_value="OPEN"),
        patch(_MOD + ".wait_and_merge", side_effect=_conflict()),
        patch(_MOD + ".merge_main_into_release") as m_heal,
        pytest.raises(ReleaseError, match="main cannot be merged into it"),
    ):
        merge_release(ctx)
    m_heal.assert_not_called()


def test_merge_release_pr_state_retries_404() -> None:
    """release_pr_url came from gh pr create/list; a 404 reading it is lag (#3137)."""
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404: Not Found")
    with (
        patch(_PR_STATE, side_effect=[err, "MERGED"]) as m_state,
        patch(_MOD + ".wait_and_merge") as m_wm,
        patch("vergil_tooling.lib.retry.time.sleep"),
    ):
        merge_release(ctx)
    assert m_state.call_count == 2
    m_wm.assert_not_called()


def test_merge_release_skips_when_already_merged() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    with (
        patch(_PR_STATE, return_value="MERGED"),
        patch(_MOD + ".wait_and_merge") as m_wm,
    ):
        merge_release(ctx)
    m_wm.assert_not_called()
    assert ctx.release_merge_sha == "merged"


def _state() -> ReleaseState:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/r"))  # noqa: S108
    state.ctx = ReleaseContext(
        repo="o/r",
        version="2.1.2",
        repo_root=Path("/tmp/r"),  # noqa: S108
        version_override=None,
    )
    state.ctx.state = _initial("2.1.2")
    return state


def _status_reason(state: ReleaseState) -> str:
    """Run publish-status on *state*; return the warning it marks the row with."""
    with patch(_MOD + ".mark_warn") as m_warn:
        _publish_status_stage(state)  # never raises: the failed stage already counted
    m_warn.assert_called_once()
    return str(m_warn.call_args.args[0])


def test_publish_status_names_only_resume_or_supersede() -> None:
    """The terminal summary names what is left and the one command that
    finishes it, never a hand-run remedy (spec §4.4)."""
    state = _state()
    assert state.ctx is not None
    state.ctx.state = _initial("2.1.2", done=("prepare", "promote", "finalize"))
    defer_publish(state.ctx, "docker-publish", "package-index")
    state.ctx.deferred_failures = ["cd-publish", "package-index"]
    reason = _status_reason(state)
    assert reason.startswith("Release v2.1.2 is not finished: cd-publish, package-index failed")
    assert "not done: merge-release, confirm-main," in reason
    assert "close-tracking." in reason
    assert "Deferred publish: docker-publish, package-index" in reason
    assert "`vrg-release --resume 2.1.2`" in reason
    assert "`vrg-release --supersede 2.1.2`" in reason
    for hand_run in ("gh run rerun", "gh workflow run", "vrg-promote", "publish-index"):
        assert hand_run not in reason


def test_publish_status_noop_when_clean() -> None:
    state = _state()
    with patch(_MOD + ".mark_warn") as m_warn:
        _publish_status_stage(state)
    m_warn.assert_not_called()


def test_publish_status_is_the_last_stage_and_fail_defer() -> None:
    stages = build_stages()
    assert stages[-1].name == "publish-status"
    assert stages[-1].mode == "fail_defer"


# --- update-deps stage (epic vergil-project/.github#381) ---------------------


def test_update_deps_stage_between_finalize_and_install() -> None:
    stages = build_stages()
    names = [s.name for s in stages]
    assert names[-6:] == [
        "finalize",
        "update-deps",
        "install",
        "release-records",
        "close-tracking",
        "publish-status",
    ]
    assert stages[names.index("update-deps")].mode == "fail_defer"


def test_update_deps_stage_is_tracked_and_runs_update_deps() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    with (
        patch(_MOD + ".update_deps") as m_update,
        patch(_MOD + ".comment_phase_complete") as m_comment,
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage") as m_tick,
    ):
        # build_stages binds update_deps when called, so build under the patch.
        stage = next(s for s in build_stages() if s.name == "update-deps")
        stage.fn(state)
    m_update.assert_called_once_with(state.ctx)
    assert m_comment.call_args.args[1] == "update-deps"
    m_tick.assert_called_once_with(state.ctx, "update-deps")


def test_phase_details_update_deps_with_pr() -> None:
    ctx = _ctx()
    ctx.deps_report = "REPORT"
    ctx.deps_pr_url = "https://x/pull/9"
    text = _phase_details(ctx, "update-deps")
    assert "**Held back by declared bounds**" in text
    assert "REPORT" in text
    assert "Dependency-update PR: https://x/pull/9" in text
    assert "No dependency changes" not in text


def test_phase_details_update_deps_noop() -> None:
    ctx = _ctx()
    ctx.deps_report = "REPORT"
    text = _phase_details(ctx, "update-deps")
    assert "REPORT" in text
    assert "No dependency changes; no PR opened." in text


def test_phase_details_update_deps_already_merged() -> None:
    ctx = _ctx()
    ctx.deps_landed_pr_url = "https://x/pull/7"
    ctx.deps_pr_url = "https://x/pull/7"
    text = _phase_details(ctx, "update-deps")
    assert text == (
        "The dependency update for this develop version had already merged: "
        "https://x/pull/7. Nothing to update."
    )


def test_phase_details_update_deps_skipped() -> None:
    ctx = _ctx()
    ctx.without_deps = True
    ctx.deps_report = "Skipped (--without-deps)."
    text = _phase_details(ctx, "update-deps")
    assert text == "Dependency update skipped (--without-deps)."


def test_publish_status_repeats_a_deps_failure() -> None:
    # update-deps already failed (and counted); publish-status repeats it as a
    # warning on its own row rather than a second failure (#3283).
    state = _state()
    assert state.ctx is not None
    state.ctx.deps_failure = "Dependency update for 2.1.243 failed (validation)"
    state.ctx.deferred_failures = ["update-deps"]
    reason = _status_reason(state)
    assert "Dependency update for 2.1.243 failed (validation)" in reason
    assert "update-deps failed" in reason
    assert "Deferred publish" not in reason


def test_release_summary_counts_a_deps_only_failure_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state()
    deps = "Dependency update for 2.1.243 failed (validation): red."

    def _failed_update_deps(st: ReleaseState) -> None:
        assert st.ctx is not None
        st.ctx.deps_failure = deps
        st.ctx.deferred_failures.append("update-deps")
        raise ReleaseError(phase="update-deps", command="vrg-update-deps", message=deps)

    stages = [
        Stage("update-deps", _failed_update_deps, mode="fail_defer"),
        Stage("publish-status", _publish_status_stage, mode="fail_defer"),
    ]
    args = argparse.Namespace(output_format="plain", output_window=None)
    rc = progress.run_pipeline(
        state, stages, command="vrg-release", label="vrg-release", args=args, repo_root=tmp_path
    )
    out = capsys.readouterr().out
    assert rc == 1
    assert "PipelineError: 1 stage failed (update-deps) · exit 1" in out
    # Never dropped: publish-status still carries the deps failure.
    assert "⚠ publish-status  " in out
    warnings = out.split("⚠  warnings (non-fatal):", 1)[1].split("✗  failures:", 1)[0]
    assert "publish-status — " in warnings
    assert deps in warnings


def test_publish_status_reports_both_failures() -> None:
    state = _state()
    assert state.ctx is not None
    defer_publish(state.ctx, "docs")
    state.ctx.deps_failure = "Dependency update for 2.1.243 failed (merge)"
    state.ctx.deferred_failures = ["cd-publish", "update-deps"]
    reason = _status_reason(state)
    assert "cd-publish, update-deps failed" in reason
    assert "Deferred publish: docs" in reason
    assert "2.1.243 failed" in reason


def test_publish_status_noop_without_ctx() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/r"))  # noqa: S108
    _publish_status_stage(state)  # no raise


def test_package_index_success_records_index_listed() -> None:
    """A package-index that saw every package records index_listed (spec §4.5 of epic 1440)."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()

    def listed(ctx: ReleaseContext) -> None:
        ctx.package_index_urls = ("https://example.test/Packages",)

    with (
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        _tracked("package-index", listed)(state)
    assert state.ctx.state is not None
    assert state.ctx.state.index_listed is True
    assert state.ctx.state.done == ("package-index",)


def test_package_index_without_packages_leaves_index_listed_unset() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = _ctx()
    with (
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        _tracked("package-index", lambda _ctx: None)(state)
    assert state.ctx.state is not None
    assert state.ctx.state.index_listed is False


def test_preflight_stage_copies_resume() -> None:
    ctx = _ctx()
    state = ReleaseState(
        version_override=None,
        repo_root=Path("/tmp/repo"),  # noqa: S108
        resume=True,
        resume_version="2.1.0",
        resume_issue_number=42,
    )
    with patch(_MOD + ".preflight", return_value=ctx):
        _preflight_stage(state)
    assert ctx.resume is True


def test_preflight_stage_copies_without_deps() -> None:
    state = ReleaseState(
        version_override=None,
        repo_root=Path("/tmp/repo"),  # noqa: S108
        without_deps=True,
    )
    ctx = _ctx()
    with patch(_MOD + ".preflight", return_value=ctx):
        _preflight_stage(state)
    assert ctx.without_deps is True


def _publish_status(out: str) -> str:
    """The publish-status stage's status line: what is left, and the command that finishes it."""
    return next(line for line in out.splitlines() if line.startswith("⚠ publish-status"))


# --- resume from the first undone stage (spec §4.3, epic vergil-project/.github#1440) ---

_DONE_THROUGH_FINALIZE = (
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
_PREFLIGHT = "vergil_tooling.lib.release.preflight"


def _tracked_run(name: str, ctx: ReleaseContext, fn: MagicMock) -> MagicMock:
    """Run *fn* as tracked stage *name* on *ctx*; return the tick-stage mock."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"))  # noqa: S108
    state.ctx = ctx
    with (
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage") as m_tick,
    ):
        _tracked(name, fn)(state)
    return m_tick


def test_tracked_stage_records_done_with_its_facts(write_state: MagicMock) -> None:
    ctx = _ctx()

    def confirm(c: ReleaseContext) -> None:
        c.tag = "v2.1.0"
        c.tag_commit = "t" * 40
        c.cd_run_id = "555"

    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    _tracked_run("confirm-main", ctx, MagicMock(side_effect=confirm))
    write_state.assert_called_once()
    assert ctx.state is not None
    assert ctx.state.done == ("confirm-main",)
    assert ctx.state.release_pr == "https://github.com/o/r/pull/100"
    assert ctx.state.tag == "v2.1.0"
    assert ctx.state.tag_commit == "t" * 40
    assert ctx.state.main_cd_run == "555"
    assert ctx.state.bump_pr is None
    assert ctx.state.index_listed is False


def test_tracked_stage_skips_a_done_stage(
    write_state: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx = _ctx()
    ctx.state = _initial("2.1.0", done=("prepare",))
    fn = MagicMock()
    m_tick = _tracked_run("prepare", ctx, fn)
    fn.assert_not_called()
    write_state.assert_not_called()
    m_tick.assert_not_called()
    assert "prepare: already done" in capsys.readouterr().out


def test_tracked_stage_without_a_state_block_refuses(write_state: MagicMock) -> None:
    ctx = _ctx()
    ctx.state = None
    with (
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError, match="no release state block"),
    ):
        _tracked_run("merge-release", ctx, MagicMock())
    write_state.assert_not_called()
    m_failed.assert_called_once()


def test_tracked_stage_failure_is_not_recorded_done(write_state: MagicMock) -> None:
    ctx = _ctx()
    fn = MagicMock(side_effect=ReleaseError(phase="prepare", command="x", message="boom"))
    with patch(_MOD + ".comment_phase_failed"), pytest.raises(ReleaseError):
        _tracked_run("prepare", ctx, fn)
    write_state.assert_not_called()


def test_install_with_none_configured_is_done(write_state: MagicMock) -> None:
    ctx = _ctx()
    ctx.install_commands = None
    _tracked_run("install", ctx, MagicMock())
    assert ctx.state is not None
    assert ctx.state.done == ("install",)


def test_resume_after_finalize_skips_branch_stages(write_state: MagicMock) -> None:
    """A resume after finalize creates no worktree and never re-enters prepare."""
    current = _initial("2.1.0", done=_DONE_THROUGH_FINALIZE)
    state = ReleaseState(
        version_override=None,
        repo_root=Path("/tmp/repo"),  # noqa: S108
        resume=True,
        resume_version="2.1.0",
        resume_issue_number=42,
    )

    def read(ctx: ReleaseContext) -> release_state.StateBlock:
        ctx.state = current
        return current

    with (
        patch(_PREFLIGHT + "._check_host_prerequisites"),
        patch(_PREFLIGHT + ".check_gh_auth", return_value="owner/repo"),
        patch(_PREFLIGHT + "._read_and_validate_config"),
        patch(_PREFLIGHT + ".read_state", side_effect=read),
        patch(_PREFLIGHT + ".verify_facts"),
        patch(_PREFLIGHT + "._acquire_release_worktree") as m_worktree,
        patch(_PREFLIGHT + ".os.chdir") as m_chdir,
    ):
        _preflight_stage(state)
    m_worktree.assert_not_called()
    m_chdir.assert_called_once_with(Path("/tmp/repo"))  # noqa: S108
    assert state.ctx is not None
    assert state.ctx.worktree_path is None
    branch_work = ("prepare", "merge-release", "back-merge-bump", "teardown-worktree")
    with (
        patch(_MOD + ".prepare") as m_prepare,
        patch(_MOD + ".merge_release") as m_merge,
        patch(_MOD + ".back_merge_and_bump") as m_bump,
        patch(_MOD + ".teardown_worktree") as m_teardown,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
    ):
        for stage in build_stages():
            if stage.name in branch_work:
                stage.fn(state)
    m_prepare.assert_not_called()
    m_merge.assert_not_called()
    m_bump.assert_not_called()
    m_teardown.assert_not_called()
    write_state.assert_not_called()
