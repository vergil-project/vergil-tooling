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
_TRACKING = "vergil_tooling.lib.release.tracking"
_FINALIZE = "vergil_tooling.lib.release.finalize"


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


def _ctx(*, promote: bool = True) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
        promote=promote,
    )
    ctx.issue_number = 42
    ctx.state = _initial("2.1.0")
    return ctx


def test_build_stages_order_and_modes() -> None:
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
        "cd-publish",
        "package-index",
        "promote",
        "close-finalize",
        "update-deps",
        "publish-status",
        "consumer-refresh",
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
    worktree is gone before close-finalize runs vrg-finalize-pr from the main
    worktree (#1578). It is fail_fast, as every branch-work stage is: finalize
    prunes the release branches only once all of them have succeeded (spec §4.3
    of epic vergil-project/.github#1440)."""
    stages = build_stages()
    names = [s.name for s in stages]
    teardown = stages[names.index("teardown-worktree")]
    assert names.index("back-merge-bump") < names.index("teardown-worktree")
    assert names.index("teardown-worktree") < names.index("confirm-develop")
    assert names.index("teardown-worktree") < names.index("close-finalize")
    assert teardown.mode == "fail_fast"


def test_teardown_stage_is_tracked_and_calls_teardown_worktree() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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
    """A failed teardown stops the run: confirm-develop and close-finalize never run."""
    state = ReleaseState(version_override=None, repo_root=tmp_path, promote=True)
    state.ctx = _ctx()
    args = argparse.Namespace(output_format="plain", output_window=None)
    names = ("teardown-worktree", "confirm-develop", "close-finalize")
    with (
        patch(_MOD + ".teardown_worktree", side_effect=OSError("busy")),
        patch(_MOD + ".confirm_develop") as m_develop,
        patch(_MOD + ".close_and_finalize") as m_finalize,
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
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=False)  # noqa: S108
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
    )
    assert state.ctx is ctx
    assert ctx.promote is False


def test_preflight_stage_renders_the_checklist_the_issue_is_created_with() -> None:
    # #4367: the stages before prepare are ticked; prepare onward are open.
    from vergil_tooling.lib.release import checklist

    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    ctx = _ctx()
    with patch(_MOD + ".preflight", return_value=ctx):
        _preflight_stage(state)
    assert ctx.progress_block is not None
    names = [stage.name for stage in build_stages()]
    pairs = checklist.parse(ctx.progress_block)
    assert [name for name, _ in pairs] == names
    assert [name for name, done in pairs if done] == ["audit", "preflight"]


def test_preflight_stage_passes_allow_pending_deps() -> None:
    state = ReleaseState(
        version_override=None,
        repo_root=Path("/tmp/repo"),  # noqa: S108
        promote=True,
        allow_pending_deps=True,
    )
    with patch(_MOD + ".preflight", return_value=_ctx()) as m_preflight:
        _preflight_stage(state)
    assert m_preflight.call_args.kwargs["allow_pending_deps"] is True


def test_audit_stage_runs_audit() -> None:
    from vergil_tooling.lib.release.orchestrator import _audit_stage

    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    with patch(_MOD + ".run_audit") as m_audit:
        _audit_stage(state)
    m_audit.assert_called_once_with(Path("/tmp/repo"))  # noqa: S108


def test_tracked_stage_comments_on_success() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    state.ctx = _ctx()
    with (
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".ensure_checklist") as m_ensure,
        patch(_MOD + ".tick_stage") as m_tick,
    ):
        _tracked("prepare", MagicMock())(state)
    names = m_ensure.call_args.args[1]
    checked = m_ensure.call_args.kwargs["checked"]
    assert names[: names.index("prepare")] == checked
    assert "prepare" not in checked
    m_tick.assert_called_once_with(state.ctx, "prepare")


def test_stage_names_match_pipeline_order() -> None:
    from vergil_tooling.lib.release.orchestrator import _stage_names, build_stages

    assert _stage_names() == [stage.name for stage in build_stages()]


def test_tracked_stage_comments_release_error_on_failure() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    state.ctx = _ctx()
    fn = MagicMock(side_effect=RuntimeError("boom"))
    with (
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError, match="boom"),
    ):
        _tracked("prepare", fn)(state)
    m_failed.assert_called_once()


