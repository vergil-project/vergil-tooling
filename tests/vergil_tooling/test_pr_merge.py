"""Loop state-table tests for vergil_tooling.lib.pr_merge."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.github import ChecksTimeoutError, GitHubAPIError, OrphanedCheckError
from vergil_tooling.lib.pr_merge import (
    MergeAbortError,
    MergeConflictError,
    MergeDeadlineError,
    wait_and_merge,
    wait_until_merged,
)

if TYPE_CHECKING:
    from collections.abc import Callable

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
    # A MergeConflictError, so vrg-finalize-pr can swap in its exact commands
    # (#3555); still a MergeAbortError for every other caller.
    with patch(_MOD + ".github", gh), pytest.raises(MergeConflictError, match="merge conflicts"):
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


def test_failure_naming_an_unregistered_uploaded_check_names_its_job() -> None:
    # #4294: the waiter returned at once because security / semgrep failed
    # before uploading, so Semgrep OSS never registered.
    gh = _gh(
        failed=["security / semgrep"],
        required=frozenset({"security / semgrep", "Semgrep OSS"}),
        outstanding=[{"Semgrep OSS": "not registered"}],
    )
    gh.pr_checks.return_value = [
        {"name": "security / semgrep", "bucket": "fail"},
        {"name": "test / unit / 3.14", "bucket": "pending"},
    ]
    with (
        patch(_MOD + ".github", gh),
        pytest.raises(MergeAbortError) as err,
    ):
        wait_and_merge("99", strategy="squash")
    assert str(err.value) == (
        "Checks failed on PR 99: security / semgrep. Required check Semgrep OSS was "
        "never registered and will not arrive: its producing job security / semgrep "
        "(failed) ended before uploading it."
    )
    gh.wait_for_checks.assert_called_once()
    gh.pr_checks.assert_called_once_with("99")
    gh.merge.assert_not_called()


def test_failure_naming_an_unregistered_check_with_no_known_job() -> None:
    gh = _gh(
        failed=["test / unit / 3.14"],
        required=frozenset({"test / evidence", "Semgrep OSS"}),
        outstanding=[{"Semgrep OSS": "not registered", "test / evidence": "not registered"}],
    )
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError) as err:
        wait_and_merge("99", strategy="squash")
    assert str(err.value) == (
        "Checks failed on PR 99: test / unit / 3.14. Required checks never registered: "
        "Semgrep OSS, test / evidence; a failed job above most likely produces them."
    )
    gh.pr_checks.assert_not_called()
    gh.merge.assert_not_called()


def test_failure_naming_two_unregistered_uploaded_checks_reads_the_checks_once() -> None:
    gh = _gh(
        failed=["security / semgrep", "security / trivy"],
        required=frozenset({"Semgrep OSS", "Trivy"}),
        outstanding=[{"Semgrep OSS": "not registered", "Trivy": "not registered"}],
    )
    gh.pr_checks.return_value = [
        {"name": "security / semgrep", "bucket": "fail"},
        {"name": "security / trivy", "bucket": "fail"},
    ]
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError) as err:
        wait_and_merge("99", strategy="squash")
    assert "producing job security / semgrep (failed)" in str(err.value)
    assert "producing job security / trivy (failed)" in str(err.value)
    gh.pr_checks.assert_called_once_with("99")


def test_failure_naming_known_and_unknown_unregistered_checks() -> None:
    gh = _gh(
        failed=["security / trivy", "test / unit / 3.14"],
        required=frozenset({"Trivy", "test / evidence"}),
        outstanding=[{"Trivy": "not registered", "test / evidence": "not registered"}],
    )
    gh.pr_checks.return_value = [{"name": "security / trivy", "bucket": "cancel"}]
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError) as err:
        wait_and_merge("99", strategy="squash")
    assert str(err.value) == (
        "Checks failed on PR 99: security / trivy, test / unit / 3.14. Required check "
        "Trivy was never registered and will not arrive: its producing job security / "
        "trivy (cancelled) ended before uploading it. Required checks never registered: "
        "test / evidence; a failed job above most likely produces them."
    )


def test_failure_with_pending_required_check_names_failures_only() -> None:
    gh = _gh(
        failed=["ci / test"],
        required=frozenset({"test / evidence"}),
        outstanding=[{"test / evidence": "pending"}],
    )
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError) as err:
        wait_and_merge("99", strategy="squash")
    assert str(err.value) == "Checks failed on PR 99: ci / test"


def test_watch_failure_naming_an_unregistered_uploaded_check_names_its_job() -> None:
    gh = _gh(
        failed=["security / semgrep"],
        required=frozenset({"Semgrep OSS"}),
        outstanding=[{"Semgrep OSS": "not registered"}],
    )
    gh.pr_checks.return_value = [{"name": "security / semgrep", "bucket": "fail"}]
    with (
        patch(_MOD + ".github", gh),
        pytest.raises(MergeAbortError, match=r"producing job security / semgrep \(failed\)"),
    ):
        wait_until_merged("99", strategy="squash", deadline_secs=600)
    gh.merge.assert_not_called()


def test_wait_and_merge_aborts_on_orphaned_check() -> None:
    gh = _gh()
    gh.wait_for_checks.side_effect = OrphanedCheckError("docs / docs")
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="orphan"):
        wait_and_merge("934", strategy="squash")
    gh.merge.assert_not_called()


def test_wait_and_merge_orphan_abort_carries_the_waiters_detail() -> None:
    # The waiter's message (which check, why it cannot pass, how to recover)
    # must reach the operator, not be replaced by a generic one (#3379).
    detail = "test / evidence (job succeeded, but it is a required status check ...)"
    gh = _gh()
    gh.wait_for_checks.side_effect = OrphanedCheckError(detail)
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError) as excinfo:
        wait_and_merge("934", strategy="squash")
    assert str(excinfo.value) == f"PR 934 cannot be merged (orphaned check-run): {detail}"


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
    """``None`` (lookup failed; the lib already warned) waits on registered checks only.

    The waiter receives ``None`` itself — not ``frozenset()`` — so it knows the
    required set is unknown and never passes a stale check (#3379); it is not
    looked up a second time.
    """
    gh = _gh(required=None)
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="squash")
    gh.required_check_names.assert_called_once_with("99")
    gh.wait_for_checks.assert_called_once_with("99", required=None)
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
    clock = iter([0.0, 5000.0])
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
    clock = iter([0.0, 4500.0])
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".time.monotonic", side_effect=lambda: next(clock)),
        pytest.raises(MergeAbortError, match=r"after 4500s: .*quality / evidence \(pending\)"),
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


# --- #3170: a check wait that expires aborts loudly with a recovery path ---

_JOB = "https://github.com/o/r/actions/runs/1/job/2"


def test_check_wait_deadline_aborts_naming_pending_checks_and_recovery() -> None:
    gh = _gh()
    gh.wait_for_checks.side_effect = ChecksTimeoutError(
        "99",
        f"checks still pending after 1800s: quality / matrix ({_JOB})",
        [("quality / matrix", _JOB)],
    )
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError) as excinfo:
        wait_and_merge("99", strategy="merge")
    message = str(excinfo.value)
    assert f"quality / matrix ({_JOB})" in message
    assert "after 1800s" in message
    assert "vrg-release --resume" in message
    assert "Re-run the stuck job" in message
    assert "Still running" not in message  # no backing run is known to be going
    gh.merge.assert_not_called()


# --- #4200: a check over a run still in progress is slow, not stuck -----------

_RUN = "https://github.com/o/r/actions/runs/1"
_BUILD = "images / build: pkg-ubuntu:24.04 + 14 descendants (amd64)"


def test_check_wait_deadline_on_a_running_job_says_wait_not_rerun() -> None:
    gh = _gh()
    gh.wait_for_checks.side_effect = ChecksTimeoutError(
        "99",
        f"checks still pending after 4500s: {_BUILD} ({_JOB})",
        [(_BUILD, _JOB)],
        [(_BUILD, _RUN)],
    )
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError) as excinfo:
        wait_and_merge("99", strategy="merge")
    message = str(excinfo.value)
    assert f"{_BUILD} ({_JOB})" in message
    assert f"Still running, not stuck: {_BUILD} (run {_RUN}); the job is in progress." in message
    assert "Wait for the run to finish, then re-run the waiting command" in message
    assert "vrg-release --resume for a release, vrg-update-deps for a dependency update" in message
    assert "$VRG_CHECKS_TIMEOUT" in message
    assert "Re-run the job instead only if its log has stopped making progress" in message
    assert "Re-run the stuck job" not in message
    assert "close and reopen" not in message
    gh.merge.assert_not_called()


def test_check_wait_deadline_with_running_and_stuck_checks_advises_each() -> None:
    gh = _gh()
    gh.wait_for_checks.side_effect = ChecksTimeoutError(
        "99",
        f"checks still pending after 4500s: {_BUILD} ({_JOB}), CodeQL (no link)",
        [(_BUILD, _JOB), ("CodeQL", "")],
        [(_BUILD, _RUN)],
    )
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError) as excinfo:
        wait_and_merge("99", strategy="merge")
    message = str(excinfo.value)
    assert f"Still running, not stuck: {_BUILD} (run {_RUN}); the job is in progress." in message
    assert "Not in progress: CodeQL. Re-run the stuck job (or close and reopen" in message
    assert message.index("Still running") < message.index("Not in progress")
    gh.merge.assert_not_called()


def test_not_ready_deadline_follows_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VRG_CHECKS_TIMEOUT", "120")
    gh = _gh(
        merge_states=["CLEAN"] * 10,
        required=_EVIDENCE,
        outstanding=[{"test / evidence": "pending"}] * 5,
    )
    clock = iter([0.0, 120.0])
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        patch(_MOD + ".time.monotonic", side_effect=lambda: next(clock)),
        pytest.raises(MergeAbortError, match="still not mergeable after 120s"),
    ):
        wait_and_merge("99", strategy="merge", wait_checks=MagicMock())


# --- #3175: the caller is told each time the engine updates the branch ---


def test_on_branch_updated_fires_after_each_update() -> None:
    gh = _gh(merge_states=["BEHIND", "BEHIND", "CLEAN", "CLEAN"])
    order: list[str] = []
    gh.update_branch.side_effect = lambda pr: order.append("update")
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="merge", on_branch_updated=lambda: order.append("hook"))
    assert order == ["update", "hook", "update", "hook"]
    gh.merge.assert_called_once()


def test_on_branch_updated_fires_after_behind_merge_rejection() -> None:
    gh = _gh(merge_states=["CLEAN", "CLEAN", "CLEAN", "CLEAN"])
    gh.merge.side_effect = [
        GitHubAPIError(1, "merge", stderr="Head branch is not up to date with the base branch"),
        None,
    ]
    hook = MagicMock()
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="merge", on_branch_updated=hook)
    hook.assert_called_once_with()
    assert gh.merge.call_count == 2


def test_on_branch_updated_not_fired_without_update() -> None:
    gh = _gh()
    hook = MagicMock()
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="merge", on_branch_updated=hook)
    hook.assert_not_called()


def test_on_branch_updated_not_fired_when_update_fails() -> None:
    gh = _gh(merge_states=["BEHIND"])
    gh.update_branch.side_effect = GitHubAPIError(1, "update-branch", stderr="boom")
    hook = MagicMock()
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="update-branch"):
        wait_and_merge("99", strategy="merge", on_branch_updated=hook)
    hook.assert_not_called()


def test_on_branch_updated_failure_propagates_before_merge() -> None:
    gh = _gh(merge_states=["BEHIND", "CLEAN", "CLEAN"])

    def hook() -> None:
        msg = "local branch diverged"
        raise RuntimeError(msg)

    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(RuntimeError, match="local branch diverged"),
    ):
        wait_and_merge("99", strategy="merge", on_branch_updated=hook)
    gh.merge.assert_not_called()


# --- #2914: stale failed checks on a BEHIND branch update, not abort -------


def test_stale_failed_check_on_behind_branch_updates_and_merges() -> None:
    """The lazy mergeStateStatus precheck misses a freshly-behind branch, the
    waiter returns at once on stale terminal checks, and one of them is red —
    the failed-checks gate must re-check BEHIND and update, not abort."""
    # iteration 1: CLEAN pre-wait (lazy miss), BEHIND at the failed-checks recheck
    # iteration 2: CLEAN pre-wait, CLEAN post-wait -> merge
    gh = _gh(merge_states=["CLEAN", "BEHIND", "CLEAN", "CLEAN"])
    gh.failed_check_names.side_effect = [["quality / common"], []]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("208", strategy="squash")
    gh.update_branch.assert_called_once_with("208")
    assert gh.wait_for_checks.call_count == 2
    gh.merge.assert_called_once_with("208", strategy="squash")


def test_genuine_failed_check_on_current_branch_still_aborts() -> None:
    """A red check on a branch that is not BEHIND is a real failure — abort
    without touching the branch."""
    gh = _gh(merge_states=["CLEAN", "CLEAN"], failed=["quality / common"])
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbortError, match="Checks failed on PR 208: quality / common"),
    ):
        wait_and_merge("208", strategy="squash")
    gh.update_branch.assert_not_called()
    gh.merge.assert_not_called()


def test_stale_failed_check_on_behind_branch_respects_update_cap() -> None:
    """A branch that keeps coming back red and BEHIND is bounded by the shared
    branch-update cap — it never loops forever."""
    gh = _gh(merge_states=["CLEAN", "BEHIND"] * 10, failed=["quality / common"])
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbortError, match="still behind"),
    ):
        wait_and_merge("208", strategy="squash")
    assert gh.update_branch.call_count == 5
    gh.merge.assert_not_called()


# --- batch PRs: rebase update, re-check, pinned merge (epic .github#1019) ----

_HEAD_MOVED_STDERR = "GraphQL: Head branch was modified. Review and try the merge again."


class _BatchCheck:
    """A batch check that records each call in *events* and returns the next SHA."""

    def __init__(self, events: list[str], shas: list[str]) -> None:
        self.events = events
        self.shas = iter(shas)
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        sha = next(self.shas)
        self.events.append(f"check:{sha}")
        return sha


def _batch_gh(events: list[str], *, merge_states: list[str]) -> MagicMock:
    """A mocked github module whose update, wait and merge calls land in *events*."""
    gh = _gh(merge_states=merge_states)
    gh.head_sha.return_value = "oldhead"
    gh.update_branch_rebase.side_effect = lambda pr, expected_head: events.append(
        f"rebase:{expected_head}"
    )
    gh.wait_for_checks.side_effect = lambda pr, required: events.append("wait")

    def merge(pr: str, *, strategy: str, match_head: str | None = None) -> None:
        events.append(f"merge:{strategy}:{match_head}")

    gh.merge.side_effect = merge
    return gh


def test_behind_batch_pr_updates_by_rebase_not_merge() -> None:
    events: list[str] = []
    gh = _batch_gh(events, merge_states=["BEHIND", "CLEAN", "CLEAN"])
    check = _BatchCheck(events, ["h1", "h1"])
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="rebase", batch_check=check)
    gh.update_branch.assert_not_called()
    gh.update_branch_rebase.assert_called_once_with("99", expected_head="oldhead")


def test_batch_check_runs_after_each_update_and_before_merge() -> None:
    events: list[str] = []
    # BEHIND at entry, BEHIND again after the wait, then current.
    gh = _batch_gh(events, merge_states=["BEHIND", "CLEAN", "BEHIND", "BEHIND", "CLEAN", "CLEAN"])
    check = _BatchCheck(events, ["h1", "h2", "h2"])
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="rebase", batch_check=check)
    assert events == [
        "rebase:oldhead",
        "check:h1",
        "wait",
        "rebase:oldhead",
        "check:h2",
        "wait",
        "check:h2",
        "merge:rebase:h2",
    ]


def test_batch_merge_pins_the_checked_head() -> None:
    """The merge carries --match-head-commit <checked sha>; a head that moved
    after the check is refused by GitHub, re-checked, and only then merged."""
    events: list[str] = []
    gh = _batch_gh(events, merge_states=["CLEAN"] * 6)
    attempts = iter([GitHubAPIError(1, "gh pr merge", stderr=_HEAD_MOVED_STDERR), None])

    def merge(pr: str, *, strategy: str, match_head: str | None = None) -> None:
        events.append(f"merge:{strategy}:{match_head}")
        error = next(attempts)
        if error is not None:
            raise error

    gh.merge.side_effect = merge
    check = _BatchCheck(events, ["h1", "h2"])
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="rebase", batch_check=check)
    assert events == ["wait", "check:h1", "merge:rebase:h1", "wait", "check:h2", "merge:rebase:h2"]


def test_batch_head_moved_rechecks_are_bounded_by_the_update_cap() -> None:
    events: list[str] = []
    gh = _batch_gh(events, merge_states=["CLEAN"] * 40)
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr=_HEAD_MOVED_STDERR)
    check = _BatchCheck(events, [f"h{i}" for i in range(10)])
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbortError, match="still behind or moving after 5"),
    ):
        wait_and_merge("99", strategy="rebase", batch_check=check)
    assert gh.merge.call_count == 6


def test_head_moved_rejection_without_batch_check_reraises() -> None:
    """A single-issue merge never pins its head, so the rejection is not retried."""
    gh = _gh(merge_states=["CLEAN", "CLEAN"])
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr=_HEAD_MOVED_STDERR)
    with patch(_MOD + ".github", gh), pytest.raises(GitHubAPIError, match="Head branch"):
        wait_and_merge("99", strategy="squash")


def test_batch_check_failure_propagates_without_merge() -> None:
    events: list[str] = []
    gh = _batch_gh(events, merge_states=["CLEAN", "CLEAN"])

    def refuse() -> str:
        raise RuntimeError("one commit per issue")

    with (
        patch(_MOD + ".github", gh),
        pytest.raises(RuntimeError, match="one commit per issue"),
    ):
        wait_and_merge("99", strategy="rebase", batch_check=refuse)
    gh.merge.assert_not_called()


def test_batch_rebase_update_failure_aborts() -> None:
    gh = _gh(merge_states=["BEHIND"])
    gh.head_sha.return_value = "oldhead"
    gh.update_branch_rebase.side_effect = GitHubAPIError(1, "gh api graphql", stderr="conflict")
    with (
        patch(_MOD + ".github", gh),
        pytest.raises(MergeAbortError, match="update-branch failed"),
    ):
        wait_and_merge("99", strategy="rebase", batch_check=lambda: "h1")
    gh.update_branch.assert_not_called()
    gh.merge.assert_not_called()


def test_behind_single_issue_pr_still_uses_update_branch() -> None:
    """Review focus 5: without a batch check, BEHIND still merges the base in."""
    gh = _gh(merge_states=["BEHIND", "CLEAN", "CLEAN"])
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    gh.update_branch.assert_called_once_with("99")
    gh.update_branch_rebase.assert_not_called()
    gh.head_sha.assert_not_called()
    gh.merge.assert_called_once_with("99", strategy="squash")


# --- wait_until_merged: mechanized PRs watch until merged (#4370) -----------

_URL = "https://github.com/o/r/pull/9"


def _clock(*values: float) -> Callable[[], float]:
    """A ``time.monotonic`` stand-in returning *values*, then the last one forever."""
    ticks = iter(values)
    last = [values[-1]]

    def tick() -> float:
        last[0] = next(ticks, last[0])
        return last[0]

    return tick


def _watch(
    gh: MagicMock, *, deadline: int = 2400, clock: Callable[[], float] | None = None
) -> None:
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch(_MOD + ".github", gh))
        stack.enter_context(patch(_MOD + ".time.sleep"))
        if clock is not None:
            stack.enter_context(patch(_MOD + ".time.monotonic", side_effect=clock))
        wait_until_merged(_URL, strategy="squash", deadline_secs=deadline)


def test_watch_ready_pr_is_merged_at_once() -> None:
    gh = _gh()
    _watch(gh)
    gh.wait_for_checks.assert_called_once_with(_URL, poll_timeout=2400, required=frozenset())
    gh.merge.assert_called_once_with(_URL, strategy="squash")
    gh.update_branch.assert_not_called()


def test_watch_already_merged_pr_is_success_without_waiting() -> None:
    gh = _gh(state="MERGED")
    _watch(gh)
    gh.wait_for_checks.assert_not_called()
    gh.merge.assert_not_called()


def test_watch_pr_behind_once_is_updated_then_merged() -> None:
    # iteration 1: BEHIND -> update; iteration 2: CLEAN pre-wait, CLEAN post-wait -> merge
    gh = _gh(merge_states=["BEHIND", "CLEAN", "CLEAN"])
    _watch(gh)
    gh.update_branch.assert_called_once_with(_URL)
    gh.wait_for_checks.assert_called_once()
    gh.merge.assert_called_once_with(_URL, strategy="squash")


def test_watch_pr_that_falls_behind_during_the_wait_is_updated_then_merged() -> None:
    # iteration 1: CLEAN pre-wait, BEHIND after the wait -> loop;
    # iteration 2: BEHIND -> update; iteration 3: CLEAN, CLEAN -> merge
    gh = _gh(merge_states=["CLEAN", "BEHIND", "BEHIND", "CLEAN", "CLEAN"])
    _watch(gh)
    gh.update_branch.assert_called_once_with(_URL)
    assert gh.wait_for_checks.call_count == 2
    gh.merge.assert_called_once()


def test_watch_merged_by_auto_merge_during_the_wait_is_success() -> None:
    gh = _gh()
    gh.pr_state.side_effect = ["OPEN", "MERGED"]
    _watch(gh)
    gh.merge.assert_not_called()
    gh.failed_check_names.assert_not_called()


def test_watch_merge_refused_because_auto_merge_won_the_race_is_success() -> None:
    gh = _gh()
    gh.pr_state.side_effect = ["OPEN", "OPEN", "MERGED"]
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr="Pull request is not open")
    _watch(gh)
    gh.merge.assert_called_once()


def test_watch_failed_ci_stops_naming_the_checks() -> None:
    gh = _gh(merge_states=["CLEAN", "CLEAN"], failed=["ci / test"])
    with pytest.raises(MergeAbortError, match=r"Checks failed on PR .*pull/9: ci / test"):
        _watch(gh)
    gh.merge.assert_not_called()
    gh.update_branch.assert_not_called()


def test_watch_stale_failed_ci_on_a_behind_branch_updates_and_merges() -> None:
    # #2914: red checks on a BEHIND branch are stale; update rather than stop.
    gh = _gh(merge_states=["CLEAN", "BEHIND", "CLEAN", "CLEAN"])
    gh.failed_check_names.side_effect = [["quality / common"], []]
    _watch(gh)
    gh.update_branch.assert_called_once_with(_URL)
    gh.merge.assert_called_once()


def test_watch_closed_pr_stops() -> None:
    gh = _gh(state="CLOSED")
    with pytest.raises(MergeAbortError, match="was closed without merging"):
        _watch(gh)
    gh.wait_for_checks.assert_not_called()


def test_watch_pr_closed_during_the_wait_stops() -> None:
    gh = _gh()
    gh.pr_state.side_effect = ["OPEN", "CLOSED"]
    with pytest.raises(MergeAbortError, match="was closed without merging"):
        _watch(gh)
    gh.merge.assert_not_called()


def test_watch_conflicting_pr_stops() -> None:
    gh = _gh(mergeable="CONFLICTING")
    with pytest.raises(MergeConflictError, match="merge conflicts"):
        _watch(gh)
    gh.wait_for_checks.assert_not_called()


def test_watch_update_cap_stops() -> None:
    gh = _gh(merge_states=["BEHIND"] * 10)
    with pytest.raises(MergeAbortError, match="still behind after 5 branch updates"):
        _watch(gh)
    assert gh.update_branch.call_count == 5
    gh.merge.assert_not_called()


def test_watch_merge_refused_as_behind_updates_and_retries() -> None:
    gh = _gh(merge_states=["CLEAN"] * 10)
    gh.merge.side_effect = [
        GitHubAPIError(1, "gh pr merge", stderr="the head branch is not up to date"),
        None,
    ]
    _watch(gh)
    gh.update_branch.assert_called_once_with(_URL)
    assert gh.merge.call_count == 2


def test_watch_merge_refused_by_policy_waits_a_poll_then_merges() -> None:
    gh = _gh(merge_states=["CLEAN"] * 10)
    gh.merge.side_effect = [
        GitHubAPIError(1, "gh pr merge", stderr="the base branch policy prohibits the merge"),
        None,
    ]
    _watch(gh)
    assert gh.merge.call_count == 2


def test_watch_unknown_merge_failure_propagates() -> None:
    gh = _gh(merge_states=["CLEAN"] * 10)
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr="HTTP 500")
    with pytest.raises(GitHubAPIError):
        _watch(gh)


def test_watch_required_check_outstanding_waits_then_merges() -> None:
    gh = _gh(
        merge_states=["CLEAN"] * 10,
        required=_EVIDENCE,
        outstanding=[{"test / evidence": "not registered"}],
    )
    _watch(gh)
    assert gh.wait_for_checks.call_count == 2
    gh.merge.assert_called_once()


def test_watch_deadline_reached_stops_before_waiting_again() -> None:
    # start 0; iteration 1 sees 100s left, the PR falls behind and is updated;
    # iteration 2 finds the deadline passed.
    gh = _gh(merge_states=["BEHIND"] * 10)
    gh.format_wait.side_effect = lambda secs: f"{int(secs)}s"
    with pytest.raises(MergeDeadlineError, match=r"pull/9 did not merge within the 2400s deadline"):
        _watch(gh, clock=_clock(0.0, 2300.0, 2400.0))
    gh.update_branch.assert_called_once()
    gh.wait_for_checks.assert_not_called()


def test_watch_check_wait_is_clamped_to_what_is_left_of_the_deadline() -> None:
    gh = _gh()
    _watch(gh, clock=_clock(0.0, 2000.5))
    gh.wait_for_checks.assert_called_once_with(_URL, poll_timeout=400, required=frozenset())


def test_watch_check_wait_never_exceeds_the_checks_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VRG_CHECKS_TIMEOUT", "120")
    gh = _gh()
    _watch(gh)
    gh.wait_for_checks.assert_called_once_with(_URL, poll_timeout=120, required=frozenset())


def test_watch_checks_still_pending_at_the_deadline_stop_naming_them() -> None:
    gh = _gh()
    gh.format_wait.side_effect = lambda secs: f"{int(secs)}s"
    gh.wait_for_checks.side_effect = ChecksTimeoutError(
        _URL, "checks still pending after 2400s: ci / test (link)", [("ci / test", "link")]
    )
    with pytest.raises(MergeDeadlineError) as excinfo:
        _watch(gh, clock=_clock(0.0, 0.0, 2400.0))
    message = str(excinfo.value)
    assert "did not merge within the 2400s deadline (waited 2400s)" in message
    assert "ci / test (link)" in message
    gh.merge.assert_not_called()


def test_watch_check_timeout_inside_the_deadline_is_the_usual_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VRG_CHECKS_TIMEOUT", "60")
    gh = _gh()
    gh.wait_for_checks.side_effect = ChecksTimeoutError(
        _URL, "checks still pending after 60s: ci / test (link)", [("ci / test", "link")]
    )
    with pytest.raises(MergeAbortError, match="cannot be merged yet") as excinfo:
        _watch(gh, clock=_clock(0.0, 0.0, 60.0))
    assert not isinstance(excinfo.value, MergeDeadlineError)


def test_watch_orphaned_check_stops() -> None:
    gh = _gh()
    gh.wait_for_checks.side_effect = OrphanedCheckError("docs / docs")
    with pytest.raises(MergeAbortError, match="orphaned check-run"):
        _watch(gh)
    gh.merge.assert_not_called()


def test_watch_not_ready_pause_never_sleeps_past_the_deadline() -> None:
    gh = _gh(
        merge_states=["CLEAN"] * 10,
        required=_EVIDENCE,
        outstanding=[{"test / evidence": "pending"}],
    )
    # start 0; loop top 2398 (2s left); the pause sees 2398 too; then loop top 2400.
    gh.format_wait.side_effect = lambda secs: f"{int(secs)}s"
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep") as sleep,
        patch(_MOD + ".time.monotonic", side_effect=_clock(0.0, 2398.0, 2398.0, 2400.0)),
        pytest.raises(MergeDeadlineError),
    ):
        wait_until_merged(_URL, strategy="squash", deadline_secs=2400)
    sleep.assert_called_once_with(2.0)


# --- the ruleset-bypassed merge into main (#5095) -----------------------------

_RULESET = "Main updates"
_RULE_VIOLATION_STDERR = (
    "GraphQL: Repository rule violations found\n\nCannot update this protected ref.\n"
)


def _bypass_gh(
    *,
    failed: list[str] | None = None,
    required: frozenset[str] | None = frozenset(),
    outstanding: list[dict[str, str]] | None = None,
    review: str = "",
) -> MagicMock:
    """A green, current PR whose head is ``h1``: the bypass path's starting point."""
    gh = _gh(
        merge_states=["CLEAN"] * 20,
        failed=failed,
        required=required,
        outstanding=outstanding,
        review=review,
    )
    gh.head_sha.return_value = "h1"
    return gh


def test_bypass_merges_a_ruleset_blocked_pr_with_admin_pinned_to_the_head() -> None:
    """A green PR whose only block is the main ruleset (mergeStateStatus BLOCKED)
    merges at once with --admin, pinned to the head its checks were read at."""
    gh = _bypass_gh()
    gh.merge_status.return_value = {"mergeStateStatus": "BLOCKED", "reviewDecision": ""}
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep") as sleep:
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    gh.merge.assert_called_once_with("99", strategy="merge", match_head="h1", admin=True)
    sleep.assert_not_called()


def test_without_bypass_a_ruleset_blocked_pr_polls_and_never_uses_admin() -> None:
    """The bug: the plain merge is refused by policy and polled until timeout."""
    gh = _gh(merge_states=["CLEAN"] * 20)
    gh.merge.side_effect = [_policy_error(), None]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="merge")
    for call in gh.merge.call_args_list:
        assert "admin" not in call.kwargs
    gh.head_sha.assert_not_called()


