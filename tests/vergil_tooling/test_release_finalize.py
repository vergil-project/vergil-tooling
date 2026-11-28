from __future__ import annotations

import subprocess
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release import reload_reminder
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.finalize import (
    _build_summary,
    close_tracking,
    finalize,
    teardown_worktree,
)
from vergil_tooling.lib.release.state import initial_state

_MOD = "vergil_tooling.lib.release.finalize"
_REMINDER_MOD = "vergil_tooling.lib.release.reload_reminder"


@pytest.fixture(autouse=True)
def _no_reload_reminder() -> Iterator[None]:
    """Default every summary test to a repo without the reload flag (its root does not exist)."""
    with patch(_REMINDER_MOD + ".reload_plugins_enabled", return_value=False):
        yield


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
    ctx.release_url = "https://github.com/owner/repo/releases/tag/v2.1.0"
    ctx.cd_run_url = "https://github.com/owner/repo/actions/runs/123"
    return ctx


def test_finalize_runs_cleanup_and_leaves_the_issue_open() -> None:
    """finalize is close-finalize without the close: close-tracking closes the
    issue (spec §4.4 of epic vergil-project/.github#1440)."""
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue") as mock_close,
        patch(_MOD + ".progress.run", return_value=0) as run,
    ):
        finalize(ctx)
    run.assert_called_once()
    mock_close.assert_not_called()


def test_finalize_streams_through_progress() -> None:
    """Issue #1470: the cleanup child must not inherit the TTY — raw writes
    under the live display strand stale frames on screen. Its output streams
    through the progress session (live display + run log) instead; stdin is
    closed so the child can never block on a terminal read (issue #1448).
    --output-format plain states the rendering contract explicitly: the
    child is itself progress-aware (issue #1479) and two live displays
    cannot nest."""
    ctx = _ctx()
    with patch(_MOD + ".progress.run", return_value=0) as run:
        finalize(ctx)
    (cmd,) = run.call_args.args
    assert cmd == (
        "vrg-finalize-pr",
        "--cleanup-only",
        "--output-format",
        "plain",
    )
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL


def test_release_cleanup_call_has_no_no_wait_cd() -> None:
    """vrg-finalize-pr refuses --no-wait-cd now that not waiting on CD is its
    default (epic vergil-project/.github#1218, spec §6.5), so the release's
    cleanup call must not pass it, and must not opt into --wait-cd either:
    confirm-develop has already judged the release's develop CD run."""
    ctx = _ctx()
    with patch(_MOD + ".progress.run", return_value=0) as run:
        finalize(ctx)
    (cmd,) = run.call_args.args
    assert "--no-wait-cd" not in cmd
    assert "--wait-cd" not in cmd


def test_finalize_fails_on_finalize_error() -> None:
    ctx = _ctx()
    err = subprocess.CalledProcessError(
        1,
        ("vrg-finalize-pr", "--cleanup-only"),
        output="",
        stderr="validation failed",
    )
    with (
        patch(_MOD + ".progress.run", side_effect=err),
        pytest.raises(ReleaseError, match="vrg-finalize-pr") as excinfo,
    ):
        finalize(ctx)
    assert excinfo.value.phase == "finalize"
    assert excinfo.value.detail == "validation failed"


def test_close_tracking_posts_the_summary_and_closes() -> None:
    ctx = _ctx()
    ctx.state = replace(
        initial_state(version="2.1.0", cut="c" * 40, issues=(), without_deps=False),
        done=("release-records",),
    )
    with (
        patch(_MOD + ".close_tracking_issue") as mock_close,
        patch(_MOD + ".progress.run") as run,
    ):
        close_tracking(ctx)
    mock_close.assert_called_once_with(ctx, _build_summary(ctx))
    run.assert_not_called()
    summary = mock_close.call_args.args[1]
    assert "### Released: records\n- None: the release's PRs close no issues." in summary


def test_close_tracking_refuses_without_a_state_block() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue") as mock_close,
        pytest.raises(ReleaseError, match="no release state block"),
    ):
        close_tracking(ctx)
    mock_close.assert_not_called()


def test_build_summary_omits_none_fields() -> None:
    ctx = _ctx()
    ctx.tag = None
    ctx.release_url = None
    ctx.cd_run_url = None
    ctx.develop_cd_run_url = None
    summary = _build_summary(ctx)
    assert "Release tag" not in summary
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


def test_build_summary_includes_reload_reminder_with_flag() -> None:
    ctx = _ctx()
    with patch(_REMINDER_MOD + ".reload_plugins_enabled", return_value=True):
        summary = _build_summary(ctx)
    assert summary.endswith(reload_reminder.REMINDER)
    assert "### Reload Claude Code" in summary


def test_build_summary_omits_reload_reminder_without_flag() -> None:
    ctx = _ctx()
    with patch(_REMINDER_MOD + ".reload_plugins_enabled", return_value=False):
        summary = _build_summary(ctx)
    assert "Reload Claude Code" not in summary
    assert "/reload-plugins" not in summary


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
        lambda p, **_kw: calls.append(("remove", p)),
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
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p, **_kw: removed.append(p))
    teardown_worktree(ctx)  # returns early; no chdir, no removal
    assert removed == []


def test_teardown_worktree_with_none_succeeds_doing_nothing(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A resume past merge-release that found no worktree (#4889): the stage
    returns without error, so its tracked wrapper records it done."""
    ctx = _ctx()
    ctx.worktree_path = None
    calls: list[object] = []
    monkeypatch.setattr(_MOD + ".os.chdir", calls.append)
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p, **_kw: calls.append(p))
    teardown_worktree(ctx)
    assert calls == []
    assert ctx.worktree_path is None
    assert "No release worktree to remove." in capsys.readouterr().out


def test_teardown_worktree_skips_removal_when_already_gone(monkeypatch) -> None:
    """If the worktree dir is already gone (e.g. a prior partial run), still
    return to root but skip the removal call."""
    ctx = _ctx()
    ctx.worktree_path = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    chdirs: list[object] = []
    removed: list[object] = []
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(p))
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p, **_kw: removed.append(p))
    monkeypatch.setattr("pathlib.Path.exists", lambda self: False)
    teardown_worktree(ctx)
    assert chdirs == [ctx.repo_root]
    assert removed == []