def test_tracked_stage_wraps_a_failed_command_from_its_fields() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    with pytest.raises(ReleaseError, match="preflight did not run"):
        _tracked("prepare", MagicMock())(state)


def test_tracked_stage_publish_deferral_is_not_a_halted_stage() -> None:
    """A stage that records its own failure as a publish deferral (package-index)
    stays out of deferred_failures, so close-finalize still runs (#3557)."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    ctx = state.ctx = _ctx()

    def _defer(_ctx: ReleaseContext) -> None:
        defer_publish(ctx, "package-index")
        raise ReleaseError(phase="package-index", command="poll", message="timed out")

    with (
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError, match="timed out"),
    ):
        _tracked("package-index", _defer)(state)
    m_failed.assert_called_once()
    assert ctx.deferred_failures == []


def test_tracked_stage_wrapped_publish_deferral_is_not_a_halted_stage() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    ctx = state.ctx = _ctx()

    def _defer(_ctx: ReleaseContext) -> None:
        defer_publish(ctx, "package-index")
        raise RuntimeError("boom")

    with (
        patch(_MOD + ".comment_phase_failed"),
        pytest.raises(ReleaseError, match="boom"),
    ):
        _tracked("package-index", _defer)(state)
    assert ctx.deferred_failures == []


def test_cd_publish_failure_with_recorded_jobs_is_not_a_halted_stage() -> None:
    """cd-publish failing while the state still records failed CD jobs is a
    deferred publish, so close-finalize still finalizes (spec §4.5)."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    ctx = state.ctx = _ctx()
    defer_publish(ctx, "pypi / publish")

    def _fail(_ctx: ReleaseContext) -> None:
        raise ReleaseError(phase="cd-publish", command="cd-publish", message="still failing")

    with (
        patch(_MOD + ".comment_phase_failed"),
        pytest.raises(ReleaseError, match="still failing"),
    ):
        _tracked("cd-publish", _fail)(state)
    assert ctx.deferred_failures == []


def test_cd_publish_failure_with_nothing_recorded_is_a_halted_stage() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    ctx = state.ctx = _ctx()

    def _fail(_ctx: ReleaseContext) -> None:
        raise ReleaseError(phase="cd-publish", command="cd-publish", message="broken")

    with (
        patch(_MOD + ".comment_phase_failed"),
        pytest.raises(ReleaseError, match="broken"),
    ):
        _tracked("cd-publish", _fail)(state)
    assert ctx.deferred_failures == ["cd-publish"]


def test_promote_phase_calls_promote_when_enabled() -> None:
    from vergil_tooling.lib.release.orchestrator import _promote_phase

    ctx = _ctx(promote=True)
    with patch(_MOD + ".promote") as mock_promote:
        _promote_phase(ctx)
    mock_promote.assert_called_once_with(ctx.version)


def test_promote_phase_skips_when_disabled() -> None:
    from vergil_tooling.lib.release.orchestrator import _promote_phase

    ctx = _ctx(promote=False)
    with patch(_MOD + ".promote") as mock_promote:
        _promote_phase(ctx)
    mock_promote.assert_not_called()


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


def test_phase_details_promote_skipped() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx(promote=False)
    details = _phase_details(ctx, "promote")
    assert "skipped" in details.lower()


def test_phase_details_cd_publish_says_nothing_is_left() -> None:
    assert _phase_details(_ctx(), "cd-publish") == "No failed CD publish job is left to deliver."


def test_phase_details_promote_held_back_by_index_deferral() -> None:
    """A deferred package-index skips promote; the comment names the pending step (#4293)."""
    ctx = _ctx()
    defer_publish(ctx, "package-index")
    details = _phase_details(ctx, "promote")
    assert "Promoted" not in details
    assert "**Promote skipped**: package-index deferred" in details
    assert "`v2.1` stays on the previous release" in details
    assert "`vrg-release --resume` moves it" in details


