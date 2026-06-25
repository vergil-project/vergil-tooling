"""Tests for vergil_tooling.lib.worktrees."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow.state import WorkflowState
from vergil_tooling.lib.worktrees import (
    Worktree,
    WorktreeState,
    WorktreeStatus,
    _newest_mtime,
    _probe_pr_workflow,
    classify_worktree,
    gather_worktree_status,
    list_worktrees,
    match_worktrees,
    rebase_onto,
    require_tty,
    select_worktree,
    select_worktrees,
    worktree_for_branch,
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
    with patch(_MOD + ".git.run") as run:
        rebase_onto(wt, "develop")
    assert run.call_args_list[0].args == ("-C", str(wt.path), "fetch", "origin", "develop")
    assert run.call_args_list[1].args == ("-C", str(wt.path), "rebase", "origin/develop")


def test_rebase_onto_propagates_conflict() -> None:
    wt = _wt("issue-1-a", "feature/1-a")
    err = subprocess.CalledProcessError(1, ["git", "rebase"])
    with (
        patch(_MOD + ".git.run", side_effect=[None, err]),
        pytest.raises(subprocess.CalledProcessError),
    ):
        rebase_onto(wt, "develop")


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
