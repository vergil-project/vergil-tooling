from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.finalize import _build_summary, close_and_finalize

_MOD = "vergil_tooling.lib.release.finalize"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    ctx.issue_url = "https://github.com/owner/repo/issues/42"
    ctx.release_pr_url = "https://github.com/owner/repo/pull/100"
    ctx.bump_pr_url = "https://github.com/owner/repo/pull/101"
    ctx.tag = "v2.1.0"
    ctx.develop_tag = "develop-v2.1.0"
    ctx.release_url = "https://github.com/owner/repo/releases/tag/v2.1.0"
    ctx.cd_run_url = "https://github.com/owner/repo/actions/runs/123"
    return ctx


def test_close_and_finalize_succeeds() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue") as mock_close,
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=0),
        ),
    ):
        close_and_finalize(ctx)
    mock_close.assert_called_once()


def test_close_and_finalize_prints_stdout() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=0, stdout="cleaned up"),
        ),
    ):
        close_and_finalize(ctx)


def test_build_summary_omits_none_fields() -> None:
    ctx = _ctx()
    ctx.tag = None
    ctx.develop_tag = None
    ctx.release_url = None
    ctx.cd_run_url = None
    summary = _build_summary(ctx)
    assert "Release tag" not in summary
    assert "Develop boundary tag" not in summary
    assert "GitHub Release" not in summary
    assert "CD workflow" not in summary


def test_close_and_finalize_fails_on_finalize_error() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=1, stderr="validation failed"),
        ),
        pytest.raises(ReleaseError, match="vrg-finalize-repo"),
    ):
        close_and_finalize(ctx)