def test_phase_details_promote_cd_deferral_still_promotes() -> None:
    """Only a package-index deferral holds promote back; a CD publish miss does not."""
    ctx = _ctx()
    defer_publish(ctx, "publish-pypi")
    assert _phase_details(ctx, "promote") == "Promoted v2.1 -> v2.1.0"


def test_phase_details_promote_no_promote_wins_over_index_deferral() -> None:
    ctx = _ctx(promote=False)
    defer_publish(ctx, "package-index")
    assert _phase_details(ctx, "promote") == "Promote skipped (--no-promote)."


def _run_promote_stage(ctx: ReleaseContext) -> MagicMock:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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


def test_promote_stage_skips_on_index_deferral(capsys: pytest.CaptureFixture[str]) -> None:
    """vX.Y stays on the previous release while the index lacks the release (#4293)."""
    ctx = _ctx()
    defer_publish(ctx, "package-index")
    _run_promote_stage(ctx).assert_not_called()
    out = capsys.readouterr().out
    assert "Skipping promote" in out
    assert "vrg-release --resume moves it" in out
    assert ctx.deferred_failures == []  # a skip, not a halted stage


def test_promote_stage_skips_with_no_promote() -> None:
    ctx = _ctx(promote=False)
    _run_promote_stage(ctx).assert_not_called()


def test_phase_details_close_finalize() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "close-finalize")
    assert details == "Tracking issue closed. Repository finalized."


def test_phase_details_close_finalize_cd_deferral_leaves_issue_open() -> None:
    """A CD publish deferral leaves the issue open; the comment says so (#3593)."""
    ctx = _ctx()
    defer_publish(ctx, "publish-pypi", "publish-docs")
    details = _phase_details(ctx, "close-finalize")
    assert "closed" not in details.lower()
    assert "left open" in details
    assert "CD publish (publish-pypi, publish-docs)" in details
    assert "package index" not in details
    assert "Repository finalized." in details


def test_phase_details_close_finalize_package_index_deferral_leaves_issue_open() -> None:
    """A package-index deferral leaves the issue open; the comment names it (#3593)."""
    ctx = _ctx()
    defer_publish(ctx, "package-index")
    details = _phase_details(ctx, "close-finalize")
    assert "closed" not in details.lower()
    assert "left open" in details
    assert "package index (package-index)" in details
    assert "CD publish" not in details
    assert "Repository finalized." in details
    # promote was held back, so the outcome names the pending promote (#4293).
    assert "Promote pending: `v2.1` was not moved" in details
    assert "`vrg-release --resume` moves it" in details


def test_phase_details_close_finalize_index_deferral_with_no_promote() -> None:
    """With --no-promote nothing was held back, so no promote is pending."""
    ctx = _ctx(promote=False)
    defer_publish(ctx, "package-index")
    details = _phase_details(ctx, "close-finalize")
    assert "left open" in details
    assert "Promote pending" not in details
    assert "vrg-promote" not in details


def test_phase_details_close_finalize_cd_deferral_names_no_pending_promote() -> None:
    ctx = _ctx()
    defer_publish(ctx, "publish-pypi")
    assert "Promote pending" not in _phase_details(ctx, "close-finalize")


def test_phase_details_close_finalize_both_deferral_kinds() -> None:
    ctx = _ctx()
    defer_publish(ctx, "publish-pypi", "package-index")
    details = _phase_details(ctx, "close-finalize")
    assert "CD publish (publish-pypi)" in details
    assert "package index (package-index)" in details


def test_phase_details_close_finalize_halted_stages_skip_close_and_cleanup() -> None:
    """Earlier failed stages leave the issue open and skip cleanup entirely."""
    ctx = _ctx()
    ctx.deferred_failures = ["promote"]
    defer_publish(ctx, "package-index")
    details = _phase_details(ctx, "close-finalize")
    assert "closed" not in details.lower()
    assert "finalized" not in details.lower()
    assert "left open" in details
    assert "promote" in details
    assert "--resume" in details


def test_phase_details_consumer_refresh_ran() -> None:
    ctx = _ctx()
    ctx.consumer_refresh_commands = "uv tool install pkg@v2.1.0"
    assert _phase_details(ctx, "consumer-refresh") == "Consumer refresh commands for v2.1.0 ran."


