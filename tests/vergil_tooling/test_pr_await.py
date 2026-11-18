"""Tests for vergil_tooling.lib.pr_await.

A fake GitHub scripts the PR's state sequence: every ``gh pr view`` snapshot
read takes the next scripted state, and the last one repeats. It also stands
in for the required-check lookup, the failed-check confirmation and the
orphan probe. A fake clock replaces ``time.monotonic`` and ``time.sleep``, so a
deadline or a grace period passes without waiting.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from vergil_tooling.lib import finalize_watch, github, pr_await
from vergil_tooling.lib.finalize_watch import DEADLINE_ENV, FinalizeWatchError
from vergil_tooling.lib.pr_await import PrAwaitError, wait_for_merge

PR = "https://github.com/o/r/pull/1"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
MERGE_SHA = "m" * 40
REQUIRED = frozenset({"test / unit", "CodeQL"})


def check_run(name: str, conclusion: str = "", n: int = 1) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
        "detailsUrl": f"https://github.com/o/r/actions/runs/1/job/{n}",
    }


def pr_state(**overrides: Any) -> dict[str, Any]:
    """One ``gh pr view`` snapshot: an open develop PR, auto-merge on, checks pending."""
    state: dict[str, Any] = {
        "id": "PR_1",
        "url": PR,
        "title": "feat: change 1",
        "state": "OPEN",
        "baseRefName": "develop",
        "headRefName": "feature/1-thing",
        "headRefOid": HEAD_A,
        "autoMergeRequest": {"mergeMethod": "SQUASH"},
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [check_run("test / unit")],
        "mergeCommit": None,
    }
    state.update(overrides)
    return state


def merged(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "state": "MERGED",
        "autoMergeRequest": None,
        "mergeStateStatus": "UNKNOWN",
        "mergeCommit": {"oid": MERGE_SHA},
        "statusCheckRollup": [check_run("test / unit", "SUCCESS")],
    }
    return pr_state(**{**fields, **overrides})


@dataclass
class FakeGitHub:
    """The PR's scripted states, the GitHub answers, and every call made."""

    states: list[dict[str, Any]] = field(default_factory=list)
    required: frozenset[str] | None = REQUIRED
    failed: list[str] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)
    clock: float = 0.0
    views: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    orphan_probes: int = 0
    required_reads: int = 0

    def script(self, *states: dict[str, Any]) -> None:
        self.states = list(states)

    def read_json(self, *args: str) -> object:
        assert args[:2] == ("pr", "view"), args
        self.views.append(args[2])
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return dict(state)

    def required_check_names(self, pr: str) -> frozenset[str] | None:
        assert pr == PR
        self.required_reads += 1
        return self.required

    def failed_check_names(self, pr: str) -> list[str]:
        assert pr == PR
        return list(self.failed)

    def orphaned_check_names(self, pr: str) -> list[str]:
        assert pr == PR
        self.orphan_probes += 1
        return list(self.orphans)

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.clock += secs


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    api = FakeGitHub()
    monkeypatch.delenv(DEADLINE_ENV, raising=False)
    monkeypatch.delenv(github.ORPHAN_GRACE_ENV, raising=False)
    for name in ("read_json", "required_check_names", "failed_check_names", "orphaned_check_names"):
        monkeypatch.setattr(github, name, getattr(api, name))
    monkeypatch.setattr(pr_await, "time", SimpleNamespace(monotonic=api.monotonic, sleep=api.sleep))
    return api


# --- merged: exit 0 --------------------------------------------------------------


def test_merged_before_the_wait_starts(fake: FakeGitHub) -> None:
    fake.script(merged())

    result = wait_for_merge(PR)

    assert result.merged
    assert result.merge_commit == MERGE_SHA
    assert result.to_output() == {
        "pr": PR,
        "outcome": "merged",
        "reason": None,
        "detail": "",
        "head_sha": HEAD_A,
        "merge_commit": MERGE_SHA,
        "merge_state_status": "UNKNOWN",
        "failed_checks": [],
        "orphaned_checks": [],
    }
    assert fake.views == [PR]
    assert fake.sleeps == []


def test_waits_until_merged(fake: FakeGitHub) -> None:
    fake.script(pr_state(), pr_state(), merged())

    result = wait_for_merge(PR, poll_interval=15)

    assert result.merged
    assert result.merge_commit == MERGE_SHA
    assert fake.sleeps == [15, 15]


def test_merged_with_no_merge_commit_is_an_error(fake: FakeGitHub) -> None:
    fake.script(merged(mergeCommit=None))

    with pytest.raises(PrAwaitError, match="names no merge commit"):
        wait_for_merge(PR)


def test_malformed_merge_commit_is_an_error(fake: FakeGitHub) -> None:
    fake.script(merged(mergeCommit={"oid": ""}))

    with pytest.raises(FinalizeWatchError, match="mergeCommit"):
        wait_for_merge(PR)


# --- reviews and commits never end the wait ----------------------------------------


