"""Tests for vergil_tooling.lib.worktrees."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib import progress
from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.finalize import teardown_worktree
from vergil_tooling.lib.release_lock.model import LockRecord, RefReadError
from vergil_tooling.lib.release_lock.refs import LockState
from vergil_tooling.lib.trust import PRRef, Role, TrustReadError
from vergil_tooling.lib.trusted_authors import TrustedAuthors, TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.worktrees import (
    MainCheckoutStatus,
    ProtectionReadError,
    RawWorktree,
    ReleaseProtection,
    Worktree,
    WorktreeKind,
    WorktreeState,
    WorktreeStatus,
    _branch_from_worktree_name,
    _newest_mtime,
    _probe_pr_workflow,
    _rebase_head_name,
    classify_worktree,
    closed_pr_for_branch,
    discover_repo_worktrees,
    discover_worktrees,
    gather_detached_status,
    gather_main_checkout_status,
    gather_worktree_status,
    gone_branches,
    is_canonical_worktree,
    list_worktrees,
    lock_protection,
    log_removal,
    match_worktrees,
    release_protection,
    require_tty,
    select_worktree,
    select_worktrees,
    worktree_for_branch,
    worktree_kind,
    worktree_still_present,
)

_MOD = "vergil_tooling.lib.worktrees"


def _pr_ref(number: int, title: str, *, state: str = "OPEN", head_oid: str = "a" * 40) -> PRRef:
    """A same-repo trusted PR, as ``trust.prs_for_branch`` returns it."""
    return PRRef(
        number=number,
        url=f"https://github.com/o/r/pull/{number}",
        title=title,
        head="feature/7-foo",
        head_oid=head_oid,
        base="develop",
        author_role=Role.HUMAN,
        state=state,
    )


def test_gather_populates_timestamps() -> None:
    wt = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
    with (
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".branch_prs", return_value=[]),
        patch(_MOD + ".git.committer_timestamp", return_value=1_699_900_000),
        patch(_MOD + "._newest_mtime", return_value=1_699_999_999.0),
    ):
        status = gather_worktree_status(wt, target="develop", with_freshness=True)
    assert status.last_commit_ts == 1_699_900_000
    assert status.last_modified_ts == 1_699_999_999.0


def test_gather_no_commits_ahead_shows_no_last_commit() -> None:
    """A branch with no commits beyond its base (AHEAD 0) has no LAST COMMIT
    of its own: HEAD is the base commit, so its age is not shown (#3828)."""
    wt = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
    with (
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.commits_ahead", return_value=0),
        patch(_MOD + ".branch_prs", return_value=[]),
        patch(_MOD + ".git.committer_timestamp", return_value=1_699_900_000),
        patch(_MOD + "._newest_mtime", return_value=1_699_999_999.0),
    ):
        status = gather_worktree_status(wt, target="develop", with_freshness=True)
    assert status.ahead == 0
    assert status.last_commit_ts is None
    assert status.last_modified_ts == 1_699_999_999.0


def test_gather_without_freshness_leaves_timestamps_none() -> None:
    wt = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
    with (
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".branch_prs", return_value=[]),
        patch(_MOD + ".git.committer_timestamp") as commit_ts,
        patch(_MOD + "._newest_mtime") as newest,
    ):
        status = gather_worktree_status(wt, target="develop")
    assert status.last_commit_ts is None
    assert status.last_modified_ts is None
    commit_ts.assert_not_called()
    newest.assert_not_called()


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


_PORCELAIN_WITH_DETACHED = """\
worktree /repo
HEAD 1111111111111111111111111111111111111111
branch refs/heads/develop

worktree /repo/.worktrees/issue-7-foo
HEAD 2222222222222222222222222222222222222222
branch refs/heads/feature/7-foo

worktree /repo/.worktrees/issue-9-mid-rebase
HEAD 3333333333333333333333333333333333333333
detached
"""


def test_discover_worktrees_keeps_detached() -> None:
    """Issue #2634: a detached-HEAD canonical worktree (paused mid-rebase) must
    survive discovery with branch=None, not be dropped like list_worktrees drops
    it."""
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN_WITH_DETACHED):
        result = discover_worktrees(Path("/repo"))
    assert result == [
        RawWorktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo"),
        RawWorktree(path=Path("/repo/.worktrees/issue-9-mid-rebase"), branch=None),
    ]


def test_list_worktrees_filters_detached_out_of_discovery() -> None:
    """list_worktrees still excludes the detached worktree discovery keeps, so
    the finalize/submit sweeps never act on a mid-operation worktree."""
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN_WITH_DETACHED):
        assert list_worktrees(Path("/repo")) == [
            Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo"),
        ]


def test_branch_from_worktree_name_maps_canonical() -> None:
    assert _branch_from_worktree_name("issue-2607-resume-attach") == "feature/2607-resume-attach"


def test_branch_from_worktree_name_none_for_non_canonical() -> None:
    assert _branch_from_worktree_name("scratch") is None
    assert _branch_from_worktree_name("issue-") is None


def test_rebase_head_name_empty_file_falls_through(tmp_path: Path) -> None:
    """An empty head-name is treated as absent — skipped, never returned as an
    empty branch name."""
    (tmp_path / "rebase-merge").mkdir()
    (tmp_path / "rebase-merge" / "head-name").write_text("\n")
    assert _rebase_head_name(tmp_path) is None


def test_gather_detached_status_rebasing_resolves_branch(tmp_path: Path) -> None:
    """A mid-rebase worktree resolves REBASING and recovers its branch from the
    paused rebase's head-name metadata (issue #2634)."""
    git_dir = tmp_path / "gitdir"
    (git_dir / "rebase-merge").mkdir(parents=True)
    (git_dir / "rebase-merge" / "head-name").write_text("refs/heads/feature/2607-resume-attach\n")
    raw = RawWorktree(path=Path("/repo/.worktrees/issue-2607-resume-attach"), branch=None)
    with (
        patch(_MOD + ".git.worktree_git_dir", return_value=git_dir),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".git.commits_ahead", return_value=3),
        patch(_MOD + "._probe_pr_workflow", return_value=(None, None, False)),
        patch(_MOD + ".branch_prs", return_value=[]),
    ):
        status = gather_detached_status(raw, target="develop")
    assert status.state is WorktreeState.REBASING
    assert status.worktree.branch == "feature/2607-resume-attach"
    assert status.ahead == 3
    assert status.dirty is False
    assert status.detail is not None
    assert "rebase in progress" in status.detail


