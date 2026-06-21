"""Tests for vergil_tooling.bin.vrg_worktree_status."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_worktree_status import main
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
) -> WorktreeStatus:
    wt = Worktree(path=Path(f"/repo/.worktrees/{branch.replace('/', '-')}"), branch=branch)
    return WorktreeStatus(
        worktree=wt, state=state, pr_number=pr, ahead=ahead, dirty=dirty, detail=detail
    )


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
