"""Reclaiming a stale release lock and breaking one (epic vergil-project/.github#1664, Task 22).

The lock and its history run against a local bare repository served as
``origin`` (see :mod:`tests.vergil_tooling.release_lock_stubs`). The
tracking-issue comment is ``gh issue comment``, answered by a fake
``github.run``; the re-arm sweep is replaced by a fake that records what the
history and the comments held when it ran.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.vergil_tooling.release_lock_stubs import REPO, Origin, make_origin
from vergil_tooling.lib import github
from vergil_tooling.lib.release_lock import history, reclaim, refs, stale, sweeps
from vergil_tooling.lib.release_lock.model import (
    STALE_AFTER_SECS,
    LockRecord,
    RefWriteError,
    from_json,
    to_json,
)
from vergil_tooling.lib.release_lock.reclaim import ReclaimError
from vergil_tooling.lib.release_lock.sweeps import RearmResult, RearmSweepError, SweepError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_ISSUE = "org/repo#5300"
_PR1 = "https://github.com/org/repo/pull/1"
_PR2 = "https://github.com/org/repo/pull/2"


def _stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(**fields: str) -> LockRecord:
    base = LockRecord(
        holder_identity="human",
        holder_host="mac.local",
        version="2.1.400",
        worktree="/w/.worktrees/release-2.1.400",
        tracking_issue=_ISSUE,
        stage="cut",
        health="running",
        broken_stage="",
        started_at="2026-11-27T10:00:00Z",
        heartbeat_at=_stamp(datetime.now(UTC)),
        token="old-holder-token",
    )
    return dataclasses.replace(base, **cast("dict[str, Any]", fields))


def _stale_age() -> str:
    return _stamp(datetime.now(UTC) - timedelta(seconds=STALE_AFTER_SECS + 60))


class Harness:
    """The origin, the comments posted, the issue states read and the re-arm sweep's runs."""

    def __init__(self, origin: Origin, monkeypatch: pytest.MonkeyPatch) -> None:
        self.origin = origin
        self.comments: list[tuple[str, str, str]] = []  # (repo, number, body)
        self.issue_state = "CLOSED"
        self.comment_error: Exception | None = None
        self.rearm_runs: list[dict[str, object]] = []
        self.rearm_outcome: RearmResult | Exception = RearmResult(
            armed=[_PR1], merged=[], held=[], refused=[]
        )
        monkeypatch.setattr(github, "run", self._gh_run)
        monkeypatch.setattr(github, "read_json", self._gh_read_json)
        monkeypatch.setattr(sweeps, "rearm_all", self._rearm_all)

    def _gh_run(self, *args: str, echo: bool = True) -> None:
        assert args[:2] == ("issue", "comment"), args
        assert not echo, "a comment's URL must not reach stdout"
        number = args[2]
        repo = args[args.index("--repo") + 1]
        body = args[args.index("--body") + 1]
        if self.comment_error is not None:
            raise self.comment_error
        self.comments.append((repo, number, body))

    def _gh_read_json(self, *args: str) -> object:
        assert args[:2] == ("issue", "view"), args
        return {"state": self.issue_state}

    def _rearm_all(self, repo: str, *, holder_pass: str | None = None) -> RearmResult:
        assert repo == REPO
        assert holder_pass is None
        self.rearm_runs.append(
            {
                "lock": self.origin.sha(refs.LOCK_REF),
                "history": [e.action for e in history.read_history(REPO)],
                "comments": len(self.comments),
            }
        )
        if isinstance(self.rearm_outcome, Exception):
            raise self.rearm_outcome
        return self.rearm_outcome

    def put_lock(self, rec: LockRecord | str) -> str:
        text = rec if isinstance(rec, str) else to_json(rec)
        sha = self.origin.commit(refs.LOCK_FILE, text)
        self.origin.set_ref(refs.LOCK_REF, sha)
        return sha

    def entries(self) -> list[history.HistoryEntry]:
        return history.read_history(REPO)