def test_new_commit_does_not_end_the_wait(fake: FakeGitHub) -> None:
    fake.script(pr_state(), pr_state(headRefOid=HEAD_B), merged(headRefOid=HEAD_B))

    result = wait_for_merge(PR)

    assert result.merged
    assert result.head_sha == HEAD_B
    assert len(fake.views) == 3


def test_reviews_are_never_read() -> None:
    source = inspect.getsource(pr_await)
    attrs = {n.attr for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Attribute)}
    assert "pr_reviews" not in attrs
    assert "reviews" not in finalize_watch._VIEW_FIELDS  # noqa: SLF001


def test_all_checks_green_but_not_yet_merged_keeps_waiting(fake: FakeGitHub) -> None:
    green = [check_run("test / unit", "SUCCESS"), check_run("CodeQL", "SUCCESS", 2)]
    fake.script(pr_state(statusCheckRollup=green, mergeStateStatus="CLEAN"), merged())

    assert wait_for_merge(PR).merged


# --- cannot merge: exit 1 ------------------------------------------------------------


def test_closed_without_merging(fake: FakeGitHub) -> None:
    fake.script(pr_state(), pr_state(state="CLOSED", autoMergeRequest=None))

    result = wait_for_merge(PR)

    assert result.reason == "closed"
    assert result.to_output()["outcome"] == "cannot_merge"
    assert result.to_output()["merge_commit"] is None


def test_auto_merge_off_at_start(fake: FakeGitHub) -> None:
    fake.script(pr_state(autoMergeRequest=None))

    result = wait_for_merge(PR)

    assert result.reason == "auto_merge_off"
    assert fake.sleeps == []


def test_push_that_turns_auto_merge_off(fake: FakeGitHub) -> None:
    fake.script(pr_state(), pr_state(headRefOid=HEAD_B, autoMergeRequest=None))

    result = wait_for_merge(PR)

    assert result.reason == "auto_merge_off"
    assert result.head_sha == HEAD_B


def test_required_check_failed_is_named(fake: FakeGitHub) -> None:
    failing = [check_run("test / unit", "FAILURE"), check_run("CodeQL", n=2)]
    fake.script(pr_state(), pr_state(statusCheckRollup=failing))
    fake.failed = ["test / unit"]

    result = wait_for_merge(PR)

    assert result.reason == "check_failed"
    assert result.failed_checks == ("test / unit",)
    assert result.to_output()["failed_checks"] == ["test / unit"]
    assert "test / unit" in result.detail


def test_non_required_failure_keeps_waiting(fake: FakeGitHub) -> None:
    failing = [check_run("lint / optional", "FAILURE"), check_run("test / unit", n=2)]
    fake.script(pr_state(statusCheckRollup=failing), merged())
    fake.failed = ["lint / optional"]

    assert wait_for_merge(PR).merged


def test_conflict(fake: FakeGitHub) -> None:
    fake.script(pr_state(), pr_state(mergeStateStatus="DIRTY"))

    result = wait_for_merge(PR)

    assert result.reason == "conflict"
    assert result.merge_state_status == "DIRTY"


def test_deadline_passes_with_pr_pending(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "40")
    fake.script(pr_state())

    result = wait_for_merge(PR, poll_interval=15)

    assert result.reason == "timed_out"
    assert "40s" in result.detail
    assert DEADLINE_ENV in result.detail
    assert fake.sleeps == [15, 15, 10]
    assert fake.clock == 40


