from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import (
    ReleaseState,
    _preflight_stage,
    _publish_status_stage,
    _tracked,
    build_stages,
)

_MOD = "vergil_tooling.lib.release.orchestrator"


def _ctx(*, promote: bool = True) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
        promote=promote,
    )
    ctx.issue_number = 42
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
        "promote",
        "close-finalize",
        "consumer-refresh",
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
    }


def test_teardown_stage_runs_after_back_merge_and_defers() -> None:
    """teardown-worktree must sit between back-merge-bump and confirm-develop,
    so the worktree is gone before close-finalize runs vrg-finalize-pr from the
    main worktree (#1578). It is fail_defer: a teardown hiccup must not abort a
    release whose branch work already succeeded."""
    stages = build_stages()
    names = [s.name for s in stages]
    teardown = stages[names.index("teardown-worktree")]
    assert names.index("back-merge-bump") < names.index("teardown-worktree")
    assert names.index("teardown-worktree") < names.index("confirm-develop")
    assert names.index("teardown-worktree") < names.index("close-finalize")
    assert teardown.mode == "fail_defer"


def test_teardown_stage_calls_teardown_worktree() -> None:
    from vergil_tooling.lib.release.orchestrator import _teardown_stage

    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    state.ctx = _ctx()
    with patch(_MOD + ".teardown_worktree") as m_teardown:
        _teardown_stage(state)
    m_teardown.assert_called_once_with(state.ctx)


def test_teardown_stage_noop_without_ctx() -> None:
    from vergil_tooling.lib.release.orchestrator import _teardown_stage

    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    with patch(_MOD + ".teardown_worktree") as m_teardown:
        _teardown_stage(state)
    m_teardown.assert_not_called()


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
    )
    assert state.ctx is ctx
    assert ctx.promote is False


def test_audit_stage_runs_audit() -> None:
    from vergil_tooling.lib.release.orchestrator import _audit_stage

    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    with patch(_MOD + ".run_audit") as m_audit:
        _audit_stage(state)
    m_audit.assert_called_once_with()


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


def test_tracked_stage_requires_ctx() -> None:
    state = ReleaseState(version_override=None, repo_root=Path("/tmp/repo"), promote=True)  # noqa: S108
    with pytest.raises(ReleaseError, match="preflight did not run"):
        _tracked("prepare", MagicMock())(state)


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


def test_phase_details_close_finalize() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "close-finalize")
    assert "finalized" in details.lower()


def test_phase_details_consumer_refresh() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "consumer-refresh")
    assert "Consumer refresh" in details


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
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".wait_and_merge") as m_wm,
    ):
        merge_release(ctx)
    m_wm.assert_called_once_with(
        "https://github.com/o/r/pull/100",
        phase="merge-release",
    )
    assert ctx.release_merge_sha == "merged"


def test_merge_release_skips_when_already_merged() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    with (
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
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
    return state


def test_publish_status_raises_when_deferred() -> None:
    state = _state()
    assert state.ctx is not None
    state.ctx.deferred_publish_failures = ["docker-publish"]
    with pytest.raises(ReleaseError, match="docker-publish"):
        _publish_status_stage(state)


def test_publish_status_noop_when_clean() -> None:
    state = _state()
    _publish_status_stage(state)  # no raise


def test_publish_status_is_terminal_fail_defer() -> None:
    stages = build_stages()
    assert stages[-1].name == "publish-status"
    assert stages[-1].mode == "fail_defer"