def test_phase_details_consumer_refresh_no_install() -> None:
    ctx = _ctx()
    ctx.consumer_refresh_commands = "uv tool install pkg@v2.1.0"
    ctx.install = False
    assert "not run (--no-install)" in _phase_details(ctx, "consumer-refresh")


def test_phase_details_consumer_refresh_none_configured() -> None:
    ctx = _ctx()
    assert "nothing to run" in _phase_details(ctx, "consumer-refresh")


def test_phase_details_consumer_refresh_held_by_a_deferred_publish() -> None:
    ctx = _ctx()
    defer_publish(ctx, "package-index")
    details = _phase_details(ctx, "consumer-refresh")
    assert "held" in details
    assert "package-index" in details


def test_phase_details_consumer_refresh_after_a_failed_stage() -> None:
    ctx = _ctx()
    ctx.consumer_refresh_commands = "uv tool install pkg@v2.1.0"
    ctx.deferred_failures = ["promote"]
    details = _phase_details(ctx, "consumer-refresh")
    assert "not run" in details
    assert "promote" in details


def test_phase_details_consumer_refresh_ran_after_only_update_deps_failed() -> None:
    """update-deps alone never holds the refresh back (issue #4828)."""
    ctx = _ctx()
    ctx.consumer_refresh_commands = "uv tool install pkg@v2.1.0"
    ctx.deferred_failures = ["update-deps"]
    assert _phase_details(ctx, "consumer-refresh") == "Consumer refresh commands for v2.1.0 ran."


def test_phase_details_consumer_refresh_names_only_blocking_stages() -> None:
    ctx = _ctx()
    ctx.consumer_refresh_commands = "uv tool install pkg@v2.1.0"
    ctx.deferred_failures = ["promote", "update-deps"]
    details = _phase_details(ctx, "consumer-refresh")
    assert details == "Consumer refresh commands shown, not run: earlier stages failed (promote)."


def test_consumer_refresh_runs_last() -> None:
    """The refresh is the release's last stage, after promote, close-finalize
    and update-deps: a consumer that follows the rolling vX.Y tag sees the
    release only once promote has moved it (#4739)."""
    stages = build_stages()
    names = [s.name for s in stages]
    assert names[-1] == "consumer-refresh"
    for earlier in ("promote", "close-finalize", "update-deps", "publish-status"):
        assert names.index(earlier) < names.index("consumer-refresh")
    assert stages[-1].mode == "fail_defer"


def _run_promote_then_refresh(
    tmp_path: Path, *, promote_error: Exception | None
) -> tuple[int, ReleaseContext, MagicMock]:
    """Run the real promote and consumer-refresh stages through the pipeline."""
    state = _state()
    ctx = state.ctx
    assert ctx is not None
    ctx.issue_number = 42
    ctx.tag = "v2.1.2"
    names = ("promote", "consumer-refresh")
    args = argparse.Namespace(output_format="plain", output_window=None)
    with (
        patch("vergil_tooling.lib.release.handoff.config.read_config") as m_config,
        patch(
            "vergil_tooling.lib.release.handoff.run_consumer_refresh", return_value=(0, "")
        ) as m_run,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
        patch(_MOD + ".promote", side_effect=promote_error),
    ):
        m_config.return_value.publish.consumer_refresh = "uv tool install pkg@v<VERSION>"
        stages = [s for s in build_stages() if s.name in names]
        assert [s.name for s in stages] == list(names)
        rc = progress.run_pipeline(
            state, stages, command="vrg-release", label="vrg-release", args=args, repo_root=tmp_path
        )
    return rc, ctx, m_run


def test_consumer_refresh_runs_after_promote(tmp_path: Path) -> None:
    rc, ctx, m_run = _run_promote_then_refresh(tmp_path, promote_error=None)
    assert rc == 0
    m_run.assert_called_once_with("uv tool install pkg@v2.1.2", cwd=ctx.repo_root)
    assert ctx.consumer_refresh_message is None