def test_orphaned_check_after_grace(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    fake.script(pr_state())
    fake.orphans = ["test / unit"]

    result = wait_for_merge(PR, poll_interval=15, orphan_grace=300)

    assert result.reason == "orphaned_check"
    assert result.orphaned_checks == ("test / unit",)
    assert fake.clock == 300
    err = capsys.readouterr().err
    assert err.count("GitHub may be delayed") == 1  # announced once, not per poll
    assert "re-checking for up to 5m00s" in err


def test_orphan_grace_defaults_from_env(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(github.ORPHAN_GRACE_ENV, "30")
    fake.script(pr_state())
    fake.orphans = ["test / unit"]

    result = wait_for_merge(PR, poll_interval=15)

    assert result.reason == "orphaned_check"
    assert fake.clock == 30


def test_late_result_within_grace_merges(fake: FakeGitHub) -> None:
    fake.script(pr_state(), pr_state(), merged())
    fake.orphans = ["test / unit"]

    assert wait_for_merge(PR, poll_interval=15, orphan_grace=300).merged


def test_grace_restarts_when_the_orphan_set_changes(fake: FakeGitHub) -> None:
    two = [check_run("test / unit"), check_run("CodeQL", n=2)]
    fake.script(pr_state(), pr_state(), pr_state(statusCheckRollup=two))
    fake.orphans = ["test / unit", "CodeQL"]

    result = wait_for_merge(PR, poll_interval=15, orphan_grace=60)

    assert result.reason == "orphaned_check"
    assert result.orphaned_checks == ("test / unit", "CodeQL")
    # The set changed at t=30, so the grace ran from there.
    assert fake.clock == 90


def test_pending_check_not_orphaned_keeps_waiting(fake: FakeGitHub) -> None:
    status = {"__typename": "StatusContext", "context": "app/status", "state": "PENDING"}
    rollup = [check_run("test / unit"), status]
    fake.script(pr_state(statusCheckRollup=rollup), pr_state(statusCheckRollup=rollup), merged())
    fake.orphans = ["test / unit"]  # the app status has no backing run

    assert wait_for_merge(PR, poll_interval=15, orphan_grace=1).merged


def test_blocked_with_nothing_pending_keeps_waiting_without_a_probe(fake: FakeGitHub) -> None:
    # A required check not registered yet: BLOCKED, but no check is pending.
    green = [check_run("test / unit", "SUCCESS")]
    fake.script(pr_state(statusCheckRollup=green), merged())
    fake.orphans = ["test / unit"]

    assert wait_for_merge(PR).merged
    assert fake.orphan_probes == 0


def test_orphan_probe_only_when_blocked(fake: FakeGitHub) -> None:
    fake.script(pr_state(mergeStateStatus="UNSTABLE"), merged())
    fake.orphans = ["test / unit"]

    assert wait_for_merge(PR).merged
    assert fake.orphan_probes == 0


# --- standing_block: one read, for a status view (#4678) -------------------------------


def test_standing_block_pending_pr_is_none_without_extra_reads(fake: FakeGitHub) -> None:
    fake.script(pr_state(mergeStateStatus="UNSTABLE"))

    assert pr_await.standing_block("4673") is None
    assert fake.views == ["4673"]
    assert fake.required_reads == 0
    assert fake.orphan_probes == 0
    assert fake.sleeps == []


def test_standing_block_merged_pr_is_none(fake: FakeGitHub) -> None:
    fake.script(merged())

    assert pr_await.standing_block(PR) is None


def test_standing_block_conflict(fake: FakeGitHub) -> None:
    fake.script(pr_state(mergeStateStatus="DIRTY"))

    result = pr_await.standing_block(PR)

    assert result is not None
    assert result.reason == "conflict"
    assert result.detail == "the PR has a merge conflict with its base"
    assert fake.required_reads == 0


def test_standing_block_auto_merge_off(fake: FakeGitHub) -> None:
    fake.script(pr_state(autoMergeRequest=None))

    result = pr_await.standing_block(PR)

    assert result is not None
    assert result.reason == "auto_merge_off"


def test_standing_block_required_check_failed(fake: FakeGitHub) -> None:
    fake.script(pr_state(statusCheckRollup=[check_run("test / unit", "FAILURE")]))
    fake.failed = ["test / unit"]

    result = pr_await.standing_block(PR)

    assert result is not None
    assert result.reason == "check_failed"
    assert result.failed_checks == ("test / unit",)
    assert fake.required_reads == 1


def test_standing_block_non_required_failure_is_none(fake: FakeGitHub) -> None:
    failing = [check_run("lint / optional", "FAILURE"), check_run("test / unit", n=2)]
    fake.script(pr_state(statusCheckRollup=failing, mergeStateStatus="UNSTABLE"))
    fake.failed = ["lint / optional"]

    assert pr_await.standing_block(PR) is None
    assert fake.required_reads == 1


def test_standing_block_orphaned_check_from_one_read(fake: FakeGitHub) -> None:
    fake.script(pr_state())
    fake.orphans = ["test / unit"]

    result = pr_await.standing_block(PR)

    assert result is not None
    assert result.reason == "orphaned_check"
    assert result.orphaned_checks == ("test / unit",)
    assert "GitHub is delayed" in result.detail
    assert fake.sleeps == []


def test_standing_block_unreadable_pr_raises(fake: FakeGitHub) -> None:
    fake.script(pr_state(state="WEIRD"))

    with pytest.raises(FinalizeWatchError, match="unknown state"):
        pr_await.standing_block(PR)


# --- configuration and read errors ------------------------------------------------------


@pytest.mark.parametrize("raw", ["0", "-5", "soon"])
def test_malformed_deadline_fails_before_any_read(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(DEADLINE_ENV, raw)
    fake.script(merged())

    with pytest.raises(ValueError, match=DEADLINE_ENV):
        wait_for_merge(PR)
    assert fake.views == []


def test_malformed_grace_fails_before_any_read(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(github.ORPHAN_GRACE_ENV, "0")
    fake.script(merged())

    with pytest.raises(ValueError, match=github.ORPHAN_GRACE_ENV):
        wait_for_merge(PR)
    assert fake.views == []


def test_non_positive_poll_interval_fails_before_any_read(fake: FakeGitHub) -> None:
    fake.script(merged())

    with pytest.raises(ValueError, match="poll_interval"):
        wait_for_merge(PR, poll_interval=0)
    assert fake.views == []


def test_unreadable_pr_raises(fake: FakeGitHub) -> None:
    fake.script(pr_state(state="WEIRD"))

    with pytest.raises(FinalizeWatchError, match="unknown state"):
        wait_for_merge(PR)
