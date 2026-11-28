"""Tests for the release lock's guard on submit (vergil-tooling#5427, design F17).

:func:`submit_guard.clear` runs before any push or PR. A lock held by a release
makes it refuse (:class:`submit_guard.LockedByReleaseError`), or with
``wait=True`` poll until the lock clears or the deadline passes. The lock is
the :class:`~tests.vergil_tooling.watch_lock_stubs.FakeLock` fake, so the real
read, judgement and reclaim rules run on top of it; time is a fake clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.vergil_tooling.watch_lock_stubs import ISSUE, FakeLock, broken, record, stale_heartbeat
from vergil_tooling.lib import finalize_watch
from vergil_tooling.lib.release_lock import stale, submit_guard
from vergil_tooling.lib.release_lock.model import (
    BROKEN_WATCH_POLL_SECS,
    SUBMIT_WAIT_POLL_SECS,
    RefReadError,
)
from vergil_tooling.lib.release_lock.stale import IssueReadError

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO = "o/r"


@dataclass
class FakeClock:
    """Monotonic time that advances only when the guard sleeps."""

    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def monotonic(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeClock]:
    monkeypatch.delenv(finalize_watch.DEADLINE_ENV, raising=False)
    yield FakeClock()


def _clear(
    clock: FakeClock, *, wait: bool, reports: list[str] | None = None
) -> bool:
    """Run the guard on the installed fake lock, with the fake clock and a recorded report."""
    return submit_guard.clear(
        REPO,
        wait=wait,
        report=(reports if reports is not None else []).append,
        sleep=clock.sleep,
        clock=clock.monotonic,
        actor="tester",
    )


def test_free_lock_clears_without_waiting(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    lock = FakeLock(states=[None]).install(monkeypatch)
    reports: list[str] = []
    assert _clear(clock, wait=False, reports=reports) is False
    assert reports == []
    assert clock.sleeps == []
    assert lock.reads == [REPO]


def test_free_lock_with_the_flag_does_not_wait(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[None]).install(monkeypatch)
    assert _clear(clock, wait=True) is False
    assert clock.sleeps == []


def test_live_release_refuses_naming_what_the_lock_records(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[record()]).install(monkeypatch)
    with pytest.raises(submit_guard.LockedByReleaseError) as caught:
        _clear(clock, wait=False)
    text = str(caught.value)
    assert "locked by another process, most likely a release" in text
    assert "may stay locked for a long time" in text
    assert "holder: human on mac.local" in text
    assert "version: 2.1.400" in text
    assert f"tracking issue: {ISSUE}" in text
    assert "stage: cut" in text
    assert "health: running" in text
    assert "started: 2026-11-27T10:00:00Z" in text
    assert "Nothing was pushed and no PR was opened" in text
    assert "--wait-for-release" in text
    assert clock.sleeps == []


def test_refusal_reads_the_lock_once(monkeypatch: pytest.MonkeyPatch, clock: FakeClock) -> None:
    lock = FakeLock(states=[record()]).install(monkeypatch)
    with pytest.raises(submit_guard.LockedByReleaseError):
        _clear(clock, wait=False)
    assert len(lock.reads) == 1


def test_broken_release_whose_issue_is_open_refuses_saying_why_not_reclaimed(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[broken()], issue_state="OPEN").install(monkeypatch)
    with pytest.raises(submit_guard.LockedByReleaseError) as caught:
        _clear(clock, wait=False)
    text = str(caught.value)
    assert "health: broken (at cut)" in text
    assert f"stale: no (its tracking issue {ISSUE} is open)" in text


def test_unreadable_lock_is_held_and_the_error_is_named(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    error = "refs/vergil/release-lock is unreadable: the lock is not valid JSON"
    FakeLock(states=[error]).install(monkeypatch)
    with pytest.raises(submit_guard.LockedByReleaseError) as caught:
        _clear(clock, wait=False)
    text = str(caught.value)
    assert f"release lock: held, unreadable: {error}" in text
    assert "Nothing was pushed" in text


def test_unreadable_lock_with_the_flag_waits_on_the_slow_pace(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=["bad json", None]).install(monkeypatch)
    assert _clear(clock, wait=True) is True
    assert clock.sleeps == [BROKEN_WATCH_POLL_SECS]


def test_failed_lock_read_refuses_and_is_never_read_as_free(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[RefReadError("reading refs/vergil/release-lock from origin failed")]).install(
        monkeypatch
    )
    with pytest.raises(submit_guard.LockGuardError) as caught:
        _clear(clock, wait=False)
    assert "reading refs/vergil/release-lock from origin failed" in str(caught.value)
    assert "nothing was pushed" in str(caught.value)


def test_failed_read_while_waiting_is_an_error_not_a_free_lock(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[record(), RefReadError("origin unreachable")]).install(monkeypatch)
    with pytest.raises(submit_guard.LockGuardError, match="origin unreachable"):
        _clear(clock, wait=True)
    assert clock.sleeps == [SUBMIT_WAIT_POLL_SECS]


def test_stale_lock_is_reclaimed_and_the_push_goes_on(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    lock = FakeLock(states=[broken()], issue_state="CLOSED").install(monkeypatch)
    assert _clear(clock, wait=False) is False
    assert lock.reclaims == [(REPO, "tester")]


def test_stale_lock_with_the_flag_is_reclaimed_not_waited_on(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    lock = FakeLock(states=[record(heartbeat_at=stale_heartbeat(), tracking_issue="")]).install(
        monkeypatch
    )
    monkeypatch.setattr(stale, "_open_issue_of_version", lambda _repo, _version: "")
    assert _clear(clock, wait=True) is False
    assert len(lock.reclaims) == 1
    assert clock.sleeps == []


def test_judging_a_lock_whose_issue_cannot_be_read_is_an_error(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[broken()]).install(monkeypatch)

    def unreadable(_ref: str) -> str:
        msg = "could not read the state of the lock's tracking issue"
        raise IssueReadError(msg)

    monkeypatch.setattr(stale, "_issue_state", unreadable)
    with pytest.raises(submit_guard.LockGuardError, match="could not read the state"):
        _clear(clock, wait=False)


def test_wait_polls_until_the_lock_clears_and_reports_each_change(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[record(), record(), record(stage="publish"), None]).install(monkeypatch)
    reports: list[str] = []
    assert _clear(clock, wait=True, reports=reports) is True
    assert clock.sleeps == [SUBMIT_WAIT_POLL_SECS] * 3
    waiting = [line for line in reports if line.startswith("waiting for the release lock")]
    assert waiting == [
        f"waiting for the release lock on {REPO}: held by release 2.1.400 (stage cut, running)",
        f"waiting for the release lock on {REPO}: held by release 2.1.400 (stage publish, running)",
    ]
    assert reports[-1] == f"the release lock on {REPO} has cleared"


def test_wait_does_not_repeat_an_unchanged_progress_line(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[record(), record(), record(), None]).install(monkeypatch)
    reports: list[str] = []
    assert _clear(clock, wait=True, reports=reports) is True
    assert len([line for line in reports if line.startswith("waiting")]) == 1


def test_wait_ends_when_a_stale_lock_is_reclaimed_during_it(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    lock = FakeLock(states=[record(), broken()], issue_state="CLOSED").install(monkeypatch)
    assert _clear(clock, wait=True) is True
    assert len(lock.reclaims) == 1
    assert len(clock.sleeps) == 1


def test_wait_reaching_the_deadline_times_out_naming_the_release(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    monkeypatch.setenv(finalize_watch.DEADLINE_ENV, "40")
    FakeLock(states=[record()]).install(monkeypatch)
    with pytest.raises(submit_guard.LockWaitTimedOutError) as caught:
        _clear(clock, wait=True)
    text = str(caught.value)
    assert "40s" in text
    assert "held by release 2.1.400 (stage cut, running)" in text
    assert "Nothing was pushed and no PR was opened" in text
    assert "--wait-for-release" in text
    # The last sleep is cut to the time left, so the deadline is not overshot.
    assert clock.sleeps == [SUBMIT_WAIT_POLL_SECS, SUBMIT_WAIT_POLL_SECS, 10]
    assert clock.now == 40


def test_wait_deadline_is_the_finalize_deadline(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[record()]).install(monkeypatch)
    with patch.object(finalize_watch, "deadline_secs", return_value=20) as deadline:
        with pytest.raises(submit_guard.LockWaitTimedOutError):
            _clear(clock, wait=True)
    deadline.assert_called_once_with()
    assert clock.now == 20


def test_malformed_deadline_is_an_error_before_waiting(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    monkeypatch.setenv(finalize_watch.DEADLINE_ENV, "soon")
    FakeLock(states=[record()]).install(monkeypatch)
    with pytest.raises(submit_guard.LockGuardError, match="VRG_FINALIZE_DEADLINE"):
        _clear(clock, wait=True)
    assert clock.sleeps == []


def test_broken_lock_waited_on_polls_at_the_slow_pace(
    monkeypatch: pytest.MonkeyPatch, clock: FakeClock
) -> None:
    FakeLock(states=[broken(), None], issue_state="OPEN").install(monkeypatch)
    assert _clear(clock, wait=True) is True
    assert clock.sleeps == [BROKEN_WATCH_POLL_SECS]
