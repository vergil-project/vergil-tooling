"""A fake release lock for the merge watch's tests (epic vergil-project/.github#1664, Task 26).

:class:`FakeLock` stands in for everything :class:`~vergil_tooling.lib.release_lock.watch.LockWatch`
reaches: the lock read (``refs.read_lock``), the tracking issue's state that
the staleness rule reads (``stale._issue_state``), the reclaim
(``reclaim.reclaim_if_stale``) and the one PR path's arm (``pr_path.arm``).
The real :class:`LockWatch` and the real staleness rule run on top of it, and
every call is recorded.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from vergil_tooling.lib import pr_path
from vergil_tooling.lib.release_lock import reclaim, refs, stale
from vergil_tooling.lib.release_lock.model import STALE_AFTER_SECS, LockRecord, RefReadError
from vergil_tooling.lib.release_lock.refs import LockState
from vergil_tooling.lib.release_lock.sweeps import RearmResult

if TYPE_CHECKING:
    import pytest

    from vergil_tooling.lib.pr_path import ArmOutcome

LOCK_SHA = "c" * 40
ISSUE = "o/r#5300"


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(**fields: str) -> LockRecord:
    """A live lock record for release 2.1.400 at stage ``cut``; *fields* replace its own."""
    base = LockRecord(
        holder_identity="human",
        holder_host="mac.local",
        version="2.1.400",
        worktree="/w/.worktrees/release-2.1.400",
        tracking_issue=ISSUE,
        stage="cut",
        health="running",
        broken_stage="",
        started_at="2026-11-27T10:00:00Z",
        heartbeat_at=stamp(datetime.now(UTC)),
        token="holder-token",
    )
    return dataclasses.replace(base, **cast("dict[str, Any]", fields))


def broken(**fields: str) -> LockRecord:
    """A lock record of a release that recorded itself broken at stage ``cut``."""
    return record(health="broken", broken_stage="cut", **fields)


def stale_heartbeat() -> str:
    return stamp(datetime.now(UTC) - timedelta(seconds=STALE_AFTER_SECS + 60))


@dataclass
class FakeLock:
    """The release lock as a sequence of reads, the issue state, and the arm and reclaim calls.

    Each read takes the next of *states* and the last one repeats; a state is
    a :class:`LockRecord` (held), ``None`` (free), a string (held but
    unreadable, the string its error) or an exception (the read fails).
    *arm_outcome* is what ``pr_path.arm`` returns, or an exception it raises.
    *reclaimed* is whether a reclaim wins; a won reclaim frees the lock for
    every later read.
    """

    states: list[object] = field(default_factory=lambda: [None])
    issue_state: str = "OPEN"
    arm_outcome: ArmOutcome | Exception = "armed"
    arm_detail: str = ""
    reclaimed: bool = True
    reclaim_error: Exception | None = None
    reads: list[str] = field(default_factory=list)
    arms: list[str] = field(default_factory=list)
    reclaims: list[tuple[str, str]] = field(default_factory=list)
    issue_reads: list[str] = field(default_factory=list)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeLock:
        monkeypatch.setattr(refs, "read_lock", self.read_lock)
        monkeypatch.setattr(stale, "_issue_state", self.read_issue)
        monkeypatch.setattr(reclaim, "reclaim_if_stale", self.reclaim_if_stale)
        monkeypatch.setattr(pr_path, "arm", self.arm)
        return self

    def read_lock(self, repo: str) -> LockState:
        self.reads.append(repo)
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if isinstance(state, Exception):
            raise state
        if state is None:
            return LockState(sha=None, record=None, error="")
        if isinstance(state, str):
            return LockState(sha=LOCK_SHA, record=None, error=state)
        assert isinstance(state, LockRecord)
        return LockState(sha=LOCK_SHA, record=state, error="")

    def read_issue(self, ref: str) -> str:
        self.issue_reads.append(ref)
        return self.issue_state

    def reclaim_if_stale(self, repo: str, *, actor: str) -> reclaim.ReclaimResult | None:
        self.reclaims.append((repo, actor))
        if self.reclaim_error is not None:
            raise self.reclaim_error
        if not self.reclaimed:
            return None
        self.states = [None]
        return reclaim.ReclaimResult(
            repo=repo,
            action="reclaim",
            old_sha=LOCK_SHA,
            tracking_issue=ISSUE,
            reason=f"its tracking issue {ISSUE} is closed",
            rearm=RearmResult(
                armed=["https://github.com/o/r/pull/1"], merged=[], held=[], refused=[]
            ),
        )

    def arm(self, pr_url: str, **_kw: object) -> pr_path.ArmResult:
        self.arms.append(pr_url)
        if isinstance(self.arm_outcome, Exception):
            raise self.arm_outcome
        return pr_path.ArmResult(
            outcome=self.arm_outcome,
            pr_url=pr_url,
            head="a" * 40,
            method="SQUASH",
            detail=self.arm_detail,
        )


__all__ = ["ISSUE", "FakeLock", "RefReadError", "broken", "record", "stale_heartbeat"]
