from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import run_release

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


def test_orchestrator_runs_all_phases() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".prepare") as m_prepare,
        patch(_MOD + ".merge_release") as m_merge_release,
        patch(_MOD + ".confirm_main") as m_confirm_main,
        patch(_MOD + ".back_merge_and_bump") as m_bump,
        patch(_MOD + ".confirm_develop") as m_confirm_develop,
        patch(_MOD + "._promote_phase") as m_promote,
        patch(_MOD + ".close_and_finalize") as m_finalize,
        patch(_MOD + ".consumer_refresh") as m_handoff,
        patch(_MOD + ".comment_phase_complete"),
    ):
        run_release(ctx)
    m_prepare.assert_called_once_with(ctx)
    m_merge_release.assert_called_once_with(ctx)
    m_confirm_main.assert_called_once_with(ctx)
    m_bump.assert_called_once_with(ctx)
    m_confirm_develop.assert_called_once_with(ctx)
    m_promote.assert_called_once_with(ctx)
    m_finalize.assert_called_once_with(ctx)
    m_handoff.assert_called_once_with(ctx)


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


def test_format_elapsed_minutes() -> None:
    from vergil_tooling.lib.release.orchestrator import _format_elapsed

    assert _format_elapsed(90) == "1m30s"
    assert _format_elapsed(125) == "2m05s"


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


def test_orchestrator_stops_on_failure_and_comments() -> None:
    ctx = _ctx()
    exc = ReleaseError(
        phase="merge-release",
        command="gh pr merge",
        message="CI failed",
    )
    with (
        patch(_MOD + ".prepare"),
        patch(_MOD + ".merge_release", side_effect=exc),
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError),
    ):
        run_release(ctx)
    m_failed.assert_called_once_with(ctx, "merge-release", exc)


def test_orchestrator_wraps_non_release_error() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".prepare",
            side_effect=subprocess.CalledProcessError(1, "git push"),
        ),
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError),
    ):
        run_release(ctx)
    wrapped = m_failed.call_args[0][2]
    assert wrapped.phase == "prepare"
    assert "git push" in wrapped.command


def test_orchestrator_does_not_run_later_phases_on_failure() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".prepare",
            side_effect=ReleaseError(
                phase="prepare",
                command="git checkout",
                message="branch exists",
            ),
        ),
        patch(_MOD + ".merge_release") as m_merge,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed"),
        pytest.raises(ReleaseError),
    ):
        run_release(ctx)
    m_merge.assert_not_called()


def test_merge_release_raises_if_no_pr_url() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    with pytest.raises(ReleaseError, match="release_pr_url is not set"):
        merge_release(ctx)


def test_merge_release_calls_wait_and_merge() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    with patch(_MOD + ".wait_and_merge") as m_wm:
        merge_release(ctx)
    m_wm.assert_called_once_with(
        "https://github.com/o/r/pull/100",
        phase="merge-release",
    )
    assert ctx.release_merge_sha == "merged"


def test_comment_failure_raises_with_comment_phase() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".prepare"),
        patch(
            _MOD + ".comment_phase_complete",
            side_effect=Exception("GitHub 502"),
        ),
        pytest.raises(ReleaseError) as exc_info,
    ):
        run_release(ctx)
    assert exc_info.value.phase == "comment(prepare)"
    assert exc_info.value.command == "comment_phase_complete"
