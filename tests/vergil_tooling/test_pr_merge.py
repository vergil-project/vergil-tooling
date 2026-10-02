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
    required: frozenset[str] | None = frozenset(),
    outstanding: list[dict[str, str]] | None = None,
    review: str = "",
) -> MagicMock:
    """Build a mocked github module for one scenario.

    ``outstanding`` is the sequence of ``required_checks_outstanding`` results,
    one per call; once exhausted (or when omitted) nothing is outstanding.
    """
    gh = MagicMock()
    gh.pr_state.return_value = state
    gh.is_draft.return_value = draft
    gh.mergeable.return_value = mergeable
    gh.merge_state_status.side_effect = merge_states or ["CLEAN", "CLEAN"]
    gh.failed_check_names.return_value = failed or []
    gh.required_check_names.return_value = required
    pending = iter(outstanding or [])
    gh.required_checks_outstanding.side_effect = lambda pr, req: next(pending, {})
    gh.merge_status.return_value = {"mergeStateStatus": "BLOCKED", "reviewDecision": review}
    return gh


def test_green_first_try_merges() -> None:
    gh = _gh()
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="squash")
    gh.wait_for_checks.assert_called_once_with("99", required=frozenset())
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
    gh.wait_for_checks.assert_called_once_with("99", required=frozenset())
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


# --- required-check awareness and policy-block retry (#3061) ---------------

_EVIDENCE = frozenset({"test / evidence", "quality / evidence"})
_POLICY_STDERR = (
    "X Pull request org/repo#1270 is not mergeable: the base branch policy prohibits the merge."
)


def _policy_error() -> GitHubAPIError:
    return GitHubAPIError(1, "gh pr merge", stderr=_POLICY_STDERR)


def test_default_waiter_receives_resolved_required_checks() -> None:
    gh = _gh(required=_EVIDENCE)
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="squash")
    gh.required_check_names.assert_called_once_with("99")
    gh.wait_for_checks.assert_called_once_with("99", required=_EVIDENCE)
    gh.merge.assert_called_once()


def test_required_lookup_failure_falls_back_to_registered_checks() -> None:
    """``None`` (lookup failed; the lib already warned) waits on registered checks only."""
    gh = _gh(required=None)
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="squash")
    gh.wait_for_checks.assert_called_once_with("99", required=frozenset())
    gh.merge.assert_called_once()


def test_required_check_missing_after_waiter_keeps_waiting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A waiter that only watches registered checks returns while a required
    aggregator has not registered — the engine must wait, not merge."""
    gh = _gh(
        merge_states=["CLEAN"] * 10,
        required=_EVIDENCE,
        outstanding=[{"test / evidence": "not registered"}, {"test / evidence": "pending"}],
    )
    waiter = MagicMock()
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep") as sleep:
        wait_and_merge("99", strategy="merge", wait_checks=waiter)
    assert waiter.call_count == 3
    assert sleep.call_count == 2
    gh.merge.assert_called_once()
    out = capsys.readouterr().out
    assert "awaiting required checks: test / evidence (not registered)" in out
    assert "awaiting required checks: test / evidence (pending)" in out


def test_required_check_outstanding_past_deadline_aborts() -> None:
    gh = _gh(
        merge_states=["CLEAN"] * 10,
        required=_EVIDENCE,
        outstanding=[{"test / evidence": "not registered"}] * 5,
    )
    clock = iter([0.0, 2000.0])
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".time.monotonic", side_effect=lambda: next(clock)),
        pytest.raises(MergeAbortError, match=r"test / evidence \(not registered\)"),
    ):
        wait_and_merge("99", strategy="squash", wait_checks=MagicMock())
    gh.merge.assert_not_called()


def test_policy_block_without_failures_retries_then_merges(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The #1270 failure: merge refused by branch policy while a required check
    had not registered — retry instead of aborting finalize."""
    gh = _gh(
        merge_states=["CLEAN"] * 10,
        required=_EVIDENCE,
        # call 1: post-wait gate (clear); call 2: policy-block reason; then clear.
        outstanding=[{}, {"quality / evidence": "not registered"}],
    )
    gh.merge.side_effect = [_policy_error(), None]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep") as sleep:
        wait_and_merge("99", strategy="squash")
    assert gh.merge.call_count == 2
    sleep.assert_called_once()
    out = capsys.readouterr().out
    assert (
        "Not ready to merge — the base branch policy prohibits the merge; "
        "awaiting required checks: quality / evidence (not registered)"
    ) in out


def test_policy_block_past_deadline_names_outstanding_checks() -> None:
    gh = _gh(
        merge_states=["CLEAN"] * 10,
        required=_EVIDENCE,
        outstanding=[{}, {"quality / evidence": "pending"}, {}, {"quality / evidence": "pending"}],
    )
    gh.merge.side_effect = _policy_error()
    clock = iter([0.0, 1800.0])
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".time.monotonic", side_effect=lambda: next(clock)),
        pytest.raises(MergeAbortError, match=r"after 1800s: .*quality / evidence \(pending\)"),
    ):
        wait_and_merge("99", strategy="squash")
    assert gh.merge.call_count == 2


def test_policy_block_with_failed_checks_aborts() -> None:
    gh = _gh(merge_states=["CLEAN"] * 4, required=_EVIDENCE)
    gh.failed_check_names.side_effect = [[], ["test / evidence"]]
    gh.merge.side_effect = _policy_error()
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep") as sleep,
        pytest.raises(MergeAbortError, match="Checks failed on PR 99: test / evidence"),
    ):
        wait_and_merge("99", strategy="squash")
    sleep.assert_not_called()


@pytest.mark.parametrize("review", ["REVIEW_REQUIRED", "CHANGES_REQUESTED"])
def test_policy_block_needing_review_aborts_immediately(review: str) -> None:
    gh = _gh(merge_states=["CLEAN"] * 4, required=_EVIDENCE, review=review)
    gh.merge.side_effect = _policy_error()
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep") as sleep,
        pytest.raises(MergeAbortError, match=f"reviewDecision={review}"),
    ):
        wait_and_merge("99", strategy="squash")
    sleep.assert_not_called()
    gh.merge.assert_called_once()


def test_policy_block_with_unreadable_required_checks_says_so(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = _gh(merge_states=["CLEAN"] * 10, required=None)
    gh.merge.side_effect = [_policy_error(), None]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    assert "required checks could not be read" in capsys.readouterr().out


def test_policy_block_with_nothing_outstanding_reports_merge_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = _gh(merge_states=["CLEAN"] * 10, required=_EVIDENCE)
    gh.merge.side_effect = [_policy_error(), None]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    out = capsys.readouterr().out
    assert "no outstanding required checks (mergeStateStatus=BLOCKED)" in out


def test_policy_block_with_blank_merge_state_reports_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = _gh(merge_states=["CLEAN"] * 10, required=_EVIDENCE)
    gh.merge_status.return_value = {"mergeStateStatus": "", "reviewDecision": ""}
    gh.merge.side_effect = [_policy_error(), None]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    assert "(mergeStateStatus=unknown)" in capsys.readouterr().out