def test_bypass_never_skips_a_failed_check() -> None:
    gh = _bypass_gh(failed=["ci / test"])
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="ci / test"):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    gh.merge.assert_not_called()


def test_bypass_rechecks_failures_at_the_pinned_head() -> None:
    """A check that fails after the wait but before the merge stops the merge."""
    gh = _bypass_gh()
    gh.failed_check_names.side_effect = [[], ["ci / lint"]]
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match="ci / lint"):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    gh.merge.assert_not_called()


def test_bypass_never_skips_an_outstanding_required_check() -> None:
    """A required check outstanding at merge time aborts; the bypass never merges past it."""
    gh = _bypass_gh(required=_EVIDENCE, outstanding=[{}, {"test / evidence": "pending"}])
    with (
        patch(_MOD + ".github", gh),
        pytest.raises(MergeAbortError, match=r"outstanding: test / evidence"),
    ):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    gh.merge.assert_not_called()


@pytest.mark.parametrize("review", ["REVIEW_REQUIRED", "CHANGES_REQUESTED"])
def test_bypass_never_skips_a_required_review(review: str) -> None:
    gh = _bypass_gh(review=review)
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbortError, match=review):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    gh.merge.assert_not_called()


def test_bypass_reads_the_head_before_the_checks() -> None:
    """The head is pinned before the checks are read, so a head that moves after
    the read is refused by GitHub rather than merged with unread checks."""
    gh = _bypass_gh()
    order: list[str] = []
    gh.head_sha.side_effect = lambda pr: order.append("head") or "h1"
    gh.failed_check_names.side_effect = lambda pr: order.append("failed") or []
    gh.merge.side_effect = lambda *a, **k: order.append("merge")
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    # post-wait gate, then the bypass's own reads: head first.
    assert order == ["failed", "head", "failed", "merge"]


