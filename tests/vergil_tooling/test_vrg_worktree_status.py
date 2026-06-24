"""Tests for vergil_tooling.bin.vrg_worktree_status."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_worktree_status import _format_age, _row, main
from vergil_tooling.lib.worktrees import Worktree, WorktreeState, WorktreeStatus

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_worktree_status"


def _status(
    branch: str,
    state: WorktreeState,
    *,
    pr: int | None = None,
    ahead: int = 0,
    dirty: bool = False,
    detail: str | None = None,
    workflow_status: str | None = None,
    workflow_error: str | None = None,
    pr_prepared: bool = False,
    last_commit_ts: float | None = None,
    last_modified_ts: float | None = None,
) -> WorktreeStatus:
    wt = Worktree(path=Path(f"/repo/.worktrees/{branch.replace('/', '-')}"), branch=branch)
    return WorktreeStatus(
        worktree=wt,
        state=state,
        pr_number=pr,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
        workflow_status=workflow_status,
        workflow_error=workflow_error,
        pr_prepared=pr_prepared,
        last_commit_ts=last_commit_ts,
        last_modified_ts=last_modified_ts,
    )


# Column index of WORKFLOW in a rendered row (between STATE and AHEAD).
_WORKFLOW_COL = 4
_LAST_COMMIT_COL = 7
_LAST_MODIFIED_COL = 8

_NOW = 1_700_000_000.0


def test_row_renders_loaded_workflow_status_verbatim() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1, workflow_status="approved"), _NOW)
    assert row[_WORKFLOW_COL] == "approved"


def test_row_renders_dash_when_no_workflow_file() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1), _NOW)
    assert row[_WORKFLOW_COL] == "-"


def test_row_renders_unknown_on_workflow_read_error() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1, workflow_error="bad json"), _NOW)
    assert row[_WORKFLOW_COL] == "unknown"


def test_main_summary_reports_prepared_count(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status(
            "feature/1-a",
            WorktreeState.NO_PR,
            ahead=1,
            workflow_status="approved",
            pr_prepared=True,
        ),
        _status("feature/2-b", WorktreeState.NO_PR, ahead=1, workflow_status="implementing"),
    ]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "WORKFLOW" in out
    assert "1 PR prepared." in out


def test_main_surfaces_workflow_read_error_note(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status("feature/3-c", WorktreeState.NO_PR, ahead=1, workflow_error="not valid JSON"),
    ]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "unknown" in out
    assert "note: feature/3-c:" in out
    assert "not valid JSON" in out
    assert "0 PR prepared." in out


def test_main_groups_cruft_last_and_summarizes(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status("feature/1470-merged", WorktreeState.MERGED, pr=1471, ahead=2),
        _status("feature/1534-open", WorktreeState.OPEN_PR, pr=1544, ahead=2),
        _status("feature/1543-nopr", WorktreeState.NO_PR, ahead=1),
    ]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("open-pr") < out.index("merged")
    assert out.index("no-pr") < out.index("merged")
    assert "1 active" in out
    assert "1 stalled (no-pr)" in out
    assert "1 cruft (removable)" in out
    assert "Run vrg-finalize-pr to clean cruft." in out


def test_main_empty_reports_none(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
    ):
        rc = main([])
    assert rc == 0
    assert "No canonical" in capsys.readouterr().out


def test_main_surfaces_unknown_detail(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/9-x", WorktreeState.UNKNOWN, detail="gh boom")]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gh boom" in out
    assert "0 cruft" in out
    assert "Run vrg-finalize-pr" not in out


def test_main_surfaces_reused_branch_detail(capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #1719: a reused-branch mismatch lands as NO_PR with a detail
    note — surfaced too, not just UNKNOWN, so the reuse never hides."""
    statuses = [
        _status(
            "feature/286-build-buckets",
            WorktreeState.NO_PR,
            pr=293,
            ahead=9,
            detail="closed PR #293 head 0ldd0cs does not match branch tip 7ead128 — reused",
        )
    ]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "note: feature/286-build-buckets:" in out
    assert "#293" in out
    assert "0 cruft" in out


def test_row_renders_relative_ages() -> None:
    row = _row(
        _status(
            "feature/1-x",
            WorktreeState.NO_PR,
            ahead=1,
            last_commit_ts=_NOW - 3 * 86400,
            last_modified_ts=_NOW - 2 * 3600,
        ),
        _NOW,
    )
    assert row[_LAST_COMMIT_COL] == "3d ago"
    assert row[_LAST_MODIFIED_COL] == "2h ago"


def test_row_renders_dash_for_missing_timestamps() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1), _NOW)
    assert row[_LAST_COMMIT_COL] == "-"
    assert row[_LAST_MODIFIED_COL] == "-"


def test_main_includes_timestamp_headers(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/1-a", WorktreeState.NO_PR, ahead=1)]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "LAST COMMIT" in out
    assert "LAST MODIFIED" in out


def test_format_age_hours() -> None:
    assert _format_age(_NOW - 2 * 3600, _NOW) == "2h ago"


def test_format_age_days() -> None:
    assert _format_age(_NOW - 3 * 86400, _NOW) == "3d ago"


def test_format_age_none_is_dash() -> None:
    assert _format_age(None, _NOW) == "-"


def test_format_age_future_clamps_to_zero() -> None:
    assert _format_age(_NOW + 5000, _NOW) == "0h ago"
