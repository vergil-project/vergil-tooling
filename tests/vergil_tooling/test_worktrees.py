"""Tests for vergil_tooling.lib.worktrees."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.worktrees import (
    MainCheckoutStatus,
    MergeCommitsError,
    RawWorktree,
    Worktree,
    WorktreeKind,
    WorktreeState,
    WorktreeStatus,
    _branch_from_worktree_name,
    _newest_mtime,
    _probe_pr_workflow,
    _rebase_head_name,
    classify_worktree,
    discover_repo_worktrees,
    discover_worktrees,
    gather_detached_status,
    gather_main_checkout_status,
    gather_worktree_status,
    gone_branches,
    is_canonical_worktree,
    list_worktrees,
    match_worktrees,
    rebase_onto,
    require_tty,
    select_worktree,
    select_worktrees,
    worktree_for_branch,
    worktree_kind,
    worktree_still_present,
)

_MOD = "vergil_tooling.lib.worktrees"


def test_gather_populates_timestamps() -> None:
    wt = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
    with (
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.closed_pr_for_branch", return_value=None),
        patch(_MOD + ".git.committer_timestamp", return_value=1_699_900_000),
        patch(_MOD + "._newest_mtime", return_value=1_699_999_999.0),
    ):
        status = gather_worktree_status(wt, target="develop", with_freshness=True)
    assert status.last_commit_ts == 1_699_900_000
    assert status.last_modified_ts == 1_699_999_999.0


def test_gather_without_freshness_leaves_timestamps_none() -> None:
    wt = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
    with (
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.closed_pr_for_branch", return_value=None),
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
        patch(_MOD + ".git.commit_sha", return_value="cafef00d"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t", "headRefOid": "cafef00d"},
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
        patch(_MOD + ".git.commit_sha", return_value="cafef00d"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t", "headRefOid": "cafef00d"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
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
        patch(_MOD + ".git.commit_sha", return_value="cafef00d"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t", "headRefOid": "cafef00d"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
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
        patch(_MOD + ".git.commit_sha", return_value="cafef00d"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t", "headRefOid": "cafef00d"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.detail is not None
    assert "8 uncommitted path(s)" in status.detail
    assert "(+3 more)" in status.detail


def test_gather_merged_clean_has_no_attention_or_dirt_detail() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.commit_sha", return_value="cafef00d"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t", "headRefOid": "cafef00d"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
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
        patch(_MOD + ".git.commit_sha", return_value="7ead128"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "293", "url": "", "title": "docs", "headRefOid": "0ldd0cs"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
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
        patch(_MOD + ".git.commit_sha", return_value="7ead128"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t", "headRefOid": ""},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.NO_PR
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
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.closed_pr_for_branch", return_value=None),
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


def test_rebase_onto_fetches_then_rebases() -> None:
    wt = _wt("issue-1-a", "feature/1-a")
    with (
        patch(_MOD + ".git.run") as run,
        patch(_MOD + ".git.read_output", return_value="") as read,
    ):
        rebase_onto(wt, "develop")
    assert run.call_args_list[0].args == ("-C", str(wt.path), "fetch", "origin", "develop")
    assert read.call_args.args == (
        "-C",
        str(wt.path),
        "rev-list",
        "--merges",
        "origin/develop..HEAD",
    )
    assert run.call_args_list[1].args == ("-C", str(wt.path), "rebase", "origin/develop")


def test_rebase_onto_propagates_conflict() -> None:
    wt = _wt("issue-1-a", "feature/1-a")
    err = subprocess.CalledProcessError(1, ["git", "rebase"])
    with (
        patch(_MOD + ".git.run", side_effect=[None, err]),
        patch(_MOD + ".git.read_output", return_value=""),
        pytest.raises(subprocess.CalledProcessError),
    ):
        rebase_onto(wt, "develop")


def test_rebase_onto_refuses_branch_with_merge_commits() -> None:
    wt = _wt("issue-1-a", "feature/1-a")
    with (
        patch(_MOD + ".git.run") as run,
        patch(_MOD + ".git.read_output", return_value="a" * 40 + "\n" + "b" * 40),
        pytest.raises(MergeCommitsError, match=r"2 merge commit\(s\)") as excinfo,
    ):
        rebase_onto(wt, "develop")
    # Only the fetch ran; the rebase that would drop the merges never did.
    assert [c.args[2] for c in run.call_args_list] == ["fetch"]
    msg = str(excinfo.value)
    assert "feature/1-a" in msg
    assert "aaaaaaaaaaaa, bbbbbbbbbbbb" in msg
    assert "submit it individually" in msg


def _real_git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def _git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    # Isolate from the developer's global/system git config (hooks, signing).
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def _origin_and_clone(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    _real_git(tmp_path, "init", "--bare", "-b", "develop", str(origin))
    clone = tmp_path / "clone"
    _real_git(tmp_path, "clone", str(origin), str(clone))
    _real_git(clone, "checkout", "-b", "develop")
    (clone / "base.txt").write_text("base\n")
    _real_git(clone, "add", "base.txt")
    _real_git(clone, "commit", "-m", "base")
    _real_git(clone, "push", "origin", "develop")
    return clone


def _advance_origin_develop(clone: Path) -> None:
    _real_git(clone, "checkout", "develop")
    (clone / "upstream.txt").write_text("upstream\n")
    _real_git(clone, "add", "upstream.txt")
    _real_git(clone, "commit", "-m", "upstream")
    _real_git(clone, "push", "origin", "develop")


@pytest.mark.usefixtures("_git_identity")
def test_rebase_onto_real_git_never_linearizes_a_merge(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)
    # A feature branch that merges in a side branch (the issue #3166 shape).
    _real_git(clone, "checkout", "-b", "side")
    (clone / "side.txt").write_text("side\n")
    _real_git(clone, "add", "side.txt")
    _real_git(clone, "commit", "-m", "side")
    _real_git(clone, "checkout", "develop")
    _real_git(clone, "checkout", "-b", "feature/1-a")
    (clone / "feat.txt").write_text("feat\n")
    _real_git(clone, "add", "feat.txt")
    _real_git(clone, "commit", "-m", "feat")
    _real_git(clone, "merge", "--no-ff", "--no-edit", "side")
    head_before = _real_git(clone, "rev-parse", "feature/1-a")
    _advance_origin_develop(clone)
    _real_git(clone, "checkout", "feature/1-a")

    with pytest.raises(MergeCommitsError):
        rebase_onto(Worktree(path=clone, branch="feature/1-a"), "develop")
    # The branch is untouched: the merge commit survives.
    assert _real_git(clone, "rev-parse", "HEAD") == head_before
    assert _real_git(clone, "rev-list", "--merges", "origin/develop..HEAD") != ""


@pytest.mark.usefixtures("_git_identity")
def test_rebase_onto_real_git_rebases_linear_branch(tmp_path: Path) -> None:
    clone = _origin_and_clone(tmp_path)
    _real_git(clone, "checkout", "-b", "feature/2-b")
    (clone / "feat.txt").write_text("feat\n")
    _real_git(clone, "add", "feat.txt")
    _real_git(clone, "commit", "-m", "feat")
    _advance_origin_develop(clone)
    _real_git(clone, "checkout", "feature/2-b")

    rebase_onto(Worktree(path=clone, branch="feature/2-b"), "develop")
    assert _real_git(clone, "merge-base", "HEAD", "origin/develop") == _real_git(
        clone, "rev-parse", "origin/develop"
    )
    assert _real_git(clone, "log", "--format=%s", "-1") == "feat"


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


# -- Repo-wide discovery for vrg-worktree-status (issue #3386) -----------------

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
