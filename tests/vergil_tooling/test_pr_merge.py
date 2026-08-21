"""Loop state-table tests for vergil_tooling.lib.pr_merge."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.github import GitHubAPIError, OrphanedCheckError
from vergil_tooling.lib.pr_merge import MergeAbortError, wait_and_merge

_MOD = "vergil_tooling.lib.pr_merge"


def _gh(
    *,
    state: str = "OPEN",
    draft: bool = False,
    mergeable: str = "MERGEABLE",
    merge_states: list[str] | None = None,
    failed: list[str] | None = None,
) -> MagicMock:
    """Build a mocked github module for one scenario."""
    gh = MagicMock()
    gh.pr_state.return_value = state
    gh.is_draft.return_value = draft
    gh.mergeable.return_value = mergeable
    gh.merge_state_status.side_effect = merge_states or ["CLEAN", "CLEAN"]
    gh.failed_check_names.return_value = failed or []
    return gh


def test_green_first_try_merges() -> None:
    gh = _gh()
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="squash")
    gh.wait_for_checks.assert_called_once_with("99")
    gh.merge.assert_called_once_with("99", strategy="squash")
    gh.update_branch.assert_not_called()


def test_merged_on_entry_raises() -> None:
    gh = _gh(state="MERGED")
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="already merged"):
        wait_and_merge("99", strategy="squash")
    gh.merge.assert_not_called()


def test_draft_aborts_before_waiting() -> None:
    gh = _gh(draft=True)
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="draft"):
        wait_and_merge("99", strategy="squash")
    gh.wait_for_checks.assert_not_called()


def test_conflicting_aborts_before_waiting() -> None:
    gh = _gh(mergeable="CONFLICTING")
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="merge conflicts"):
        wait_and_merge("99", strategy="squash")
    gh.wait_for_checks.assert_not_called()


def test_behind_on_entry_updates_before_waiting() -> None:
    gh = _gh(merge_states=["BEHIND", "CLEAN", "CLEAN"])
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    gh.update_branch.assert_called_once_with("99")
    # update happened BEFORE the (single) wait — BEHIND-first ordering
    gh.wait_for_checks.assert_called_once_with("99")
    gh.merge.assert_called_once()


def test_behind_after_wait_loops_and_updates() -> None:
    # iteration 1: CLEAN pre-wait, BEHIND post-wait -> loop
    # iteration 2: BEHIND pre-wait -> update; iteration 3: CLEAN, CLEAN -> merge
    gh = _gh(merge_states=["CLEAN", "BEHIND", "BEHIND", "CLEAN", "CLEAN"])
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    gh.update_branch.assert_called_once_with("99")
    assert gh.wait_for_checks.call_count == 2
    gh.merge.assert_called_once()


def test_check_failure_aborts_with_names() -> None:
    gh = _gh(failed=["ci / test", "vergil-audit/approved"])
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="ci / test"):
        wait_and_merge("99", strategy="squash")
    gh.merge.assert_not_called()


def test_wait_and_merge_aborts_on_orphaned_check() -> None:
    gh = _gh()
    gh.wait_for_checks.side_effect = OrphanedCheckError("docs / docs")
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="orphan"):
        wait_and_merge("934", strategy="squash")
    gh.merge.assert_not_called()


def test_merge_train_guard_exhausts() -> None:
    gh = _gh(merge_states=["BEHIND"] * 10)
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbortError, match="still behind"),
    ):
        wait_and_merge("99", strategy="squash")
    assert gh.update_branch.call_count == 5


def test_injected_wait_callable_is_used() -> None:
    gh = _gh()
    waiter = MagicMock()
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="merge", wait_checks=waiter)
    waiter.assert_called_once_with("99")
    gh.wait_for_checks.assert_not_called()


def test_conflict_arising_mid_loop_aborts() -> None:
    """A conflict appearing after a BEHIND update still aborts — the
    per-iteration re-check is the multi-worktree guarantee."""
    gh = _gh(merge_states=["BEHIND", "CLEAN"])
    gh.mergeable.side_effect = ["MERGEABLE", "CONFLICTING"]
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbortError, match="merge conflicts"),
    ):
        wait_and_merge("99", strategy="squash")
    gh.update_branch.assert_called_once_with("99")
    gh.merge.assert_not_called()


def test_update_branch_failure_aborts_cleanly() -> None:
    gh = _gh(merge_states=["BEHIND"])
    gh.update_branch.side_effect = GitHubAPIError(1, "update-branch", stderr="boom")
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbortError, match="update-branch failed"),
    ):
        wait_and_merge("99", strategy="squash")
    gh.merge.assert_not_called()


_NOT_UP_TO_DATE_STDERR = (
    "Pull request org/repo#278 is not mergeable: the head branch is not up to "
    "date with the base branch."
)


def test_merge_endpoint_not_up_to_date_updates_and_retries() -> None:
    """mergeStateStatus lagged (never BEHIND) but the merge endpoint rejects the
    stale-behind branch — the authoritative rejection must drive update+retry."""
    gh = _gh(merge_states=["CLEAN", "CLEAN", "CLEAN", "CLEAN"])
    gh.merge.side_effect = [
        GitHubAPIError(1, "gh pr merge", stderr=_NOT_UP_TO_DATE_STDERR),
        None,
    ]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    gh.update_branch.assert_called_once_with("99")
    assert gh.merge.call_count == 2


def test_merge_endpoint_not_up_to_date_respects_update_cap() -> None:
    """A merge endpoint that keeps rejecting as behind is bounded by the same
    branch-update cap as the mergeStateStatus path — it never loops forever."""
    gh = _gh(merge_states=["CLEAN"] * 40)
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr=_NOT_UP_TO_DATE_STDERR)
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbortError, match="still behind"),
    ):
        wait_and_merge("99", strategy="squash")
    assert gh.update_branch.call_count == 5


def test_merge_endpoint_unrelated_error_reraises() -> None:
    """A merge failure that is not a stale-behind rejection is a real error —
    it must propagate, not be swallowed into an update-and-retry loop."""
    gh = _gh(merge_states=["CLEAN", "CLEAN"])
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr="required status check failing")
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(GitHubAPIError, match="required status check"),
    ):
        wait_and_merge("99", strategy="squash")
    gh.update_branch.assert_not_called()