def test_gather_detached_status_falls_back_to_dir_name(tmp_path: Path) -> None:
    """A detached worktree with no rebase in progress classifies DETACHED and
    recovers its branch from the issue-<N>-<slug> directory name."""
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    raw = RawWorktree(path=Path("/repo/.worktrees/issue-42-widget"), branch=None)
    with (
        patch(_MOD + ".git.worktree_git_dir", return_value=git_dir),
        patch(_MOD + ".git.read_output", return_value=" M src/x.py"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.commits_ahead") as ahead,
        patch(_MOD + "._probe_pr_workflow", return_value=(None, None, False)),
        patch(_MOD + ".branch_prs", return_value=[]),
    ):
        status = gather_detached_status(raw, target="develop")
    assert status.state is WorktreeState.DETACHED
    assert status.worktree.branch == "feature/42-widget"
    # Branch ref does not exist, so commits-ahead is skipped, not guessed.
    ahead.assert_not_called()
    assert status.ahead == 0
    assert status.dirty is True
    assert status.detail is not None
    assert "detached HEAD" in status.detail


def _rebasing_git_dir(tmp_path: Path, branch: str) -> Path:
    git_dir = tmp_path / "gitdir"
    (git_dir / "rebase-merge").mkdir(parents=True)
    (git_dir / "rebase-merge" / "head-name").write_text(f"refs/heads/{branch}\n")
    return git_dir


def test_gather_detached_status_keeps_the_open_pr_of_a_branch_mid_rebase(tmp_path: Path) -> None:
    """A submitted PR whose branch is mid-rebase keeps its PR number (#4678):
    the open PR of the recovered branch is looked up, not skipped."""
    branch = "feature/4529-docs-review-parallel-merging"
    raw = RawWorktree(
        path=Path("/repo/.worktrees/issue-4529-docs-review-parallel-merging"), branch=None
    )
    open_pr = _pr_ref(4673, "t")
    with (
        patch(_MOD + ".git.worktree_git_dir", return_value=_rebasing_git_dir(tmp_path, branch)),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + "._probe_pr_workflow", return_value=("ready", None, False)),
        patch(_MOD + ".branch_prs", return_value=[open_pr]) as lookup,
    ):
        status = gather_detached_status(raw, target="develop")
    lookup.assert_called_once_with(branch, state="open")
    assert status.state is WorktreeState.REBASING
    assert status.pr_number == 4673


def test_gather_detached_status_pr_lookup_failure_is_in_the_detail(tmp_path: Path) -> None:
    """A failed open-PR lookup is reported in the detail, never read as no PR."""
    branch = "feature/7-x"
    raw = RawWorktree(path=Path("/repo/.worktrees/issue-7-x"), branch=None)
    failure = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502\n")
    with (
        patch(_MOD + ".git.worktree_git_dir", return_value=_rebasing_git_dir(tmp_path, branch)),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + "._probe_pr_workflow", return_value=(None, None, False)),
        patch(_MOD + ".branch_prs", side_effect=failure),
    ):
        status = gather_detached_status(raw, target="develop")
    assert status.state is WorktreeState.REBASING
    assert status.pr_number is None
    assert status.detail is not None
    assert status.detail.startswith("rebase in progress")
    assert status.detail.endswith("; open-PR lookup for feature/7-x failed: HTTP 502")


def test_gather_detached_status_without_a_branch_looks_up_no_pr(tmp_path: Path) -> None:
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    raw = RawWorktree(path=Path("/repo/.worktrees/scratch"), branch=None)
    with (
        patch(_MOD + ".git.worktree_git_dir", return_value=git_dir),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + "._probe_pr_workflow", return_value=(None, None, False)),
        patch(_MOD + ".branch_prs") as lookup,
    ):
        status = gather_detached_status(raw, target="develop")
    lookup.assert_not_called()
    assert status.worktree.branch == "(detached)"
    assert status.pr_number is None


