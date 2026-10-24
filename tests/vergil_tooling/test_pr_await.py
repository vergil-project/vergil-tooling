"""Tests for vergil_tooling.lib.pr_await."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github, pr_await
from vergil_tooling.lib.pr_await import PrMergedError, PrState, settle_reason, to_output

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.pr_await"


@pytest.fixture(autouse=True)
def _no_grace_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(github.ORPHAN_GRACE_ENV, raising=False)


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


def test_state_pending_check_names() -> None:
    state = PrState("sha", _checks("pass", "pending", "pending"), [])
    assert state.pending_check_names == ["check-1", "check-2"]


def test_state_blocked() -> None:
    assert PrState("sha", [], [], merge_state_status="BLOCKED").blocked
    assert not PrState("sha", [], [], merge_state_status="CLEAN").blocked
    assert not PrState("sha", [], []).blocked


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
        patch(f"{_MOD}.github.pr_state", return_value="OPEN"),
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
    ):
        state = pr_await.gather_state("PR")
    assert state.head_sha == "abc123"
    assert state.checks == checks
    assert state.reviews == reviews
    assert state.pr_state == "OPEN"
    assert state.merge_state_status == "CLEAN"


def test_wait_for_settle_aborts_when_merged_at_start() -> None:
    merged = PrState("sha", _checks("pass"), [], pr_state="MERGED")
    with (
        patch(f"{_MOD}.gather_state", return_value=merged),
        patch(f"{_MOD}.time.sleep") as slept,
        pytest.raises(PrMergedError, match="already merged"),
    ):
        pr_await.wait_for_settle("PR", since_sha=None, since_reviews=None)
    slept.assert_not_called()


def test_wait_for_settle_aborts_when_merged_mid_watch() -> None:
    pending = PrState("sha", _checks("pending"), [])
    merged = PrState("sha", _checks("pending"), [], pr_state="MERGED")
    with (
        patch(f"{_MOD}.gather_state", side_effect=[pending, merged]),
        patch(f"{_MOD}.time.sleep") as slept,
        pytest.raises(PrMergedError),
    ):
        pr_await.wait_for_settle("PR", since_sha=None, since_reviews=None)
    slept.assert_called_once()


def test_wait_for_settle_merged_abort_wins_over_settle() -> None:
    # Even when a settle reason exists (new commit, checks terminal), a
    # merged PR aborts instead of settling.
    merged = PrState("new-sha", _checks("pass"), [], pr_state="MERGED")
    with (
        patch(f"{_MOD}.gather_state", return_value=merged),
        patch(f"{_MOD}.time.sleep"),
        pytest.raises(PrMergedError),
    ):
        pr_await.wait_for_settle("PR", since_sha="old-sha", since_reviews=0)


def test_to_output_shape() -> None:
    state = PrState("abc123", _checks("pass", "fail"), [{"id": "r1"}], merge_state_status="DIRTY")
    out = to_output(state, "checks_terminal")
    assert out["reason"] == "checks_terminal"
    assert out["head_sha"] == "abc123"
    assert out["review_count"] == 1
    assert out["failed_checks"] == ["check-1"]
    assert out["all_checks_passed"] is False
    assert out["merge_state_status"] == "DIRTY"
    # Only an orphaned_check settle populates orphaned_checks.
    assert out["orphaned_checks"] == []


def test_to_output_orphaned_checks_lists_pending_checks() -> None:
    checks = _checks("pass", "pending", "pending")
    state = PrState("abc123", checks, [], merge_state_status="BLOCKED")
    out = to_output(state, "orphaned_check")
    assert out["reason"] == "orphaned_check"
    assert out["orphaned_checks"] == ["check-1", "check-2"]
    assert out["merge_state_status"] == "BLOCKED"


def test_is_orphaned_block_true_when_blocked_and_all_pending_orphaned() -> None:
    state = PrState("sha", _checks("pass", "pending", "pending"), [], merge_state_status="BLOCKED")
    orphans = ["check-1", "check-2"]
    with patch(f"{_MOD}.github.orphaned_check_names", return_value=orphans) as detect:
        assert pr_await.is_orphaned_block(state, "PR") is True
    detect.assert_called_once_with("PR")


def test_is_orphaned_block_false_when_not_blocked() -> None:
    state = PrState("sha", _checks("pending"), [], merge_state_status="CLEAN")
    with patch(f"{_MOD}.github.orphaned_check_names") as detect:
        assert pr_await.is_orphaned_block(state, "PR") is False
    # No API call when the cheap local predicate already fails.
    detect.assert_not_called()


def test_is_orphaned_block_false_when_no_pending_checks() -> None:
    state = PrState("sha", _checks("pass"), [], merge_state_status="BLOCKED")
    with patch(f"{_MOD}.github.orphaned_check_names") as detect:
        assert pr_await.is_orphaned_block(state, "PR") is False
    detect.assert_not_called()


def test_is_orphaned_block_false_when_a_pending_check_is_not_orphaned() -> None:
    # One pending check is a genuinely-running app status (not in the orphan
    # set); the watch must keep waiting rather than declare a false orphan.
    state = PrState("sha", _checks("pending", "pending"), [], merge_state_status="BLOCKED")
    with patch(f"{_MOD}.github.orphaned_check_names", return_value=["check-0"]):
        assert pr_await.is_orphaned_block(state, "PR") is False


class _Clock:
    """A fake monotonic clock that ``time.sleep`` advances."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.now += secs


