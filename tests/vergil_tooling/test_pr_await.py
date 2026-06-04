"""Tests for vergil_tooling.lib.pr_await."""

from __future__ import annotations

from unittest.mock import patch

from vergil_tooling.lib import pr_await
from vergil_tooling.lib.pr_await import PrState, settle_reason, to_output

_MOD = "vergil_tooling.lib.pr_await"


def _checks(*buckets: str) -> list[dict[str, str]]:
    return [{"name": f"check-{i}", "bucket": b, "state": b.upper()} for i, b in enumerate(buckets)]


def test_state_has_checks() -> None:
    assert PrState("sha", _checks("pass"), []).has_checks
    assert not PrState("sha", [], []).has_checks


def test_state_checks_pending() -> None:
    assert PrState("sha", _checks("pass", "pending"), []).checks_pending
    assert not PrState("sha", _checks("pass", "skipping"), []).checks_pending


def test_state_failed_checks() -> None:
    state = PrState("sha", _checks("pass", "fail", "cancel"), [])
    assert state.failed_checks == ["check-1", "check-2"]


def test_state_all_checks_passed() -> None:
    assert PrState("sha", _checks("pass", "skipping"), []).all_checks_passed
    assert not PrState("sha", _checks("pass", "fail"), []).all_checks_passed
    assert not PrState("sha", _checks("pass", "pending"), []).all_checks_passed
    # No checks at all is not "passed".
    assert not PrState("sha", [], []).all_checks_passed


def test_settle_reason_new_commit() -> None:
    state = PrState("new-sha", _checks("pending"), [])
    assert settle_reason(state, since_sha="old-sha", since_reviews=None) == "new_commit"


def test_settle_reason_no_commit_change_without_baseline() -> None:
    state = PrState("sha", _checks("pending"), [])
    assert settle_reason(state, since_sha=None, since_reviews=None) is None


def test_settle_reason_new_review() -> None:
    state = PrState("sha", _checks("pending"), [{"id": "r1"}, {"id": "r2"}])
    assert settle_reason(state, since_sha=None, since_reviews=1) == "new_review"


def test_settle_reason_no_new_review_when_count_unchanged() -> None:
    state = PrState("sha", _checks("pending"), [{"id": "r1"}])
    assert settle_reason(state, since_sha=None, since_reviews=1) is None


def test_settle_reason_checks_terminal() -> None:
    state = PrState("sha", _checks("pass", "fail"), [])
    assert settle_reason(state, since_sha="sha", since_reviews=0) == "checks_terminal"


def test_settle_reason_none_while_pending() -> None:
    state = PrState("sha", _checks("pass", "pending"), [])
    assert settle_reason(state, since_sha="sha", since_reviews=0) is None


def test_settle_reason_none_when_no_checks_registered_yet() -> None:
    state = PrState("sha", [], [])
    assert settle_reason(state, since_sha="sha", since_reviews=0) is None


def test_settle_reason_new_commit_takes_priority_over_terminal() -> None:
    state = PrState("new-sha", _checks("pass"), [])
    assert settle_reason(state, since_sha="old-sha", since_reviews=0) == "new_commit"


def test_wait_for_settle_blocks_until_settled() -> None:
    pending = PrState("sha", _checks("pending"), [])
    terminal = PrState("sha", _checks("pass"), [])
    with (
        patch(f"{_MOD}.gather_state", side_effect=[pending, terminal]),
        patch(f"{_MOD}.time.sleep") as slept,
    ):
        state, reason = pr_await.wait_for_settle("PR", since_sha=None, since_reviews=None)
    assert reason == "checks_terminal"
    assert state is terminal
    slept.assert_called_once()


def test_wait_for_settle_returns_immediately_when_already_settled() -> None:
    terminal = PrState("sha", _checks("pass"), [])
    with (
        patch(f"{_MOD}.gather_state", return_value=terminal),
        patch(f"{_MOD}.time.sleep") as slept,
    ):
        _, reason = pr_await.wait_for_settle("PR", since_sha=None, since_reviews=None)
    assert reason == "checks_terminal"
    slept.assert_not_called()


def test_gather_state_reads_github() -> None:
    checks = [{"name": "build", "bucket": "pass", "state": "SUCCESS"}]
    reviews = [{"id": "r1", "state": "APPROVED"}]
    with (
        patch(f"{_MOD}.github.head_sha", return_value="abc123"),
        patch(f"{_MOD}.github.pr_checks", return_value=checks),
        patch(f"{_MOD}.github.pr_reviews", return_value=reviews),
    ):
        state = pr_await.gather_state("PR")
    assert state.head_sha == "abc123"
    assert state.checks == checks
    assert state.reviews == reviews


def test_to_output_shape() -> None:
    state = PrState("abc123", _checks("pass", "fail"), [{"id": "r1"}])
    out = to_output(state, "checks_terminal")
    assert out["reason"] == "checks_terminal"
    assert out["head_sha"] == "abc123"
    assert out["review_count"] == 1
    assert out["failed_checks"] == ["check-1"]
    assert out["all_checks_passed"] is False