def test_gather_detached_status_last_commit_follows_ahead(tmp_path: Path) -> None:
    """A detached worktree's LAST COMMIT follows the same AHEAD count: shown
    when it is ahead of the target, '-' (None) at a measured AHEAD 0, and
    still shown when there is no branch ref to measure against (#3828)."""
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    raw = RawWorktree(path=Path("/repo/.worktrees/issue-42-widget"), branch=None)
    cases = ((True, 2, 1_699_900_000), (True, 0, None), (False, 0, 1_699_900_000))
    for ref_exists, ahead, expected in cases:
        with (
            patch(_MOD + ".git.worktree_git_dir", return_value=git_dir),
            patch(_MOD + ".git.read_output", return_value=""),
            patch(_MOD + ".git.ref_exists", return_value=ref_exists),
            patch(_MOD + ".git.commits_ahead", return_value=ahead),
            patch(_MOD + "._probe_pr_workflow", return_value=(None, None, False)),
            patch(_MOD + ".branch_prs", return_value=[]),
            patch(_MOD + ".git.committer_timestamp", return_value=1_699_900_000),
            patch(_MOD + "._newest_mtime", return_value=1_699_999_999.0),
        ):
            status = gather_detached_status(raw, target="develop", with_freshness=True)
        assert status.ahead == ahead
        assert status.last_commit_ts == expected
        assert status.last_modified_ts == 1_699_999_999.0


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
    merged_head_matches_tip: bool = True,
) -> WorktreeStatus:
    return classify_worktree(
        _SAMPLE_WT,
        pr_number=pr_number,
        pr_state=pr_state,
        pr_lookup_failed=pr_lookup_failed,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
        merged_head_matches_tip=merged_head_matches_tip,
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


def test_classify_merged_head_mismatch_with_commits_is_stalled() -> None:
    """Issue #1719: a merged PR whose head no longer matches the branch tip
    (name reused) must drop to NO_PR, never MERGED/removable."""
    status = _classify(pr_number=293, pr_state="MERGED", ahead=9, merged_head_matches_tip=False)
    assert status.state is WorktreeState.NO_PR
    assert status.removable is False


def test_classify_merged_head_mismatch_zero_commits_is_draft() -> None:
    status = _classify(pr_number=293, pr_state="MERGED", ahead=0, merged_head_matches_tip=False)
    assert status.state is WorktreeState.DRAFT
    assert status.removable is False


def test_classify_closed_head_mismatch_is_not_removable() -> None:
    status = _classify(pr_number=293, pr_state="CLOSED", ahead=4, merged_head_matches_tip=False)
    assert status.state is WorktreeState.NO_PR
    assert status.removable is False


def test_classify_merged_head_match_remains_removable() -> None:
    status = _classify(pr_number=11, pr_state="MERGED", ahead=2, merged_head_matches_tip=True)
    assert status.state is WorktreeState.MERGED
    assert status.removable is True


# -- needs_attention (issue #2347) -------------------------------------------


def test_classify_merged_dirty_needs_attention() -> None:
    """Issue #2347: a merged PR whose tree is dirty is finished-but-stuck —
    not removable, not live work — and must be flagged needs_attention."""
    status = _classify(pr_number=11, pr_state="MERGED", ahead=2, dirty=True)
    assert status.state is WorktreeState.MERGED
    assert status.removable is False
    assert status.needs_attention is True


def test_classify_closed_dirty_needs_attention() -> None:
    status = _classify(pr_number=12, pr_state="CLOSED", ahead=1, dirty=True)
    assert status.state is WorktreeState.CLOSED
    assert status.removable is False
    assert status.needs_attention is True


def test_classify_merged_clean_is_not_needs_attention() -> None:
    """Removable cruft (merged + clean + matching tip) is not needs_attention;
    it stays plain cleanup, never the attention bucket."""
    status = _classify(pr_number=11, pr_state="MERGED", ahead=2)
    assert status.removable is True
    assert status.needs_attention is False


def test_classify_reused_branch_needs_attention() -> None:
    """A reused branch name withholds the merged verdict (issue #1719); the
    worktree is still finished-but-stuck, so it is flagged needs_attention."""
    status = _classify(pr_number=293, pr_state="MERGED", ahead=9, merged_head_matches_tip=False)
    assert status.state is WorktreeState.NO_PR
    assert status.removable is False
    assert status.needs_attention is True


def test_classify_live_work_is_not_needs_attention() -> None:
    assert _classify(pr_number=10, pr_state="OPEN", ahead=2).needs_attention is False
    assert _classify(ahead=1).needs_attention is False  # NO_PR stalled
    assert _classify(pr_lookup_failed=True).needs_attention is False


# -- gather_worktree_status (I/O wrapper) ------------------------------------


def test_gather_open_pr_short_circuits_closed_lookup() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=2),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(
            _MOD + ".branch_prs",
            return_value=[_pr_ref(9, "old", state="MERGED"), _pr_ref(10, "t")],
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.OPEN_PR
    assert status.pr_number == 10


def test_gather_merged_pr_is_removable() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.branch_commit", return_value="cafef00d"),
        patch(
            _MOD + ".branch_prs",
            return_value=[_pr_ref(11, "t", state="MERGED", head_oid="cafef00d")],
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.MERGED
    assert status.pr_number == 11
    assert status.removable is True


def test_gather_dirty_overlay_blocks_removal() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=" M file.py"),
        patch(_MOD + ".git.branch_commit", return_value="cafef00d"),
        patch(
            _MOD + ".branch_prs",
            return_value=[_pr_ref(11, "t", state="MERGED", head_oid="cafef00d")],
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.MERGED
    assert status.dirty is True
    assert status.removable is False


def test_gather_merged_dirty_flags_attention_with_dirt_detail() -> None:
    """Issue #2347: reproduce the real Mac scenario — a merged worktree left
    dirty by build output. It must be flagged needs_attention (not removable,
    not live work) with an actionable note naming the offending paths."""
    porcelain = " M dist/app.js\n?? build/out.log\n?? coverage/\n"
    with (
        patch(_MOD + ".git.commits_ahead", return_value=2),
        patch(_MOD + ".git.read_output", return_value=porcelain),
        patch(_MOD + ".git.branch_commit", return_value="cafef00d"),
        patch(
            _MOD + ".branch_prs",
            return_value=[_pr_ref(11, "t", state="MERGED", head_oid="cafef00d")],
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.MERGED
    assert status.dirty is True
    assert status.removable is False
    assert status.needs_attention is True
    assert status.detail is not None
    assert "merged PR #11" in status.detail
    assert "dirty" in status.detail
    assert "3 uncommitted path(s)" in status.detail
    assert "dist/app.js" in status.detail
    assert "vrg-finalize-pr" in status.detail


def test_gather_merged_dirt_detail_truncates_long_lists() -> None:
    porcelain = "".join(f"?? file{i}.tmp\n" for i in range(8))
    with (
        patch(_MOD + ".git.commits_ahead", return_value=0),
        patch(_MOD + ".git.read_output", return_value=porcelain),
        patch(_MOD + ".git.branch_commit", return_value="cafef00d"),
        patch(
            _MOD + ".branch_prs",
            return_value=[_pr_ref(11, "t", state="MERGED", head_oid="cafef00d")],
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.detail is not None
    assert "8 uncommitted path(s)" in status.detail
    assert "(+3 more)" in status.detail


def test_gather_merged_clean_has_no_attention_or_dirt_detail() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.branch_commit", return_value="cafef00d"),
        patch(
            _MOD + ".branch_prs",
            return_value=[_pr_ref(11, "t", state="MERGED", head_oid="cafef00d")],
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.removable is True
    assert status.needs_attention is False
    assert status.detail is None


def test_gather_reused_branch_name_is_not_removable() -> None:
    """Issue #1719: a branch name reused after a same-named PR merged must
    not be classified MERGED — its current tip carries unmerged work."""
    with (
        patch(_MOD + ".git.commits_ahead", return_value=9),
        patch(_MOD + ".git.read_output", return_value=""),
        # The local tip is a fresh commit; the merged PR points at an old one.
        patch(_MOD + ".git.branch_commit", return_value="7ead128"),
        patch(
            _MOD + ".branch_prs",
            return_value=[_pr_ref(293, "docs", state="MERGED", head_oid="0ldd0cs")],
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.NO_PR
    assert status.removable is False
    assert status.needs_attention is True
    assert status.detail is not None
    assert "#293" in status.detail
    assert "reused" in status.detail


def test_gather_missing_head_sha_withholds_removal() -> None:
    """Without a head SHA to prove the merge covers this tip, removal is
    withheld — never delete unproven-merged work (issue #1719)."""
    with (
        patch(_MOD + ".git.commits_ahead", return_value=2),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.branch_commit", return_value="7ead128"),
        patch(
            _MOD + ".branch_prs",
            return_value=[_pr_ref(11, "t", state="MERGED", head_oid="")],
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.NO_PR
    assert status.removable is False


def test_gather_no_pr_with_commits_is_stalled() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=3),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".branch_prs", return_value=[]),
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
        patch(_MOD + ".branch_prs", side_effect=err),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.UNKNOWN
    assert "boom" in (status.detail or "")


_SPAM_LOGIN = "spam-login-xyz"
_SPAM_TITLE = "SPAM-MARKER-TEXT get the fix here: [download](https://example.invalid/a)"


def _graphql_pr(number: int, *, fork: bool, state: str, author: str, title: str) -> object:
    """A ``pullRequests`` node as GitHub's GraphQL returns it."""
    return {
        "number": number,
        "url": f"https://github.com/o/r/pull/{number}",
        "title": title,
        "headRefName": _SAMPLE_WT.branch,
        "headRefOid": "cafef00d",
        "state": state,
        "createdAt": "2026-11-01T09:30:00Z",
        "author": {"__typename": "User", "login": author},
        "isCrossRepository": fork,
        "headRepositoryOwner": {"login": "mallory" if fork else "o"},
        "headRepository": {"name": "r"},
    }


def test_worktree_state_ignores_fork_pr(capsys: pytest.CaptureFixture[str]) -> None:
    """A fork's merged PR on the branch name never makes the worktree MERGED or
    removable (GHSA-r4j2-cwcp-xg56); a fork's open PR is no open PR either.
    Each is reported on stderr, metadata only, and its text appears nowhere."""
    nodes = [
        _graphql_pr(70, fork=True, state="MERGED", author=_SPAM_LOGIN, title=_SPAM_TITLE),
        _graphql_pr(71, fork=True, state="OPEN", author=_SPAM_LOGIN, title=_SPAM_TITLE),
    ]
    page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
    authors = TrustedAuthors(human=("the-human",), agents=("the-agent",), orgs={})
    with (
        patch(_MOD + ".git.commits_ahead", return_value=2),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.branch_commit", return_value="cafef00d"),
        patch(_MOD + ".github.current_repo", return_value="o/r"),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=authors),
        patch(
            "vergil_tooling.lib.github.graphql",
            return_value={"repository": {"pullRequests": page}},
        ),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.NO_PR
    assert status.pr_number is None
    assert status.removable is False
    captured = capsys.readouterr()
    assert captured.err == "".join(
        f"skipped untrusted content: o/r#{n} pr {_SPAM_LOGIN} at 2026-11-01T09:30:00Z"
        " on head (cross-repository)\n"
        for n in (70, 71)
    )
    assert "SPAM-MARKER-TEXT" not in captured.out + captured.err + (status.detail or "")


def test_gather_several_open_prs_is_unknown_naming_them() -> None:
    """Several open PRs on one branch are a failed lookup naming them, never the first."""
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".branch_prs", return_value=[_pr_ref(10, "a"), _pr_ref(12, "b")]),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.UNKNOWN
    assert "#10, #12" in (status.detail or "")


_TRUST_FAILURES = [
    pytest.param(
        TrustReadError("prs_for_branch o/r feature/7-foo", "the GraphQL call failed: 502"),
        "the GraphQL call failed: 502",
        id="read-failure",
    ),
    pytest.param(
        TrustedAuthorsNotConfiguredError("no trusted authors are configured"),
        "no trusted authors are configured",
        id="not-configured",
    ),
]


@pytest.mark.parametrize(("err", "reason"), _TRUST_FAILURES)
def test_gather_trust_failure_is_unknown(err: Exception, reason: str) -> None:
    """A trust-layer read or configuration failure is UNKNOWN with its reason,
    never 'no PR' and never a traceback."""
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".branch_prs", side_effect=err),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.UNKNOWN
    assert reason in (status.detail or "")


@pytest.mark.parametrize(("err", "reason"), _TRUST_FAILURES)
def test_gather_detached_trust_failure_is_reported(
    tmp_path: Path, err: Exception, reason: str
) -> None:
    """A detached worktree's failed open-PR lookup is named in its detail."""
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir()
    raw = RawWorktree(path=Path("/repo/.worktrees/issue-42-widget"), branch=None)
    with (
        patch(_MOD + ".git.worktree_git_dir", return_value=git_dir),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + "._probe_pr_workflow", return_value=(None, None, False)),
        patch(_MOD + ".branch_prs", side_effect=err),
    ):
        status = gather_detached_status(raw, target="develop")
    assert status.state is WorktreeState.DETACHED
    assert status.pr_number is None
    assert f"open-PR lookup for feature/42-widget failed: {err}" in (status.detail or "")
    assert reason in (status.detail or "")


def test_closed_pr_for_branch_takes_the_latest_closed() -> None:
    prs = [_pr_ref(5, "a", state="MERGED"), _pr_ref(9, "b", state="CLOSED"), _pr_ref(11, "c")]
    with patch(_MOD + ".branch_prs", return_value=prs) as lookup:
        found = closed_pr_for_branch("feature/7-foo")
    lookup.assert_called_once_with("feature/7-foo", state="all")
    assert found is not None
    assert found.number == 9


# -- _probe_pr_workflow (local .vergil/pr-workflow.json) ---------------------


def _write_state(worktree_root: Path, **overrides: object) -> None:
    """Write a valid pr-workflow.json under *worktree_root* with overrides."""
    state = WorkflowState(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        status="implementing",
        created_at="2026-06-22T00:00:00Z",
        updated_at="2026-06-22T00:00:00Z",
        git={},
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    target = worktree_root / ".vergil"
    target.mkdir(parents=True, exist_ok=True)
    (target / "pr-workflow.json").write_text(state.to_json())


def test_probe_absent_file_is_not_prepared(tmp_path: Path) -> None:
    wt = Worktree(path=tmp_path, branch="feature/42-x")
    assert _probe_pr_workflow(wt) == (None, None, False)


def test_probe_loaded_with_metadata_is_prepared(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        status="ready",
        pr_metadata={"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"},
    )
    wt = Worktree(path=tmp_path, branch="feature/42-x")
    assert _probe_pr_workflow(wt) == ("ready", None, True)


def test_probe_already_submitted_is_not_prepared(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        status="ready",
        pr_metadata={"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"},
        submitted={"pr_url": "https://example/pull/9", "pr_number": 9},
    )
    wt = Worktree(path=tmp_path, branch="feature/42-x")
    status, error, prepared = _probe_pr_workflow(wt)
    assert status == "ready"
    assert error is None
    assert prepared is False


def test_probe_no_metadata_is_not_prepared(tmp_path: Path) -> None:
    _write_state(tmp_path, status="implementing")
    wt = Worktree(path=tmp_path, branch="feature/42-x")
    assert _probe_pr_workflow(wt) == ("implementing", None, False)


def test_probe_malformed_file_reports_error(tmp_path: Path) -> None:
    target = tmp_path / ".vergil"
    target.mkdir(parents=True)
    (target / "pr-workflow.json").write_text("{not valid json")
    wt = Worktree(path=tmp_path, branch="feature/42-x")
    status, error, prepared = _probe_pr_workflow(wt)
    assert status is None
    assert error is not None
    assert prepared is False


def test_probe_v1_schema_file_reports_error(tmp_path: Path) -> None:
    """A leftover v1 schema file gracefully degrades instead of crashing.

    Issue #1872 design: the no-migration approach relies on _probe_pr_workflow
    capturing unsupported-version errors so worktrees with stale v1 pr-workflow.json
    files degrade gracefully.
    """
    import json

    target = tmp_path / ".vergil"
    target.mkdir(parents=True)
    # Write a valid JSON document with schema_version=1 (old schema).
    # from_dict checks schema_version BEFORE required fields, so even a
    # minimal v1 document triggers the unsupported-version error.
    v1_doc = {"schema_version": 1}
    (target / "pr-workflow.json").write_text(json.dumps(v1_doc))
    wt = Worktree(path=tmp_path, branch="feature/42-x")
    status, error, prepared = _probe_pr_workflow(wt)
    assert status is None
    assert error is not None
    assert "unsupported schema_version" in error
    assert prepared is False


def test_gather_attaches_pr_workflow_probe(tmp_path: Path) -> None:
    _write_state(
        tmp_path,
        status="ready",
        pr_metadata={"title": "t", "summary": "s", "notes": "n", "linkage": "Ref"},
    )
    wt = Worktree(path=tmp_path, branch="feature/42-x")
    with (
        patch(_MOD + ".git.commits_ahead", return_value=3),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".branch_prs", return_value=[]),
    ):
        status = gather_worktree_status(wt, target="develop")
    assert status.state is WorktreeState.NO_PR
    assert status.workflow_status == "ready"
    assert status.workflow_error is None
    assert status.pr_prepared is True


def _wt(name: str, branch: str) -> Worktree:
    return Worktree(path=Path(f"/repo/.worktrees/{name}"), branch=branch)


def test_select_worktrees_single_skips_prompt() -> None:
    wt = _wt("issue-1-foo", "feature/1-foo")
    assert select_worktrees([wt], purpose="p", labels=["foo"]) == [wt]


def test_select_worktrees_multi_uses_prompt_indices() -> None:
    a = _wt("issue-1-a", "feature/1-a")
    b = _wt("issue-2-b", "feature/2-b")
    c = _wt("issue-3-c", "feature/3-c")
    with (
        patch(_MOD + ".require_tty"),
        patch(_MOD + ".prompt_multi_choice", return_value=[0, 2]),
    ):
        assert select_worktrees([a, b, c], purpose="p", labels=["a", "b", "c"]) == [a, c]


def test_match_worktrees_by_issue_number_and_name_in_token_order() -> None:
    a = _wt("issue-1673-foo", "feature/1673-foo")
    b = _wt("issue-1681-bar", "feature/1681-bar")
    assert match_worktrees([a, b], ["1681", "issue-1673-foo"]) == [b, a]


def test_match_worktrees_unmatched_token_errors() -> None:
    a = _wt("issue-1-a", "feature/1-a")
    with pytest.raises(ValueError, match="no ready worktree matches: 999"):
        match_worktrees([a], ["999"])


def test_match_worktrees_skips_non_issue_named() -> None:
    # A worktree name that is not canonical issue-<N>-... is matched only by
    # directory name; it never populates the issue-number index.
    a = _wt("scratch", "feature/scratch")
    assert match_worktrees([a], ["scratch"]) == [a]


def test_match_worktrees_ambiguous_issue_number_errors() -> None:
    a = _wt("issue-5-a", "feature/5-a")
    b = _wt("issue-5-b", "feature/5-b")
    with pytest.raises(ValueError, match="ambiguous .multiple worktrees.: 5"):
        match_worktrees([a, b], ["5"])


def test_select_worktrees_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least one candidate"):
        select_worktrees([], purpose="p", labels=[])


def test_newest_mtime_takes_max_over_tracked_and_untracked(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked.py"
    tracked.write_text("x")
    os.utime(tracked, (1000.0, 1000.0))
    untracked = tmp_path / "untracked.py"
    untracked.write_text("y")
    os.utime(untracked, (5000.0, 5000.0))
    # First read_output call → tracked listing; second → untracked listing.
    with patch(_MOD + ".git.read_output", side_effect=["tracked.py", "untracked.py"]):
        assert _newest_mtime(tmp_path) == 5000.0


def test_newest_mtime_keeps_max_when_later_file_is_older(tmp_path: Path) -> None:
    """Cover the mtime > newest false branch: a later-listed file with a lower
    mtime must not replace the already-tracked maximum."""
    newer = tmp_path / "newer.py"
    newer.write_text("x")
    os.utime(newer, (5000.0, 5000.0))
    older = tmp_path / "older.py"
    older.write_text("y")
    os.utime(older, (1000.0, 1000.0))
    with patch(_MOD + ".git.read_output", side_effect=["newer.py\nolder.py", ""]):
        assert _newest_mtime(tmp_path) == 5000.0


def test_newest_mtime_skips_listed_but_missing_file(tmp_path: Path) -> None:
    present = tmp_path / "present.py"
    present.write_text("x")
    os.utime(present, (2000.0, 2000.0))
    with patch(_MOD + ".git.read_output", side_effect=["present.py\nghost.py", ""]):
        assert _newest_mtime(tmp_path) == 2000.0


def test_newest_mtime_none_when_no_files(tmp_path: Path) -> None:
    with patch(_MOD + ".git.read_output", side_effect=["", ""]):
        assert _newest_mtime(tmp_path) is None


_RAW = RawWorktree(path=Path("/repo/.worktrees/issue-327-x"), branch="feature/327-x")


def test_worktree_still_present_when_rediscovered_and_branch_exists() -> None:
    with (
        patch(_MOD + ".discover_worktrees", return_value=[_RAW]),
        patch(_MOD + ".git.ref_exists", return_value=True) as ref_exists,
    ):
        assert worktree_still_present(_RAW, Path("/repo")) is True
    ref_exists.assert_called_once_with("refs/heads/feature/327-x")


def test_worktree_not_present_when_no_longer_discovered() -> None:
    """Issue #3187: a worktree removed by a concurrent finalize is gone."""
    with patch(_MOD + ".discover_worktrees", return_value=[]):
        assert worktree_still_present(_RAW, Path("/repo")) is False


def test_worktree_not_present_when_branch_deleted() -> None:
    with (
        patch(_MOD + ".discover_worktrees", return_value=[_RAW]),
        patch(_MOD + ".git.ref_exists", return_value=False),
    ):
        assert worktree_still_present(_RAW, Path("/repo")) is False


def test_detached_worktree_present_without_branch_check() -> None:
    raw = RawWorktree(path=_RAW.path, branch=None)
    with (
        patch(_MOD + ".discover_worktrees", return_value=[raw]),
        patch(_MOD + ".git.ref_exists") as ref_exists,
    ):
        assert worktree_still_present(raw, Path("/repo")) is True
    ref_exists.assert_not_called()


# -- Repo-wide discovery for vrg-repo-status (issue #3386) -----------------

_PORCELAIN_REPO_WIDE = """\
worktree /repo
HEAD 1111111111111111111111111111111111111111
branch refs/heads/develop

worktree /repo/.worktrees/issue-7-foo
HEAD 2222222222222222222222222222222222222222
branch refs/heads/feature/7-foo

worktree /elsewhere/rogue
HEAD 3333333333333333333333333333333333333333
branch refs/heads/feature/9-rogue

worktree /repo/.worktrees/tmp-wheel-build
HEAD 4444444444444444444444444444444444444444
detached
"""


def test_discover_repo_worktrees_keeps_main_and_every_linked_worktree() -> None:
    """The status view sees the main checkout and every linked worktree,
    including one outside .worktrees/ and a detached one."""
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN_REPO_WIDE):
        repo = discover_repo_worktrees()
    assert repo.main == RawWorktree(path=Path("/repo"), branch="develop")
    assert repo.linked == (
        RawWorktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo"),
        RawWorktree(path=Path("/elsewhere/rogue"), branch="feature/9-rogue"),
        RawWorktree(path=Path("/repo/.worktrees/tmp-wheel-build"), branch=None),
    )


def test_discover_worktrees_unchanged_by_repo_wide_discovery() -> None:
    """Finalize and sweep callers still see only canonical worktrees."""
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN_REPO_WIDE):
        result = discover_worktrees(Path("/repo"))
    assert result == [
        RawWorktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo"),
        RawWorktree(path=Path("/repo/.worktrees/tmp-wheel-build"), branch=None),
    ]


def test_is_canonical_worktree() -> None:
    assert is_canonical_worktree(Path("/repo/.worktrees/issue-7-foo"), Path("/repo")) is True
    assert is_canonical_worktree(Path("/elsewhere/rogue"), Path("/repo")) is False
    assert is_canonical_worktree(Path("/repo"), Path("/repo")) is False


@pytest.mark.parametrize(
    ("branch", "canonical", "kind"),
    [
        ("feature/7-foo", True, WorktreeKind.DEVELOPMENT),
        ("release/1.4.0", True, WorktreeKind.MACHINERY),
        ("release/post-1.4.0", True, WorktreeKind.MACHINERY),
        ("deps/update-1.0.3", True, WorktreeKind.MACHINERY),
        ("bugfix/7-foo", True, WorktreeKind.DEVELOPMENT),
        ("hotfix/8-urgent", True, WorktreeKind.DEVELOPMENT),
        # The fix-task branch vrg-update-deps asks for when an update fails.
        ("chore/3400-dep-update-1.0.3", True, WorktreeKind.DEVELOPMENT),
        # An issue prefix without an issue number is not recognised.
        ("feature/no-issue-number", True, WorktreeKind.OTHER),
        ("chore/tidy", True, WorktreeKind.OTHER),
        # An unknown prefix, even with a number.
        ("spike/12-idea", True, WorktreeKind.OTHER),
        ("experiment", True, WorktreeKind.OTHER),
        ("(detached)", True, WorktreeKind.OTHER),
        (None, True, WorktreeKind.OTHER),
        # Outside .worktrees/ is always Other, whatever the branch.
        ("feature/9-rogue", False, WorktreeKind.OTHER),
        ("release/1.4.0", False, WorktreeKind.OTHER),
    ],
)
def test_worktree_kind(branch: str | None, canonical: bool, kind: WorktreeKind) -> None:
    assert worktree_kind(branch, canonical=canonical) is kind


def test_worktree_still_present_any_location_uses_repo_wide_discovery() -> None:
    rogue = RawWorktree(path=Path("/elsewhere/rogue"), branch="feature/9-rogue")
    with (
        patch(_MOD + ".git.read_output", return_value=_PORCELAIN_REPO_WIDE),
        patch(_MOD + ".git.ref_exists", return_value=True),
    ):
        assert worktree_still_present(rogue, Path("/repo"), any_location=True) is True
        assert worktree_still_present(rogue, Path("/repo")) is False


def _main_status(
    tmp_path: Path, raw: RawWorktree, *, porcelain: str = "", integration: str = "develop"
) -> MainCheckoutStatus:
    git_dir = tmp_path / "gitdir"
    git_dir.mkdir(exist_ok=True)
    with (
        patch(_MOD + ".git.worktree_git_dir", return_value=git_dir),
        patch(_MOD + ".git.read_output", return_value=porcelain),
        patch(_MOD + ".git.committer_timestamp", return_value=1_699_900_000),
    ):
        return gather_main_checkout_status(raw, integration=integration)


def test_main_checkout_on_integration_branch_is_ok(tmp_path: Path) -> None:
    status = _main_status(tmp_path, RawWorktree(path=Path("/repo"), branch="develop"))
    assert status.branch == "develop"
    assert status.problems == ()
    assert status.notes == ()
    assert status.last_commit_ts == 1_699_900_000


def test_main_checkout_off_integration_branch_is_flagged(tmp_path: Path) -> None:
    raw = RawWorktree(path=Path("/repo"), branch="feature/12-x")
    status = _main_status(tmp_path, raw)
    assert status.problems == ("not on develop",)


def test_main_checkout_uses_configured_integration_branch(tmp_path: Path) -> None:
    raw = RawWorktree(path=Path("/repo"), branch="main")
    assert _main_status(tmp_path, raw, integration="main").problems == ()


def test_main_checkout_dirty_is_flagged_with_paths(tmp_path: Path) -> None:
    raw = RawWorktree(path=Path("/repo"), branch="develop")
    status = _main_status(tmp_path, raw, porcelain=" M src/x.py\n?? notes.txt")
    assert status.dirty is True
    assert status.problems == ("dirty",)
    assert status.notes == ("2 uncommitted path(s): src/x.py, notes.txt",)


def test_main_checkout_mid_rebase_is_flagged(tmp_path: Path) -> None:
    git_dir = tmp_path / "gitdir"
    (git_dir / "rebase-merge").mkdir(parents=True)
    (git_dir / "rebase-merge" / "head-name").write_text("refs/heads/develop\n")
    status = _main_status(tmp_path, RawWorktree(path=Path("/repo"), branch=None))
    assert status.branch == "develop"
    assert status.rebasing is True
    assert status.problems == ("mid-rebase",)
    assert any("rebase in progress" in note for note in status.notes)


def test_main_checkout_detached_is_off_integration(tmp_path: Path) -> None:
    status = _main_status(tmp_path, RawWorktree(path=Path("/repo"), branch=None))
    assert status.branch is None
    assert status.problems == ("not on develop",)


def test_main_checkout_every_problem_at_once(tmp_path: Path) -> None:
    git_dir = tmp_path / "gitdir"
    (git_dir / "rebase-apply").mkdir(parents=True)
    (git_dir / "rebase-apply" / "head-name").write_text("refs/heads/feature/1-x\n")
    raw = RawWorktree(path=Path("/repo"), branch=None)
    status = _main_status(tmp_path, raw, porcelain="UU src/x.py")
    assert status.problems == ("not on develop", "dirty", "mid-rebase")


def test_gone_branches_flags_upstream_missing_on_remote() -> None:
    upstreams = {
        "develop": ("origin", "develop"),
        "feature/1-merged": ("origin", "feature/1-merged"),
        "feature/2-live": ("origin", "feature/2-live"),
        "feature/3-tracks-develop": ("origin", "develop"),
    }
    with (
        patch(_MOD + ".git.branch_upstreams", return_value=upstreams),
        patch(_MOD + ".git.remote_branches", return_value={"develop", "feature/2-live"}) as remote,
    ):
        gone = gone_branches(
            [
                "develop",
                "feature/1-merged",
                "feature/2-live",
                "feature/3-tracks-develop",
                "feature/4-never-pushed",
            ]
        )
    assert gone == {"feature/1-merged"}
    remote.assert_called_once_with("origin")


def test_gone_branches_skips_remote_lookup_without_upstreams() -> None:
    with (
        patch(_MOD + ".git.branch_upstreams", return_value={}),
        patch(_MOD + ".git.remote_branches") as remote,
    ):
        assert gone_branches(["feature/4-never-pushed"]) == set()
    remote.assert_not_called()


def test_gone_branches_propagates_remote_failure() -> None:
    err = subprocess.CalledProcessError(128, ("git", "ls-remote"), stderr="no network")
    with (
        patch(
            _MOD + ".git.branch_upstreams", return_value={"feature/1-x": ("origin", "feature/1-x")}
        ),
        patch(_MOD + ".git.remote_branches", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        gone_branches(["feature/1-x"])


# -- release protection and removal logging (issue #5187) --------------------

_RESUME = _MOD + "._open_release_issues"
_PREFLIGHT = _MOD + "._trusted_release_issues"
_READ_LOCK = _MOD + ".refs.read_lock"
_NO_LOCK = LockState(sha=None, record=None, error="")


def test_release_protection_active_while_a_trusted_release_issue_is_open() -> None:
    listed = [("2.1.9", 77, "listed body")]
    with (
        patch(_READ_LOCK, return_value=_NO_LOCK),
        patch(_RESUME, return_value=listed) as listing,
        patch(_PREFLIGHT, return_value=[("2.1.9", 77, "trusted body")]) as trusted,
    ):
        protection = release_protection("o/r")
    assert protection == ReleaseProtection(
        active=True, reason="open tracking issue: release 2.1.9 (#77)"
    )
    listing.assert_called_once_with("o/r", phase="cleanup")
    trusted.assert_called_once_with(listed, "o/r")


def test_release_protection_inactive_with_no_trusted_release_issue() -> None:
    """An open release issue whose body is untrusted is no release running."""
    with (
        patch(_READ_LOCK, return_value=_NO_LOCK),
        patch(_RESUME, return_value=[("2.1.9", 77, "outsider body")]),
        patch(_PREFLIGHT, return_value=[]),
    ):
        protection = release_protection("o/r")
    assert protection.active is False
    assert protection.reason == (
        "no release lock is held and no release tracking issue is open"
    )


def test_release_protection_raises_on_a_refused_listing() -> None:
    refusal = ReleaseError(
        phase="cleanup", command="gh issue list", message="search returned str", detail="raw"
    )
    with (
        patch(_READ_LOCK, return_value=_NO_LOCK),
        patch(_RESUME, side_effect=refusal),
        pytest.raises(ProtectionReadError) as info,
    ):
        release_protection("o/r")
    assert "search returned str" in str(info.value)
    assert "raw" in str(info.value)


def test_release_protection_raises_on_a_failed_gh_call() -> None:
    failure = subprocess.CalledProcessError(1, ("gh", "issue", "list"))
    with (
        patch(_READ_LOCK, return_value=_NO_LOCK),
        patch(_RESUME, side_effect=failure),
        pytest.raises(ProtectionReadError, match="o/r"),
    ):
        release_protection("o/r")


def test_release_protection_raises_on_a_failed_trust_read() -> None:
    refusal = ReleaseError(phase="preflight", command="read_issue", message="refusing blind")
    with (
        patch(_READ_LOCK, return_value=_NO_LOCK),
        patch(_RESUME, return_value=[("2.1.9", 77, "body")]),
        patch(_PREFLIGHT, side_effect=refusal),
        pytest.raises(ProtectionReadError, match="refusing blind"),
    ):
        release_protection("o/r")


def test_log_removal_outside_a_pipeline_prints_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_removal(Path("/r/.worktrees/issue-1-x"), "feature/1-x", "PR merged")
    err = capsys.readouterr().err
    assert err == "removed worktree /r/.worktrees/issue-1-x (branch feature/1-x): PR merged\n"


def test_log_removal_in_a_stage_reaches_the_run_log_once(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = progress.RunLog("test-cmd", tmp_path)
    try:
        progress.run_stage(
            "cleanup",
            lambda: log_removal(Path("/r/.worktrees/release-2.1.9"), "release/2.1.9", "why"),
            args=argparse.Namespace(output_window=None, output_format="plain"),
            log=log,
            describe=lambda _: ("ok", "done"),
        )
    finally:
        log.close()
    line = "removed worktree /r/.worktrees/release-2.1.9 (branch release/2.1.9): why"
    assert log.path.read_text(encoding="utf-8").count(line) == 1
    assert line in capsys.readouterr().out


def test_log_removal_with_an_attached_log_reaches_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = progress.RunLog("test-cmd", tmp_path)
    try:
        with progress.attach_log(log):
            log_removal(Path("/r/.worktrees/issue-1-x"), "feature/1-x", "PR closed")
    finally:
        log.close()
    line = "removed worktree /r/.worktrees/issue-1-x (branch feature/1-x): PR closed"
    assert log.path.read_text(encoding="utf-8").count(line) == 1
    assert line in capsys.readouterr().err


# -- the release lock's key (Task 27 of epic vergil-project/.github#1664) ----

_LOCK_SHA = "b" * 40


def _lock_record(**changes: str) -> LockRecord:
    fields = {
        "holder_identity": "human",
        "holder_host": "mac.local",
        "version": "2.1.9",
        "worktree": "/r/.worktrees/release-2.1.9",
        "tracking_issue": "o/r#77",
        "stage": "cut",
        "health": "running",
        "broken_stage": "",
        "started_at": "2026-11-27T10:00:00Z",
        "heartbeat_at": "2026-11-27T10:05:00Z",
        "token": "a1b2c3",
    }
    fields.update(changes)
    return LockRecord(**fields)  # type: ignore[arg-type]


def test_release_protection_active_while_the_lock_is_held() -> None:
    """A held lock protects, names its worktree, and needs no tracking-issue read."""
    held = LockState(sha=_LOCK_SHA, record=_lock_record(), error="")
    with (
        patch(_READ_LOCK, return_value=held) as read,
        patch(_RESUME) as listing,
    ):
        protection = release_protection("o/r")
    assert protection.active is True
    assert protection.worktree == "/r/.worktrees/release-2.1.9"
    assert "release lock held: release 2.1.9" in protection.reason
    read.assert_called_once_with("o/r")
    listing.assert_not_called()


def test_release_protection_unreadable_lock_is_held() -> None:
    """A lock that is present but cannot be read protects: it is held, never absent."""
    unreadable = LockState(sha=_LOCK_SHA, record=None, error="refs/vergil/release-lock is unreadable")
    with patch(_READ_LOCK, return_value=unreadable), patch(_RESUME) as listing:
        protection = release_protection("o/r")
    assert protection.active is True
    assert protection.worktree == ""
    assert "the release lock cannot be read" in protection.reason
    assert "is unreadable" in protection.reason
    listing.assert_not_called()


def test_release_protection_after_break_keeps_the_tracking_issue_key() -> None:
    """With no lock (a break deleted it), an open trusted tracking issue still protects."""
    with (
        patch(_READ_LOCK, return_value=_NO_LOCK),
        patch(_RESUME, return_value=[("2.1.9", 77, "body")]),
        patch(_PREFLIGHT, return_value=[("2.1.9", 77, "body")]),
    ):
        protection = release_protection("o/r")
    assert protection == ReleaseProtection(
        active=True, reason="open tracking issue: release 2.1.9 (#77)"
    )


def test_release_protection_raises_when_the_lock_cannot_be_read() -> None:
    """Whether the lock is held cannot be told: a read error, never no lock."""
    with (
        patch(_READ_LOCK, side_effect=RefReadError("reading refs/vergil/release-lock failed")),
        patch(_RESUME) as listing,
        pytest.raises(ProtectionReadError, match="could not read the release lock of o/r"),
    ):
        release_protection("o/r")
    listing.assert_not_called()


def test_release_protection_reuses_a_lock_already_read() -> None:
    """A caller that has the lock's protection passes it; the lock is not read again."""
    absent = ReleaseProtection(active=False, reason="no release lock is held")
    with (
        patch(_READ_LOCK) as read,
        patch(_RESUME, return_value=[]),
        patch(_PREFLIGHT, return_value=[]),
    ):
        protection = release_protection("o/r", lock=absent)
    read.assert_not_called()
    assert protection.active is False


def test_lock_protection_inactive_with_no_lock() -> None:
    with patch(_READ_LOCK, return_value=_NO_LOCK):
        protection = lock_protection("o/r")
    assert protection == ReleaseProtection(active=False, reason="no release lock is held")


def test_lock_protection_names_its_worktree(tmp_path: Path) -> None:
    """The named worktree matches by resolved path; a relative name is under the root."""
    wt = tmp_path / ".worktrees" / "release-2.1.9"
    absolute = ReleaseProtection(active=True, reason="held", worktree=str(wt))
    relative = ReleaseProtection(active=True, reason="held", worktree=".worktrees/release-2.1.9")
    unnamed = ReleaseProtection(active=True, reason="held")
    assert absolute.names(wt, tmp_path) is True
    assert relative.names(wt, tmp_path) is True
    assert absolute.names(tmp_path / ".worktrees" / "issue-1-x", tmp_path) is False
    assert unnamed.names(wt, tmp_path) is False


def test_release_own_teardown_not_blocked(tmp_path: Path) -> None:
    """The release's teardown-worktree stage removes the worktree the held lock
    names: it is a sanctioned removal, never judged by the protection."""
    wt = tmp_path / ".worktrees" / "release-2.1.9"
    wt.mkdir(parents=True)
    ctx = ReleaseContext(repo="o/r", version="2.1.9", repo_root=tmp_path, version_override=None)
    ctx.worktree_path = wt
    ctx.release_branch = "release/2.1.9"
    held = LockState(sha=_LOCK_SHA, record=_lock_record(worktree=str(wt)), error="")
    calls: list[tuple[str, ...]] = []
    cwd = Path.cwd()
    try:
        with (
            patch(_READ_LOCK, return_value=held) as read,
            patch(_RESUME) as listing,
            patch(
                "vergil_tooling.lib.managed_worktree.git.run",
                side_effect=lambda *a: calls.append(a),
            ),
        ):
            teardown_worktree(ctx)
    finally:
        os.chdir(cwd)
    assert calls == [("worktree", "remove", "--force", str(wt))]
    assert ctx.worktree_path is None
    read.assert_not_called()
    listing.assert_not_called()
