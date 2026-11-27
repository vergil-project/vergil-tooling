"""The release lock's staleness rule (epic .github#1664, Task 18; spec §5.6)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release_lock import stale
from vergil_tooling.lib.release_lock.model import STALE_AFTER_SECS, LockRecord
from vergil_tooling.lib.release_lock.refs import LockState
from vergil_tooling.lib.release_lock.stale import IssueReadError, Staleness, judge

_REPO = "vergil-project/vergil-tooling"
_ISSUE = f"{_REPO}#5300"
_SHA = "a" * 40
_NOW = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
_FRESH = (_NOW - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
_OLD = (_NOW - timedelta(seconds=STALE_AFTER_SECS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
_MOD = "vergil_tooling.lib.release_lock.stale"


def _record(**changes: str) -> LockRecord:
    fields = {
        "holder_identity": "human",
        "holder_host": "mac.local",
        "version": "2.1.400",
        "worktree": "",
        "tracking_issue": "",
        "stage": "pre-check",
        "health": "running",
        "broken_stage": "",
        "started_at": "2026-11-27T10:00:00Z",
        "heartbeat_at": _FRESH,
        "token": "a1b2c3",
    }
    fields.update(changes)
    return LockRecord(**fields)  # type: ignore[arg-type]


def _held(rec: LockRecord) -> LockState:
    return LockState(sha=_SHA, record=rec, error="")


def _judge(
    state: LockState,
    *,
    issue_state: str | Exception = "OPEN",
    open_issues: list[tuple[str, int, str]] | Exception = (),  # type: ignore[assignment]
) -> tuple[Staleness, dict[str, MagicMock]]:
    """Judge *state* with the issue reads mocked: the named issue's state, the open releases."""
    view = MagicMock(
        side_effect=issue_state if isinstance(issue_state, Exception) else None,
        return_value={"state": issue_state},
    )
    listed = MagicMock(
        side_effect=open_issues if isinstance(open_issues, Exception) else None,
        return_value=list(open_issues) if not isinstance(open_issues, Exception) else None,
    )
    trusted = MagicMock(side_effect=lambda issues, _repo: list(issues))
    with (
        patch(f"{_MOD}.github.read_json", view),
        patch(f"{_MOD}._open_release_issues", listed),
        patch(f"{_MOD}._trusted_release_issues", trusted),
    ):
        result = judge(_REPO, state, now=_NOW)
    return result, {"view": view, "listed": listed, "trusted": trusted}


def test_absent_lock_not_stale() -> None:
    result, mocks = _judge(LockState(sha=None, record=None, error=""))
    assert result == Staleness(stale=False, live=False, reason="no release lock held", tracking_issue="")
    mocks["view"].assert_not_called()


def test_unreadable_lock_not_stale() -> None:
    result, mocks = _judge(LockState(sha=_SHA, record=None, error="not JSON"))
    assert (result.stale, result.live, result.reason) == (False, False, "unreadable")
    mocks["view"].assert_not_called()
    mocks["listed"].assert_not_called()


def test_running_fresh_never_stale_even_with_closed_issue() -> None:
    result, mocks = _judge(_held(_record(tracking_issue=_ISSUE)), issue_state="CLOSED")
    assert result.live
    assert not result.stale
    assert result.tracking_issue == _ISSUE
    # The one issue read is made only for a lock that is held and not live.
    mocks["view"].assert_not_called()


def test_broken_with_open_issue_not_stale() -> None:
    rec = _record(tracking_issue=_ISSUE, health="broken", broken_stage="cut", heartbeat_at=_OLD)
    result, mocks = _judge(_held(rec), issue_state="OPEN")
    assert not result.stale
    assert not result.live
    assert result.tracking_issue == _ISSUE
    assert _ISSUE in result.reason
    args = mocks["view"].call_args.args
    assert args[:3] == ("issue", "view", "5300")
    assert "--repo" in args
    assert args[args.index("--repo") + 1] == _REPO


def test_broken_fresh_heartbeat_with_closed_issue_stale() -> None:
    rec = _record(tracking_issue=_ISSUE, health="broken", broken_stage="cut")
    result, _ = _judge(_held(rec), issue_state="CLOSED")
    assert result.stale
    assert "closed" in result.reason


def test_closed_issue_stale() -> None:
    result, _ = _judge(_held(_record(tracking_issue=_ISSUE, heartbeat_at=_OLD)), issue_state="CLOSED")
    assert result.stale
    assert not result.live
    assert result.tracking_issue == _ISSUE
    assert f"{_ISSUE} is closed" in result.reason