@pytest.fixture
def h(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Harness:
    return Harness(make_origin(tmp_path, monkeypatch), monkeypatch)


# --- reclaim ------------------------------------------------------------------


def test_reclaim_not_stale_does_nothing(h: Harness) -> None:
    sha = h.put_lock(_record())  # running, fresh heartbeat: live
    assert reclaim.reclaim_if_stale(REPO, actor="user-agent") is None
    assert h.origin.sha(refs.LOCK_REF) == sha
    assert h.entries() == []
    assert h.comments == []
    assert h.rearm_runs == []


def test_reclaim_absent_lock_does_nothing(h: Harness) -> None:
    assert reclaim.reclaim_if_stale(REPO, actor="user-agent") is None
    assert h.entries() == []
    assert h.rearm_runs == []


def test_reclaim_stale_deletes_rearms_records(h: Harness) -> None:
    rec = _record(health="broken", broken_stage="cut", heartbeat_at=_stale_age())
    sha = h.put_lock(rec)
    result = reclaim.reclaim_if_stale(REPO, actor="user-agent")
    assert result is not None
    assert (result.action, result.old_sha, result.tracking_issue) == ("reclaim", sha, _ISSUE)
    assert result.rearm.armed == [_PR1]
    assert h.origin.sha(refs.LOCK_REF) is None
    entries = h.entries()
    assert [e.action for e in entries] == ["reclaim", "reclaim-rearm"]
    first, second = entries
    assert from_json(first.old) == rec
    assert first.actor == "user-agent"
    assert "closed" in first.reason
    assert first.rearmed == ()
    assert from_json(second.old) == rec
    assert second.rearmed == (_PR1,)
    assert second.reason == ""
    assert len(h.comments) == 1
    repo, number, body = h.comments[0]
    assert (repo, number) == ("org/repo", "5300")
    assert "reclaimed" in body
    assert "user-agent" in body
    assert refs.HISTORY_REF in body


def test_reclaim_stale_without_issue_posts_no_comment(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stale, "_open_issue_of_version", lambda repo, version: "")
    h.put_lock(_record(tracking_issue="", heartbeat_at=_stale_age()))
    result = reclaim.reclaim_if_stale(REPO, actor="human on mac.local")
    assert result is not None
    assert result.tracking_issue == ""
    assert h.comments == []
    assert [e.action for e in h.entries()] == ["reclaim", "reclaim-rearm"]


def test_reclaim_lost_race_returns_none(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    h.put_lock(_record(heartbeat_at=_stale_age()))
    revived = _record(heartbeat_at=_stamp(datetime.now(UTC)))
    real_delete = refs.delete_lock

    def delete_after_heartbeat(repo: str, *, expected: str) -> bool:
        h.put_lock(revived)  # the holder's heartbeat lands between the read and the delete
        return real_delete(repo, expected=expected)

    monkeypatch.setattr(refs, "delete_lock", delete_after_heartbeat)
    assert reclaim.reclaim_if_stale(REPO, actor="user-agent") is None
    assert from_json(h.origin.file(refs.LOCK_REF, refs.LOCK_FILE)) == revived
    assert h.entries() == []
    assert h.comments == []
    assert h.rearm_runs == []


def test_reclaim_refuses_unparseable_lock(h: Harness) -> None:
    sha = h.put_lock("{not json")
    assert reclaim.reclaim_if_stale(REPO, actor="user-agent") is None
    assert h.origin.sha(refs.LOCK_REF) == sha
    assert h.entries() == []
    assert h.rearm_runs == []


def test_reclaim_broken_open_issue_refused(h: Harness) -> None:
    h.issue_state = "OPEN"
    sha = h.put_lock(_record(health="broken", broken_stage="cut", heartbeat_at=_stale_age()))
    assert reclaim.reclaim_if_stale(REPO, actor="user-agent") is None
    assert h.origin.sha(refs.LOCK_REF) == sha
    assert h.entries() == []
    assert h.comments == []
    assert h.rearm_runs == []


def test_reclaim_issue_read_failure_raises(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: str) -> object:
        raise RuntimeError("gh is down")

    monkeypatch.setattr(github, "read_json", fail)
    sha = h.put_lock(_record(heartbeat_at=_stale_age()))
    with pytest.raises(stale.IssueReadError):
        reclaim.reclaim_if_stale(REPO, actor="user-agent")
    assert h.origin.sha(refs.LOCK_REF) == sha


def test_reclaim_records_before_rearm(h: Harness) -> None:
    h.put_lock(_record(heartbeat_at=_stale_age()))
    reclaim.reclaim_if_stale(REPO, actor="user-agent")
    assert h.rearm_runs == [{"lock": None, "history": ["reclaim"], "comments": 1}]


def test_reclaim_sweep_failure_still_recorded(h: Harness) -> None:
    partial = RearmResult(armed=[_PR1], merged=[], held=[], refused=[])
    failure = RearmSweepError(partial, [(_PR2, "the arm call failed")])
    h.rearm_outcome = failure
    h.put_lock(_record(heartbeat_at=_stale_age()))
    with pytest.raises(SweepError) as caught:
        reclaim.reclaim_if_stale(REPO, actor="user-agent")
    assert caught.value is failure
    entries = h.entries()
    assert [e.action for e in entries] == ["reclaim", "reclaim-rearm"]
    assert entries[1].rearmed == (_PR1,)
    assert "the arm call failed" in entries[1].reason
    assert len(h.comments) == 1


def test_reclaim_listing_failure_still_recorded(h: Harness) -> None:
    h.rearm_outcome = SweepError("listing the open PRs failed")
    h.put_lock(_record(heartbeat_at=_stale_age()))
    with pytest.raises(SweepError, match="listing the open PRs failed"):
        reclaim.reclaim_if_stale(REPO, actor="user-agent")
    entries = h.entries()
    assert [e.action for e in entries] == ["reclaim", "reclaim-rearm"]
    assert entries[1].rearmed == ()
    assert "listing the open PRs failed" in entries[1].reason


def test_reclaim_record_failure_still_rearms_then_raises(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_append(repo: str, entry: history.HistoryEntry) -> None:
        raise RefWriteError("the history push was refused")

    monkeypatch.setattr(history, "append", no_append)
    h.comment_error = RuntimeError("no token")
    h.put_lock(_record(heartbeat_at=_stale_age()))
    with pytest.raises(ReclaimError) as caught:
        reclaim.reclaim_if_stale(REPO, actor="user-agent")
    message = str(caught.value)
    # The lock is gone, so the PRs it held are re-armed whatever else failed.
    assert len(h.rearm_runs) == 1
    assert h.origin.sha(refs.LOCK_REF) is None
    assert "reclaimed" in message
    assert "the history push was refused" in message
    assert "no token" in message


# --- break --------------------------------------------------------------------


def test_break_requires_reason(h: Harness) -> None:
    sha = h.put_lock(_record())
    for reason in ("", "   "):
        with pytest.raises(ValueError, match="reason"):
            reclaim.break_lock(REPO, actor="human on mac.local", reason=reason)
    assert h.origin.sha(refs.LOCK_REF) == sha
    assert h.entries() == []


def test_break_no_lock_raises(h: Harness) -> None:
    with pytest.raises(ReclaimError, match="no release lock"):
        reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert h.entries() == []
    assert h.rearm_runs == []


def test_break_records_before_delete_then_rearms(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = _record()  # a live lock: a break needs no staleness
    sha = h.put_lock(rec)
    seen_at_delete: list[tuple[list[str], int]] = []
    real_delete = refs.delete_lock

    def delete(repo: str, *, expected: str) -> bool:
        seen_at_delete.append(([e.action for e in h.entries()], len(h.comments)))
        return real_delete(repo, expected=expected)

    monkeypatch.setattr(refs, "delete_lock", delete)
    result = reclaim.break_lock(REPO, actor="human on mac.local", reason="the laptop died")
    assert seen_at_delete == [(["break"], 1)]
    assert h.rearm_runs == [{"lock": None, "history": ["break"], "comments": 1}]
    assert (result.action, result.old_sha, result.tracking_issue) == ("break", sha, _ISSUE)
    assert result.reason == "the laptop died"
    assert result.rearm.armed == [_PR1]
    (entry,) = h.entries()
    assert from_json(entry.old) == rec
    assert (entry.actor, entry.reason) == ("human on mac.local", "the laptop died")
    _repo, number, body = h.comments[0]
    assert number == "5300"
    assert "broken" in body
    assert "the laptop died" in body


def test_break_record_failure_deletes_nothing(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = h.put_lock(_record())
    h.comment_error = RuntimeError("no token")
    with pytest.raises(ReclaimError, match="no token"):
        reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert h.origin.sha(refs.LOCK_REF) == sha
    assert h.rearm_runs == []


def _rewrite_before_delete(
    h: Harness, monkeypatch: pytest.MonkeyPatch, rewrites: list[Callable[[], object]]
) -> list[str]:
    """Run each of *rewrites* just before its delete; returns the leases deleted against."""
    leases: list[str] = []
    real_delete = refs.delete_lock

    def delete(repo: str, *, expected: str) -> bool:
        leases.append(expected)
        if len(leases) <= len(rewrites):
            rewrites[len(leases) - 1]()
        return real_delete(repo, expected=expected)

    monkeypatch.setattr(refs, "delete_lock", delete)
    return leases


def _beat(h: Harness, n: int) -> Callable[[], object]:
    return lambda: h.put_lock(_record(heartbeat_at=f"2026-11-27T11:00:{n:02d}Z"))


def test_break_retries_cas_after_heartbeat_write(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = h.put_lock(_record())
    leases = _rewrite_before_delete(h, monkeypatch, [_beat(h, 1)])
    result = reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert h.origin.sha(refs.LOCK_REF) is None
    assert len(leases) == 2
    assert leases[0] == first
    assert leases[1] != first
    assert result.old_sha == leases[1]
    # One break entry: the heartbeat kept the holder's token, so it is the same lock.
    assert [e.action for e in h.entries()] == ["break"]
    assert len(h.comments) == 1
    assert len(h.rearm_runs) == 1


def test_break_still_losing_records_failure_and_raises(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.put_lock(_record())
    leases = _rewrite_before_delete(h, monkeypatch, [_beat(h, 1), _beat(h, 2), _beat(h, 3)])
    with pytest.raises(ReclaimError, match="did not land") as caught:
        reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert len(leases) == 3
    assert "3" in str(caught.value)
    assert h.origin.sha(refs.LOCK_REF) is not None
    entries = h.entries()
    assert [e.action for e in entries] == ["break", "break-failed"]
    assert from_json(entries[1].old).token == from_json(entries[0].old).token
    assert "did not land" in entries[1].reason
    assert len(h.comments) == 2
    assert "did not land" in h.comments[1][2]
    assert h.rearm_runs == []


def test_break_stops_when_another_holder_took_the_lock(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.put_lock(_record())
    other = _record(token="a-new-holder", version="2.1.401")
    _rewrite_before_delete(h, monkeypatch, [lambda: h.put_lock(other)])
    with pytest.raises(ReclaimError, match="did not land"):
        reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    # The new holder's lock is never deleted on the strength of a break of another.
    assert from_json(h.origin.file(refs.LOCK_REF, refs.LOCK_FILE)) == other
    assert [e.action for e in h.entries()] == ["break", "break-failed"]
    assert h.rearm_runs == []


def test_break_lock_gone_at_the_delete_counts_as_landed(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed delete that finds the ref absent cannot tell its own push from
    # another writer's (refs.write_ref), and the lock is gone either way.
    h.put_lock(_record())
    _rewrite_before_delete(
        h, monkeypatch, [lambda: h.origin.git("update-ref", "-d", refs.LOCK_REF)]
    )
    reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert [e.action for e in h.entries()] == ["break"]
    assert len(h.rearm_runs) == 1


def test_break_stops_when_the_lock_went_after_a_lost_delete(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.put_lock(_record())
    real_delete = refs.delete_lock

    def lose_then_vanish(repo: str, *, expected: str) -> bool:
        h.put_lock(_record(heartbeat_at="2026-11-27T11:00:01Z"))
        landed = real_delete(repo, expected=expected)
        h.origin.git("update-ref", "-d", refs.LOCK_REF)  # gone before the break re-reads it
        return landed

    monkeypatch.setattr(refs, "delete_lock", lose_then_vanish)
    with pytest.raises(ReclaimError, match="no longer held"):
        reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert [e.action for e in h.entries()] == ["break", "break-failed"]
    assert h.rearm_runs == []


def test_break_clears_unparseable_lock(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    sha = h.put_lock("{not json")
    leases = _rewrite_before_delete(h, monkeypatch, [])
    result = reclaim.break_lock(REPO, actor="human on mac.local", reason="corrupt lock")
    assert leases == [sha]
    assert h.origin.sha(refs.LOCK_REF) is None
    (entry,) = h.entries()
    assert entry.old == "{not json"
    assert entry.reason.startswith("corrupt lock")
    assert "unreadable" in entry.reason
    assert h.comments == []
    assert result.tracking_issue == ""
    assert h.rearm_runs == [{"lock": None, "history": ["break"], "comments": 0}]


def test_break_sweep_failure_names_the_landed_break(h: Harness) -> None:
    h.rearm_outcome = SweepError("listing the open PRs failed")
    h.put_lock(_record())
    with pytest.raises(ReclaimError) as caught:
        reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert "was broken" in str(caught.value)
    assert "listing the open PRs failed" in str(caught.value)
    assert isinstance(caught.value.__cause__, SweepError)
    assert h.origin.sha(refs.LOCK_REF) is None


def test_result_as_json(h: Harness) -> None:
    h.rearm_outcome = RearmResult(
        armed=[_PR1], merged=[_PR2], held=[], refused=[("https://x/3", "a draft")]
    )
    sha = h.put_lock(_record())
    result = reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert json.loads(json.dumps(result.as_json())) == {
        "repo": REPO,
        "action": "break",
        "old_sha": sha,
        "tracking_issue": _ISSUE,
        "reason": "stuck",
        "rearm": {
            "armed": [_PR1],
            "merged": [_PR2],
            "held": [],
            "refused": [{"url": "https://x/3", "reason": "a draft"}],
        },
    }


def _recommit(h: Harness, text: str) -> str:
    """Point the lock at a new commit holding *text*: a different sha for the same text."""
    sha = h.origin.commit(refs.LOCK_FILE, text, parent=h.origin.sha(refs.LOCK_REF))
    h.origin.set_ref(refs.LOCK_REF, sha)
    return sha


def test_break_unparseable_lock_rewritten_with_same_text_is_retried(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = h.put_lock("{not json")
    leases = _rewrite_before_delete(h, monkeypatch, [lambda: _recommit(h, "{not json")])
    reclaim.break_lock(REPO, actor="human on mac.local", reason="corrupt lock")
    assert len(leases) == 2
    assert leases[0] == first
    assert h.origin.sha(refs.LOCK_REF) is None
    assert [e.action for e in h.entries()] == ["break"]
    assert len(h.rearm_runs) == 1


def test_break_unparseable_lock_rewritten_with_other_text_stops(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.put_lock("{not json")
    _rewrite_before_delete(h, monkeypatch, [lambda: _recommit(h, "{other")])
    with pytest.raises(ReclaimError, match="another holder"):
        reclaim.break_lock(REPO, actor="human on mac.local", reason="corrupt lock")
    entries = h.entries()
    assert [e.action for e in entries] == ["break", "break-failed"]
    assert entries[1].old == "{not json"
    assert h.comments == []  # an unparseable lock names no tracking issue
    assert h.rearm_runs == []


def test_break_lock_replaced_by_unreadable_one_stops(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.put_lock(_record())
    _rewrite_before_delete(h, monkeypatch, [lambda: h.put_lock("{not json")])
    with pytest.raises(ReclaimError, match="an unreadable lock at"):
        reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    assert h.origin.file(refs.LOCK_REF, refs.LOCK_FILE) == "{not json"
    assert h.rearm_runs == []


def test_break_not_landed_record_failures_are_named(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h.put_lock(_record())
    other = _record(token="a-new-holder")
    real_append = history.append

    def append_break_only(repo: str, entry: history.HistoryEntry) -> None:
        if entry.action == "break-failed":
            raise RefWriteError("the history push was refused")
        real_append(repo, entry)

    def rewrite() -> None:
        h.put_lock(other)
        h.comment_error = RuntimeError("no token")

    monkeypatch.setattr(history, "append", append_break_only)
    _rewrite_before_delete(h, monkeypatch, [rewrite])
    with pytest.raises(ReclaimError) as caught:
        reclaim.break_lock(REPO, actor="human on mac.local", reason="stuck")
    message = str(caught.value)
    assert "did not land" in message
    assert "recording that also failed" in message
    assert "the history push was refused" in message
    assert "no token" in message
    assert [e.action for e in h.entries()] == ["break"]
    assert h.rearm_runs == []


def test_reclaim_record_and_sweep_failures_are_all_named(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_append(repo: str, entry: history.HistoryEntry) -> None:
        raise RefWriteError("the history push was refused")

    monkeypatch.setattr(history, "append", no_append)
    sweep = SweepError("listing the open PRs failed")
    h.rearm_outcome = sweep
    h.put_lock(_record(heartbeat_at=_stale_age()))
    with pytest.raises(ReclaimError) as caught:
        reclaim.reclaim_if_stale(REPO, actor="user-agent")
    assert caught.value.__cause__ is sweep
    assert "the history push was refused" in str(caught.value)
    assert "the re-arm sweep failed: listing the open PRs failed" in str(caught.value)
