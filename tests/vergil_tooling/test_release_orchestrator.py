from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import run_release

_MOD = "vergil_tooling.lib.release.orchestrator"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_orchestrator_runs_all_phases() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".prepare") as m_prepare,
        patch(_MOD + ".merge_release") as m_merge_release,
        patch(_MOD + ".merge_bump") as m_bump,
        patch(_MOD + ".confirm_publish") as m_confirm,
        patch(_MOD + ".close_and_finalize") as m_finalize,
        patch(_MOD + ".consumer_refresh") as m_handoff,
        patch(_MOD + ".comment_phase_complete"),
    ):
        run_release(ctx)
    m_prepare.assert_called_once_with(ctx)
    m_merge_release.assert_called_once_with(ctx)
    m_bump.assert_called_once_with(ctx)
    m_confirm.assert_called_once_with(ctx)
    m_finalize.assert_called_once_with(ctx)
    m_handoff.assert_called_once_with(ctx)


def test_phase_details_includes_ctx_fields() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.release_branch = "release/2.1.0"
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    ctx.issue_url = "https://github.com/o/r/issues/42"
    details = _phase_details(ctx, "prepare")
    assert "release/2.1.0" in details
    assert "pull/100" in details
    assert "issues/42" in details

    ctx.tag = "v2.1.0"
    ctx.release_url = "https://github.com/o/r/releases/tag/v2.1.0"
    ctx.publish_run_url = "https://github.com/o/r/actions/runs/123"
    details = _phase_details(ctx, "confirm-publish")
    assert "v2.1.0" in details
    assert "runs/123" in details


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
