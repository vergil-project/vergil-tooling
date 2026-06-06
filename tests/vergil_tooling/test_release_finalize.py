from __future__ import annotations

import subprocess
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


def test_close_and_finalize_streams_cleanup_only() -> None:
    """Issue #1448: invoke the non-interactive flag, stream stdout to the
    user, never hand the child the TTY stdin, and capture only stderr
    (for ReleaseError.detail)."""
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=0, stderr=""),
        ) as run,
    ):
        close_and_finalize(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-finalize-pr", "--cleanup-only")
    kwargs = run.call_args.kwargs
    assert "capture_output" not in kwargs
    assert "stdout" not in kwargs  # stdout streams to the user
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.PIPE


def test_close_and_finalize_relays_child_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Captured child stderr is replayed on success so warnings are
    never silently swallowed."""
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=0, stderr="WARNING: x"),
        ),
    ):
        close_and_finalize(ctx)
    assert "WARNING: x" in capsys.readouterr().err


def test_build_summary_omits_none_fields() -> None:
    ctx = _ctx()
    ctx.tag = None
    ctx.develop_tag = None
    ctx.release_url = None
    ctx.cd_run_url = None
    ctx.develop_cd_run_url = None
    summary = _build_summary(ctx)
    assert "Release tag" not in summary
    assert "Develop boundary tag" not in summary
    assert "GitHub Release" not in summary
    assert "CD workflow" not in summary


def test_build_summary_includes_develop_cd() -> None:
    ctx = _ctx()
    ctx.develop_cd_run_url = "https://github.com/owner/repo/actions/runs/456"
    summary = _build_summary(ctx)
    assert "Develop CD" in summary
    assert "runs/456" in summary


def test_build_summary_labels_back_merge_pr() -> None:
    ctx = _ctx()
    summary = _build_summary(ctx)
    assert "Back-merge PR" in summary


def test_close_and_finalize_fails_on_finalize_error() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=1, stderr="validation failed"),
        ),
        pytest.raises(ReleaseError, match="vrg-finalize-pr") as excinfo,
    ):
        close_and_finalize(ctx)
    assert excinfo.value.detail == "validation failed"
