from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.finalize import (
    _build_summary,
    close_and_finalize,
    teardown_worktree,
)

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
        patch(_MOD + ".progress.run", return_value=0),
    ):
        close_and_finalize(ctx)
    mock_close.assert_called_once()


def test_close_and_finalize_skips_when_deferred_failures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = _ctx()
    ctx.deferred_failures = ["promote"]
    with (
        patch(_MOD + ".close_tracking_issue") as mock_close,
        patch(_MOD + ".progress.run") as mock_run,
    ):
        close_and_finalize(ctx)
    mock_close.assert_not_called()
    mock_run.assert_not_called()
    out = capsys.readouterr().out
    assert "open" in out
    assert "promote" in out


def test_close_and_finalize_streams_through_progress() -> None:
    """Issue #1470: the cleanup child must not inherit the TTY — raw writes
    under the live display strand stale frames on screen. Its output streams
    through the progress session (live display + run log) instead; stdin is
    closed so the child can never block on a terminal read (issue #1448).
    --output-format plain states the rendering contract explicitly: the
    child is itself progress-aware (issue #1479) and two live displays
    cannot nest."""
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(_MOD + ".progress.run", return_value=0) as run,
    ):
        close_and_finalize(ctx)
    (cmd,) = run.call_args.args
    assert cmd == ("vrg-finalize-pr", "--cleanup-only", "--output-format", "plain")
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


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


def test_teardown_worktree_chdirs_root_and_removes(monkeypatch) -> None:
    """teardown returns cwd to the root checkout, then removes the worktree —
    the chdir must precede the removal so vrg-finalize-pr later runs from the
    main worktree even if removal hiccups (#1578)."""
    ctx = _ctx()
    ctx.worktree_path = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: calls.append(("chdir", p)))
    monkeypatch.setattr(
        _MOD + ".remove_worktree",
        lambda p: calls.append(("remove", p)),
    )
    monkeypatch.setattr("pathlib.Path.exists", lambda self: True)
    teardown_worktree(ctx)
    assert calls == [
        ("chdir", ctx.repo_root),
        ("remove", Path("/tmp/repo/.worktrees/release-2.1.0")),  # noqa: S108
    ]
    assert ctx.worktree_path is None


def test_teardown_worktree_noop_without_path(monkeypatch) -> None:
    ctx = _ctx()
    ctx.worktree_path = None
    removed: list[object] = []
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: removed.append(p))
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p: removed.append(p))
    teardown_worktree(ctx)  # returns early; no chdir, no removal
    assert removed == []


def test_teardown_worktree_skips_removal_when_already_gone(monkeypatch) -> None:
    """If the worktree dir is already gone (e.g. a prior partial run), still
    return to root but skip the removal call."""
    ctx = _ctx()
    ctx.worktree_path = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    chdirs: list[object] = []
    removed: list[object] = []
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(p))
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p: removed.append(p))
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    teardown_worktree(ctx)
    assert chdirs == [ctx.repo_root]
    assert removed == []


def test_close_and_finalize_fails_on_finalize_error() -> None:
    ctx = _ctx()
    err = subprocess.CalledProcessError(
        1,
        ("vrg-finalize-pr", "--cleanup-only"),
        output="",
        stderr="validation failed",
    )
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(_MOD + ".progress.run", side_effect=err),
        pytest.raises(ReleaseError, match="vrg-finalize-pr") as excinfo,
    ):
        close_and_finalize(ctx)
    assert excinfo.value.detail == "validation failed"


def test_close_and_finalize_publish_deferred_leaves_open() -> None:
    from unittest.mock import patch

    from vergil_tooling.lib.release import finalize

    ctx = _ctx()
    ctx.deferred_publish_failures = ["docker-publish"]
    with (
        patch.object(finalize, "comment_publish_deferred") as comment,
        patch.object(finalize, "close_tracking_issue") as close,
        patch.object(finalize, "progress"),
    ):
        finalize.close_and_finalize(ctx)
    comment.assert_called_once()
    close.assert_not_called()  # issue stays open


def test_close_and_finalize_clean_closes_issue() -> None:
    from unittest.mock import patch

    from vergil_tooling.lib.release import finalize

    ctx = _ctx()
    with (
        patch.object(finalize, "comment_publish_deferred") as comment,
        patch.object(finalize, "close_tracking_issue") as close,
        patch.object(finalize, "progress"),
    ):
        finalize.close_and_finalize(ctx)
    close.assert_called_once()
    comment.assert_not_called()


def test_close_and_finalize_stage_failure_short_circuits() -> None:
    from unittest.mock import patch

    from vergil_tooling.lib.release import finalize

    ctx = _ctx()
    ctx.deferred_failures = ["confirm-main"]
    with (
        patch.object(finalize, "close_tracking_issue") as close,
        patch.object(finalize, "progress") as prog,
    ):
        finalize.close_and_finalize(ctx)
    close.assert_not_called()
    prog.run.assert_not_called()  # cleanup skipped — resumable