def test_bypass_head_moved_is_rechecked_then_merged_at_the_new_head() -> None:
    gh = _bypass_gh()
    gh.head_sha.side_effect = ["h1", "h2"]
    gh.merge.side_effect = [GitHubAPIError(1, "gh pr merge", stderr=_HEAD_MOVED_STDERR), None]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    assert [c.kwargs["match_head"] for c in gh.merge.call_args_list] == ["h1", "h2"]
    assert gh.wait_for_checks.call_count == 2


def test_bypass_head_moved_rechecks_are_bounded_by_the_update_cap() -> None:
    gh = _gh(merge_states=["CLEAN"] * 40)
    gh.head_sha.return_value = "h1"
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr=_HEAD_MOVED_STDERR)
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbortError, match="still behind after 5"),
    ):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    assert gh.merge.call_count == 6


@pytest.mark.parametrize("stderr", [_POLICY_STDERR, _RULE_VIOLATION_STDERR])
def test_bypass_refused_names_the_ruleset_and_does_not_poll(stderr: str) -> None:
    """A caller who is not a bypass actor gets a clear stop, never a silent loop."""
    gh = _bypass_gh()
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr=stderr)
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep") as sleep,
        pytest.raises(MergeAbortError, match="'Main updates' ruleset") as excinfo,
    ):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    assert "repository admin" in str(excinfo.value)
    assert gh.merge.call_count == 1
    sleep.assert_not_called()


def test_bypass_behind_rejection_still_updates_and_retries() -> None:
    gh = _bypass_gh()
    gh.merge.side_effect = [GitHubAPIError(1, "gh pr merge", stderr=_NOT_UP_TO_DATE_STDERR), None]
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)
    gh.update_branch.assert_called_once_with("99")
    assert gh.merge.call_count == 2


def test_bypass_unrelated_merge_error_reraises() -> None:
    gh = _bypass_gh()
    gh.merge.side_effect = GitHubAPIError(1, "gh pr merge", stderr="HTTP 502: Bad Gateway")
    with patch(_MOD + ".github", gh), pytest.raises(GitHubAPIError, match="502"):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET)


def test_bypass_cannot_be_combined_with_a_batch_check() -> None:
    gh = _bypass_gh()
    with patch(_MOD + ".github", gh), pytest.raises(ValueError, match="cannot be combined"):
        wait_and_merge("99", strategy="merge", bypass_ruleset=_RULESET, batch_check=lambda: "h1")
    gh.merge.assert_not_called()
