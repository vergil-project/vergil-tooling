"""Tests for vergil_tooling.lib.worktrees."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.worktrees import (
    Worktree,
    WorktreeState,
    WorktreeStatus,
    classify_worktree,
    gather_worktree_status,
    list_worktrees,
    require_tty,
    select_worktree,
    worktree_for_branch,
)

_MOD = "vergil_tooling.lib.worktrees"

_PORCELAIN = """\
worktree /repo
HEAD 1111111111111111111111111111111111111111
branch refs/heads/develop

worktree /repo/.worktrees/issue-7-foo
HEAD 2222222222222222222222222222222222222222
branch refs/heads/feature/7-foo

worktree /elsewhere/rogue
HEAD 3333333333333333333333333333333333333333
branch refs/heads/feature/9-rogue

worktree /repo/.worktrees/issue-8-bar
HEAD 4444444444444444444444444444444444444444
branch refs/heads/feature/8-bar
"""


def test_list_worktrees_filters_to_canonical_container() -> None:
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN):
        result = list_worktrees(Path("/repo"))
    assert result == [
        Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo"),
        Worktree(path=Path("/repo/.worktrees/issue-8-bar"), branch="feature/8-bar"),
    ]


def test_list_worktrees_ignores_detached_worktrees() -> None:
    porcelain = "worktree /repo/.worktrees/issue-5-x\nHEAD 5555\ndetached\n"
    with patch(_MOD + ".git.read_output", return_value=porcelain):
        assert list_worktrees(Path("/repo")) == []


def test_worktree_for_branch_found() -> None:
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN):
        path = worktree_for_branch("feature/8-bar", Path("/repo"))
    assert path == Path("/repo/.worktrees/issue-8-bar")


def test_worktree_for_branch_none_when_absent() -> None:
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN):
        assert worktree_for_branch("feature/missing", Path("/repo")) is None


def test_worktree_for_branch_ignores_non_canonical() -> None:
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN):
        assert worktree_for_branch("feature/9-rogue", Path("/repo")) is None


def test_require_tty_passes_on_tty() -> None:
    with (
        patch(_MOD + ".sys.stdin") as stdin,
        patch(_MOD + ".sys.stdout") as stdout,
    ):
        stdin.isatty.return_value = True
        stdout.isatty.return_value = True
        require_tty("test context")  # no raise


def test_require_tty_fails_fast_on_non_tty() -> None:
    with patch(_MOD + ".sys.stdin") as stdin:
        stdin.isatty.return_value = False
        with pytest.raises(SystemExit, match="interactive terminal"):
            require_tty("test context")


def test_require_tty_fails_fast_on_non_tty_stdout() -> None:
    """Issue #1448: when stdout is captured, prompts are written into the
    void — a TTY stdin alone must not pass the guard."""
    with (
        patch(_MOD + ".sys.stdin") as stdin,
        patch(_MOD + ".sys.stdout") as stdout,
    ):
        stdin.isatty.return_value = True
        stdout.isatty.return_value = False
        with pytest.raises(SystemExit, match="interactive terminal"):
            require_tty("test context")


def test_select_worktree_single_candidate_no_prompt() -> None:
    wt = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
    with patch(_MOD + ".prompt_choice") as choice:
        result = select_worktree([wt], purpose="Pick one", labels=["foo"])
    assert result is wt
    choice.assert_not_called()


def test_select_worktree_multiple_candidates_prompts() -> None:
    wts = [
        Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo"),
        Worktree(path=Path("/repo/.worktrees/issue-8-bar"), branch="feature/8-bar"),
    ]
    with (
        patch(_MOD + ".require_tty"),
        patch(_MOD + ".prompt_choice", return_value="bar label") as choice,
    ):
        result = select_worktree(wts, purpose="Pick one", labels=["foo label", "bar label"])
    assert result is wts[1]
    choice.assert_called_once_with("Pick one", ["foo label", "bar label"])


def test_select_worktree_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        select_worktree([], purpose="Pick one", labels=[])


# -- classify_worktree (pure) ------------------------------------------------

_SAMPLE_WT = Worktree(path=Path("/repo/.worktrees/issue-1-x"), branch="feature/1-x")


def _classify(
    *,
    pr_number: int | None = None,
    pr_state: str | None = None,
    pr_lookup_failed: bool = False,
    ahead: int = 0,
    dirty: bool = False,
    detail: str | None = None,
) -> WorktreeStatus:
    return classify_worktree(
        _SAMPLE_WT,
        pr_number=pr_number,
        pr_state=pr_state,
        pr_lookup_failed=pr_lookup_failed,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
    )


def test_classify_open_pr_not_removable() -> None:
    status = _classify(pr_number=10, pr_state="OPEN", ahead=2)
    assert status.state is WorktreeState.OPEN_PR
    assert status.removable is False


def test_classify_merged_is_removable() -> None:
    status = _classify(pr_number=11, pr_state="MERGED", ahead=2)
    assert status.state is WorktreeState.MERGED
    assert status.removable is True


def test_classify_closed_is_removable() -> None:
    status = _classify(pr_number=12, pr_state="CLOSED", ahead=1)
    assert status.state is WorktreeState.CLOSED
    assert status.removable is True


def test_classify_no_pr_with_commits_is_stalled() -> None:
    status = _classify(ahead=1)
    assert status.state is WorktreeState.NO_PR
    assert status.removable is False


def test_classify_no_pr_zero_commits_is_draft() -> None:
    status = _classify(ahead=0)
    assert status.state is WorktreeState.DRAFT


def test_classify_dirty_merged_is_not_removable() -> None:
    status = _classify(pr_number=11, pr_state="MERGED", ahead=2, dirty=True)
    assert status.state is WorktreeState.MERGED
    assert status.dirty is True
    assert status.removable is False


def test_classify_lookup_failure_is_unknown() -> None:
    status = _classify(pr_lookup_failed=True, detail="gh exploded")
    assert status.state is WorktreeState.UNKNOWN
    assert status.removable is False
    assert status.detail == "gh exploded"


# -- gather_worktree_status (I/O wrapper) ------------------------------------


def test_gather_open_pr_short_circuits_closed_lookup() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=2),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"number": "10", "url": "", "title": "t"},
        ),
        patch(_MOD + ".github.closed_pr_for_branch") as closed,
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.OPEN_PR
    assert status.pr_number == 10
    closed.assert_not_called()


def test_gather_merged_pr_is_removable() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.MERGED
    assert status.pr_number == 11
    assert status.removable is True


def test_gather_dirty_overlay_blocks_removal() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=" M file.py"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.dirty is True
    assert status.removable is False


def test_gather_no_pr_with_commits_is_stalled() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=3),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.closed_pr_for_branch", return_value=None),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.NO_PR
    assert status.pr_number is None
    assert status.removable is False


def test_gather_pr_lookup_failure_is_unknown() -> None:
    err = subprocess.CalledProcessError(1, ["gh"], stderr="boom")
    with (
        patch(_MOD + ".git.commits_ahead", return_value=0),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".github.pr_for_branch", side_effect=err),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.UNKNOWN
    assert "boom" in (status.detail or "")