def test_consumer_refresh_not_run_after_a_failed_promote(tmp_path: Path) -> None:
    """A failed promote leaves vX.Y on the previous release, so a refresh that
    follows the rolling tag would install it: the commands are only shown, as
    the post-pipeline install never ran after a failed pipeline."""
    error = ReleaseError(phase="promote", command="git push", message="refused")
    rc, ctx, m_run = _run_promote_then_refresh(tmp_path, promote_error=error)
    assert rc == 1
    m_run.assert_not_called()
    assert ctx.deferred_failures == ["promote"]
    message = ctx.consumer_refresh_message or ""
    assert "not run: earlier stages failed: promote" in message
    assert "uv tool install pkg@v2.1.2" in message


_DEPS_FAILURE = (
    "Dependency update for 2.1.3 failed (validation): Validation failed after dependency "
    "updates. The PR stays open: https://example.test/pull/7."
)


def _fail_update_deps(ctx: ReleaseContext) -> None:
    ctx.deps_failure = _DEPS_FAILURE
    raise ReleaseError(phase="update-deps", command="vrg-update-deps", message=_DEPS_FAILURE)


def _run_deps_then_refresh(
    tmp_path: Path, *, names: tuple[str, ...], promote_error: Exception | None
) -> tuple[int, ReleaseContext, MagicMock]:
    """Run the real stages in *names* through the pipeline, update-deps failing."""
    state = _state()
    ctx = state.ctx
    assert ctx is not None
    ctx.issue_number = 42
    ctx.tag = "v2.1.2"
    args = argparse.Namespace(output_format="plain", output_window=None)
    with (
        patch("vergil_tooling.lib.release.handoff.config.read_config") as m_config,
        patch(
            "vergil_tooling.lib.release.handoff.run_consumer_refresh", return_value=(0, "")
        ) as m_run,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
        patch(_MOD + ".promote", side_effect=promote_error),
        patch(_MOD + ".update_deps", side_effect=_fail_update_deps),
    ):
        m_config.return_value.publish.consumer_refresh = "uv tool install pkg@v<VERSION>"
        stages = [s for s in build_stages() if s.name in names]
        assert [s.name for s in stages] == list(names)
        rc = progress.run_pipeline(
            state, stages, command="vrg-release", label="vrg-release", args=args, repo_root=tmp_path
        )
    return rc, ctx, m_run


def test_consumer_refresh_runs_when_only_update_deps_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """update-deps prepares the next version, so its failure alone leaves the
    published release valid: the refresh runs, and the run still fails on the
    update-deps failure, which publish-status repeats (issue #4828)."""
    rc, ctx, m_run = _run_deps_then_refresh(
        tmp_path,
        names=("update-deps", "publish-status", "consumer-refresh"),
        promote_error=None,
    )
    assert rc == 1
    assert ctx.deferred_failures == ["update-deps"]
    out = capsys.readouterr().out
    m_run.assert_called_once_with("uv tool install pkg@v2.1.2", cwd=ctx.repo_root)
    assert ctx.consumer_refresh_message is None
    assert "✗ update-deps" in out
    assert "✓ consumer-refresh" in out
    assert f"repeats the update-deps failure: {_DEPS_FAILURE}" in out


def test_consumer_refresh_not_run_after_promote_and_update_deps_failed(tmp_path: Path) -> None:
    """A failed stage the release depends on still holds the refresh back, even
    alongside an update-deps failure, and only it is named."""
    error = ReleaseError(phase="promote", command="git push", message="refused")
    rc, ctx, m_run = _run_deps_then_refresh(
        tmp_path,
        names=("promote", "update-deps", "publish-status", "consumer-refresh"),
        promote_error=error,
    )
    assert rc == 1
    assert ctx.deferred_failures == ["promote", "update-deps"]
    m_run.assert_not_called()
    message = ctx.consumer_refresh_message or ""
    assert "not run: earlier stages failed: promote)." in message