def test_stale_heartbeat_open_issue_not_stale() -> None:
    # Review Focus 4: a laptop that sleeps mid-release. The heartbeat goes
    # stale while the tracking issue is open; no watch may reclaim it.
    result, mocks = _judge(
        _held(_record(tracking_issue=_ISSUE, heartbeat_at=_OLD)), issue_state="OPEN"
    )
    assert not result.stale
    assert not result.live
    assert result.tracking_issue == _ISSUE
    mocks["listed"].assert_not_called()


def test_no_issue_old_heartbeat_stale() -> None:
    result, mocks = _judge(_held(_record(heartbeat_at=_OLD)), open_issues=[])
    assert result.stale
    assert result.tracking_issue == ""
    assert "heartbeat" in result.reason
    mocks["view"].assert_not_called()


def test_no_issue_fresh_heartbeat_broken_not_stale() -> None:
    rec = _record(health="broken", broken_stage="preflight")
    result, _ = _judge(_held(rec), open_issues=[])
    assert not result.stale
    assert not result.live


def test_no_issue_but_open_tracking_issue_of_version_not_stale() -> None:
    open_issues = [("2.1.399", 5200, "body"), ("2.1.400", 5301, "body")]
    result, mocks = _judge(_held(_record(heartbeat_at=_OLD)), open_issues=open_issues)
    assert not result.stale
    assert result.tracking_issue == f"{_REPO}#5301"
    assert f"{_REPO}#5301" in result.reason
    # Only a trusted issue counts: the lookup goes through the trust filter.
    mocks["trusted"].assert_called_once()


def test_no_issue_open_tracking_issue_of_other_version_stale() -> None:
    result, _ = _judge(_held(_record(heartbeat_at=_OLD)), open_issues=[("2.1.399", 5200, "b")])
    assert result.stale
    assert result.tracking_issue == ""


def test_no_issue_untrusted_tracking_issue_of_version_stale() -> None:
    with (
        patch(f"{_MOD}._open_release_issues", return_value=[("2.1.400", 5301, "b")]),
        patch(f"{_MOD}._trusted_release_issues", return_value=[]),
    ):
        result = judge(_REPO, _held(_record(heartbeat_at=_OLD)), now=_NOW)
    assert result.stale


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"),
        ValueError("bad JSON"),
        OSError("gh not found"),
    ],
)
def test_issue_read_failure_raises(error: Exception) -> None:
    rec = _record(tracking_issue=_ISSUE, heartbeat_at=_OLD)
    with pytest.raises(IssueReadError, match=_ISSUE):
        _judge(_held(rec), issue_state=error)


@pytest.mark.parametrize(
    "error",
    [
        ReleaseError(phase="release-lock", command="gh issue list", message="truncated"),
        subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502"),
    ],
)
def test_open_issue_lookup_failure_raises(error: Exception) -> None:
    with pytest.raises(IssueReadError, match="2.1.400"):
        _judge(_held(_record(heartbeat_at=_OLD)), open_issues=error)


@pytest.mark.parametrize("value", [{"state": "MERGED"}, {"state": None}, [], "OPEN"])
def test_unexpected_issue_state_raises(value: Any) -> None:
    rec = _record(tracking_issue=_ISSUE, heartbeat_at=_OLD)
    with (
        patch(f"{_MOD}.github.read_json", return_value=value),
        pytest.raises(IssueReadError, match=_ISSUE),
    ):
        judge(_REPO, _held(rec), now=_NOW)


def test_issue_in_another_repo_is_read_there() -> None:
    other = "vergil-project/.github#77"
    rec = _record(tracking_issue=other, heartbeat_at=_OLD)
    result, mocks = _judge(_held(rec), issue_state="CLOSED")
    assert result.stale
    args = mocks["view"].call_args.args
    assert args[2] == "77"
    assert args[args.index("--repo") + 1] == "vergil-project/.github"


def test_heartbeat_age() -> None:
    assert stale.heartbeat_age_secs(_record(heartbeat_at=_OLD), now=_NOW) == STALE_AFTER_SECS + 1
    future = (_NOW + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert stale.heartbeat_age_secs(_record(heartbeat_at=future), now=_NOW) == 0


def test_heartbeat_exactly_at_threshold_is_not_fresh() -> None:
    at = (_NOW - timedelta(seconds=STALE_AFTER_SECS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    result, _ = _judge(_held(_record(heartbeat_at=at)), open_issues=[])
    assert not result.live
    assert result.stale


def test_naive_now_refused() -> None:
    with pytest.raises(ValueError, match="aware"):
        judge(_REPO, _held(_record()), now=datetime(2026, 11, 27, 12, 0, 0))  # noqa: DTZ001