@pytest.fixture
def clock() -> Iterator[_Clock]:
    fake = _Clock()
    with (
        patch(f"{_MOD}.time.monotonic", side_effect=fake.monotonic),
        patch(f"{_MOD}.time.sleep", side_effect=fake.sleep),
    ):
        yield fake


def test_wait_for_settle_returns_orphaned_check_reason_after_grace(
    clock: _Clock, capsys: pytest.CaptureFixture[str]
) -> None:
    blocked = PrState("sha", _checks("pass", "pending"), [], merge_state_status="BLOCKED")
    with (
        patch(f"{_MOD}.gather_state", return_value=blocked),
        patch(f"{_MOD}.github.orphaned_check_names", return_value=["check-1"]),
    ):
        state, reason = pr_await.wait_for_settle(
            "PR", since_sha="sha", since_reviews=0, poll_interval=15, orphan_grace=300
        )
    assert reason == "orphaned_check"
    assert state is blocked
    assert clock.now == 300
    err = capsys.readouterr().err
    assert err.count("GitHub may be delayed") == 1  # announced once, not per poll
    assert "1 check-run(s) have a completed run but no result yet (check-1)" in err
    assert "re-checking for up to 5m00s" in err
    assert "Still no result for check-1 after 5m00s; reporting orphaned" in err


def test_wait_for_settle_late_result_within_grace_settles_normally(clock: _Clock) -> None:
    # Replay of the 2026-10-22 Actions incident: the check looked orphaned, then
    # GitHub wrote its result back a few minutes later. No orphan verdict.
    blocked = PrState("sha", _checks("pass", "pending"), [], merge_state_status="BLOCKED")
    clean = PrState("sha", _checks("pass", "pass"), [], merge_state_status="CLEAN")

    def gather(pr: str) -> PrState:
        return clean if clock.now >= 120 else blocked

    with (
        patch(f"{_MOD}.gather_state", side_effect=gather),
        patch(f"{_MOD}.github.orphaned_check_names", return_value=["check-1"]),
    ):
        state, reason = pr_await.wait_for_settle(
            "PR", since_sha="sha", since_reviews=0, poll_interval=15, orphan_grace=300
        )
    assert reason == "checks_terminal"
    assert state is clean
    assert clock.now == 120


def test_wait_for_settle_grace_restarts_when_the_orphan_set_changes(clock: _Clock) -> None:
    # A suspect cleared (not orphaned) and a different pending set both restart
    # the grace, so every check reported orphaned has waited the full period.
    one = PrState("sha", _checks("pass", "pending"), [], merge_state_status="BLOCKED")
    two = PrState("sha", _checks("pending", "pending"), [], merge_state_status="BLOCKED")
    timeline = [(0, one, ["check-1"]), (100, one, []), (150, one, ["check-1"]), (250, two, None)]

    def current() -> tuple[PrState, list[str] | None]:
        return next((s, o) for t, s, o in reversed(timeline) if clock.now >= t)

    def orphans(pr: str) -> list[str]:
        names = current()[1]
        return ["check-0", "check-1"] if names is None else names

    with (
        patch(f"{_MOD}.gather_state", side_effect=lambda pr: current()[0]),
        patch(f"{_MOD}.github.orphaned_check_names", side_effect=orphans),
    ):
        state, reason = pr_await.wait_for_settle(
            "PR", since_sha="sha", since_reviews=0, poll_interval=10, orphan_grace=200
        )
    assert reason == "orphaned_check"
    assert state is two
    assert clock.now == 450


def test_wait_for_settle_default_grace_comes_from_github(clock: _Clock) -> None:
    blocked = PrState("sha", _checks("pass", "pending"), [], merge_state_status="BLOCKED")
    with (
        patch(f"{_MOD}.gather_state", return_value=blocked),
        patch(f"{_MOD}.github.orphaned_check_names", return_value=["check-1"]),
        patch(f"{_MOD}.github.orphan_grace_secs", return_value=60) as grace,
    ):
        _, reason = pr_await.wait_for_settle("PR", since_sha="sha", since_reviews=0)
    assert reason == "orphaned_check"
    assert clock.now == 60
    grace.assert_called_once_with()


def test_wait_for_settle_keeps_waiting_when_pending_but_not_orphaned() -> None:
    # First poll: blocked with a pending non-orphan → keep waiting.
    # Second poll: checks terminal → settle normally.
    blocked = PrState("sha", _checks("pass", "pending"), [], merge_state_status="BLOCKED")
    terminal = PrState("sha", _checks("pass", "pass"), [], merge_state_status="CLEAN")
    with (
        patch(f"{_MOD}.gather_state", side_effect=[blocked, terminal]),
        patch(f"{_MOD}.github.orphaned_check_names", return_value=[]),
        patch(f"{_MOD}.time.sleep") as slept,
    ):
        state, reason = pr_await.wait_for_settle("PR", since_sha="sha", since_reviews=0)
    assert reason == "checks_terminal"
    assert state is terminal
    slept.assert_called_once()


def test_wait_for_settle_normal_reason_wins_over_orphan_check() -> None:
    # A new commit settles before the orphan detector is even consulted.
    blocked = PrState("new-sha", _checks("pending"), [], merge_state_status="BLOCKED")
    with (
        patch(f"{_MOD}.gather_state", return_value=blocked),
        patch(f"{_MOD}.github.orphaned_check_names") as detect,
        patch(f"{_MOD}.time.sleep"),
    ):
        _, reason = pr_await.wait_for_settle("PR", since_sha="old-sha", since_reviews=0)
    assert reason == "new_commit"
    detect.assert_not_called()