def test_package_index_then_promote_then_close_finalize() -> None:
    """package-index is a fail_defer stage before close-finalize, so an index
    miss leaves the tracking issue open with the deferral comment (#3557), and
    the later consumer refresh holds on it (epic .github#356, §8.3). promote
    sits between them, so vX.Y moves only once the index has the release
    (#4293)."""
    from vergil_tooling.lib.release.orchestrator import _stage_names

    names = _stage_names()
    assert names[names.index("package-index") + 1] == "promote"
    assert names[names.index("promote") + 1] == "close-finalize"
    assert names.index("package-index") < names.index("consumer-refresh")
    stages = build_stages()
    assert stages[names.index("package-index")].mode == "fail_defer"
    assert stages[names.index("promote")].mode == "fail_defer"


def test_package_index_stage_runs_wait_for_index_tracked() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/r"), promote=True)  # noqa: S108
    state.ctx = ReleaseContext(
        repo="o/r",
        version="2.1.2",
        repo_root=Path("/tmp/r"),  # noqa: S108
        version_override=None,
    )
    state.ctx.state = _initial("2.1.2")
    return state


def test_publish_status_raises_when_deferred() -> None:
    state = _state()
    assert state.ctx is not None
    defer_publish(state.ctx, "docker-publish")
    with pytest.raises(ReleaseError, match="docker-publish"):
        _publish_status_stage(state)


def test_publish_status_gives_per_kind_advice_for_an_index_deferral() -> None:
    state = _state()
    assert state.ctx is not None
    defer_publish(state.ctx, "package-index")
    with (
        patch(_TRACKING + "._packages_repo", return_value="o/packages"),
        pytest.raises(ReleaseError) as info,
    ):
        _publish_status_stage(state)
    msg = str(info.value)
    assert "artifact publish was deferred (package-index)" in msg
    assert "publish-index workflow in o/packages" in msg
    assert "then finish the release with vrg-release --resume" in msg
    assert "CD publish" not in msg
    assert "vrg-promote" not in msg
    assert "NOT" not in msg


def test_publish_status_noop_when_clean() -> None:
    state = _state()
    _publish_status_stage(state)  # no raise


def test_publish_status_is_fail_defer_just_before_consumer_refresh() -> None:
    stages = build_stages()
    assert stages[-2].name == "publish-status"
    assert stages[-2].mode == "fail_defer"


# --- update-deps stage (epic vergil-project/.github#381) ---------------------


def test_update_deps_stage_between_close_finalize_and_publish_status() -> None:
    stages = build_stages()
    names = [s.name for s in stages]
    assert names[-4:] == ["close-finalize", "update-deps", "publish-status", "consumer-refresh"]
    assert stages[names.index("update-deps")].mode == "fail_defer"


def test_update_deps_stage_is_tracked_and_runs_update_deps() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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


def test_publish_status_reports_deps_only_failure_as_a_warning() -> None:
    # update-deps already failed (and counted); publish-status repeats it as a
    # warning on its own row rather than a second failure (#3283).
    state = _state()
    assert state.ctx is not None
    state.ctx.deps_failure = "Dependency update for 2.1.243 failed (validation)"
    with patch(_MOD + ".mark_warn") as m_warn:
        _publish_status_stage(state)  # no raise
    reason = m_warn.call_args.args[0]
    assert "Dependency update for 2.1.243 failed (validation)" in reason
    assert "update-deps" in reason
    assert "CD publish" not in reason


def test_release_summary_counts_a_deps_only_failure_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = _state()
    deps = "Dependency update for 2.1.243 failed (validation): red."

    def _failed_update_deps(st: ReleaseState) -> None:
        assert st.ctx is not None
        st.ctx.deps_failure = deps
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
    with pytest.raises(ReleaseError) as info:
        _publish_status_stage(state)
    msg = str(info.value)
    assert "artifact publish was deferred (docs)" in msg
    assert "fix the cause of the failed CD publish jobs (the resume re-runs them)" in msg
    assert "2.1.243 failed" in msg


def test_publish_status_noop_without_ctx() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/r"), promote=True)  # noqa: S108
    _publish_status_stage(state)  # no raise


def test_package_index_success_records_index_listed() -> None:
    """A package-index that saw every package records index_listed (spec §4.5 of epic 1440)."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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
        promote=True,
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
        promote=True,
        without_deps=True,
    )
    ctx = _ctx()
    with patch(_MOD + ".preflight", return_value=ctx):
        _preflight_stage(state)
    assert ctx.without_deps is True


# --- deferred publishes through the real stage tail (#3557) -----------------

_TAIL = ("package-index", "promote", "close-finalize", "publish-status", "consumer-refresh")


def _run_tail(
    tmp_path: Path,
    *,
    cd_deferred: list[str],
    index_times_out: bool,
    promote_mock: MagicMock | None = None,
    refresh_run: MagicMock | None = None,
) -> tuple[int, ReleaseContext, MagicMock, MagicMock, MagicMock]:
    """Run the real tail stages, from package-index on, through the pipeline.

    Returns the exit code, the context, and the deferral-comment, issue-close
    and vrg-finalize-pr mocks. ``promote_mock`` stands in for the tag move and
    ``refresh_run`` for running the consumer-refresh commands.
    """
    state = _state()
    ctx = state.ctx
    assert ctx is not None
    ctx.issue_number = 42
    ctx.tag = "v2.1.2"
    defer_publish(ctx, *cd_deferred)
    timeout = ReleaseError(phase="package-index", command="poll package index", message="slow")
    args = argparse.Namespace(output_format="plain", output_window=None)
    with (
        patch(
            "vergil_tooling.lib.release.package_index._wait",
            side_effect=timeout if index_times_out else None,
        ),
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed"),
        patch(_MOD + ".ensure_checklist"),
        patch(_MOD + ".tick_stage"),
        patch(_TRACKING + "._packages_repo", return_value="o/packages"),
        patch(_FINALIZE + ".comment_publish_deferred") as m_comment,
        patch(_FINALIZE + ".close_tracking_issue") as m_close,
        patch(_FINALIZE + ".progress") as m_progress,
        patch(_MOD + ".promote", promote_mock or MagicMock()),
        patch("vergil_tooling.lib.release.handoff.config.read_config") as m_config,
        patch(
            "vergil_tooling.lib.release.handoff.run_consumer_refresh",
            refresh_run or MagicMock(return_value=(0, "")),
        ),
    ):
        m_config.return_value.publish.consumer_refresh = "uv tool install pkg@v<VERSION>"
        stages = [s for s in build_stages() if s.name in _TAIL]
        assert [s.name for s in stages] == list(_TAIL)
        rc = progress.run_pipeline(
            state, stages, command="vrg-release", label="vrg-release", args=args, repo_root=tmp_path
        )
    return rc, ctx, m_comment, m_close, m_progress.run


def _publish_status(out: str) -> str:
    """The publish-status stage's status line: the deferral remedy the run ends on."""
    return next(line for line in out.splitlines() if line.startswith("✗ publish-status"))


def test_index_only_timeout_leaves_issue_open_with_deferral_comment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    m_refresh = MagicMock(return_value=(0, ""))
    rc, ctx, m_comment, m_close, m_finalize = _run_tail(
        tmp_path, cd_deferred=[], index_times_out=True, refresh_run=m_refresh
    )
    assert rc == 1
    m_comment.assert_called_once_with(ctx, ["package-index"])
    m_close.assert_not_called()  # the tracking issue stays open
    m_finalize.assert_called_once()  # cleanup still runs: the release is complete
    assert ctx.deferred_failures == []  # not a halted, resumable stage
    message = _publish_status(capsys.readouterr().out)
    assert "publish-index workflow in o/packages" in message
    assert "CD publish" not in message
    # The consumer refresh holds on the deferral, as it did before #4739.
    m_refresh.assert_not_called()
    held = ctx.consumer_refresh_message or ""
    assert "held" in held
    assert "publish-index workflow in o/packages" in held


def test_index_timeout_skips_promote_and_names_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A deferred index leaves vX.Y on the previous release (#4293): promote never
    moves the tag, the run stays a completed-with-deferral one, and the run's
    closing deferral message tells the human to finish it with --resume."""
    m_promote = MagicMock()
    rc, ctx, m_comment, m_close, _ = _run_tail(
        tmp_path, cd_deferred=[], index_times_out=True, promote_mock=m_promote
    )
    assert rc == 1
    m_promote.assert_not_called()
    m_comment.assert_called_once_with(ctx, ["package-index"])
    m_close.assert_not_called()
    assert ctx.deferred_failures == []
    assert "then finish the release with vrg-release --resume" in _publish_status(
        capsys.readouterr().out
    )


def test_index_found_promotes_after_it(tmp_path: Path) -> None:
    """With the index found, promote moves the tag before close-finalize closes the issue."""
    m_promote = MagicMock()
    m_refresh = MagicMock(return_value=(0, ""))
    rc, ctx, m_comment, m_close, _ = _run_tail(
        tmp_path,
        cd_deferred=[],
        index_times_out=False,
        promote_mock=m_promote,
        refresh_run=m_refresh,
    )
    assert rc == 0
    assert ctx.deferred_publish_failures == []
    m_promote.assert_called_once_with("2.1.2")
    m_comment.assert_not_called()
    m_close.assert_called_once()
    m_refresh.assert_called_once_with("uv tool install pkg@v2.1.2", cwd=ctx.repo_root)
    assert ctx.consumer_refresh_message is None


def test_cd_only_deferral_leaves_issue_open_with_cd_advice(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, ctx, m_comment, m_close, m_finalize = _run_tail(
        tmp_path, cd_deferred=["docker-publish"], index_times_out=False
    )
    assert rc == 1
    m_comment.assert_called_once_with(ctx, ["docker-publish"])
    m_close.assert_not_called()
    m_finalize.assert_called_once()
    message = _publish_status(capsys.readouterr().out)
    assert "the resume re-runs them" in message
    assert "publish-index" not in message
    assert "held" in (ctx.consumer_refresh_message or "")


def test_cd_and_index_deferral_together_give_both_remedies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, ctx, m_comment, m_close, _ = _run_tail(
        tmp_path, cd_deferred=["docker-publish"], index_times_out=True
    )
    assert rc == 1
    m_comment.assert_called_once_with(ctx, ["docker-publish", "package-index"])
    m_close.assert_not_called()
    message = _publish_status(capsys.readouterr().out)
    assert "the resume re-runs them" in message
    assert "publish-index workflow in o/packages" in message


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
    "close-finalize",
)
_PREFLIGHT = "vergil_tooling.lib.release.preflight"


def _tracked_run(name: str, ctx: ReleaseContext, fn: MagicMock) -> MagicMock:
    """Run *fn* as tracked stage *name* on *ctx*; return the tick-stage mock."""
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
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


@pytest.mark.parametrize(
    ("name", "setup"),
    [
        ("promote", lambda c: setattr(c, "promote", False)),
        ("promote", lambda c: defer_publish(c, "package-index")),
        ("close-finalize", lambda c: c.deferred_failures.append("confirm-develop")),
        ("close-finalize", lambda c: defer_publish(c, "docs")),
        ("consumer-refresh", lambda c: defer_publish(c, "docs")),
        ("consumer-refresh", lambda c: setattr(c, "install", False)),
        ("consumer-refresh", lambda c: c.deferred_failures.append("promote")),
    ],
)
def test_held_stage_is_not_recorded_done(
    write_state: MagicMock, name: str, setup: Callable[[ReleaseContext], None]
) -> None:
    """A stage that returned without doing its work runs again on a resume."""
    ctx = _ctx()
    setup(ctx)
    ctx.consumer_refresh_commands = "vrg-tooling-install v2.1.0"
    _tracked_run(name, ctx, MagicMock())
    write_state.assert_not_called()


def test_consumer_refresh_with_none_configured_is_done(write_state: MagicMock) -> None:
    ctx = _ctx()
    ctx.consumer_refresh_commands = None
    _tracked_run("consumer-refresh", ctx, MagicMock())
    assert ctx.state is not None
    assert ctx.state.done == ("consumer-refresh",)


def test_resume_after_finalize_skips_branch_stages(write_state: MagicMock) -> None:
    """A resume after finalize creates no worktree and never re-enters prepare."""
    current = _initial("2.1.0", done=_DONE_THROUGH_FINALIZE)
    state = ReleaseState(
        version_override=None,
        repo_root=Path("/tmp/repo"),  # noqa: S108
        promote=True,
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
