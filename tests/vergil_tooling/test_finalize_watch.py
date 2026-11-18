"""Tests for vergil_tooling.lib.finalize_watch.

A fake GitHub scripts each PR's state sequence: every ``gh pr view`` snapshot
read takes the PR's next scripted state, and the last one repeats. The fake
also stands in for the provenance check, the required-check lookup, the
failed-check confirmation and the GraphQL auto-merge disable, and records
every call. A fake clock replaces ``time.monotonic`` and ``time.sleep``, so a
deadline passes without waiting.
"""

from __future__ import annotations

import ast
import inspect
import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from vergil_tooling.lib import auto_merge, finalize_watch, github, pr_provenance
from vergil_tooling.lib.finalize_watch import (
    DEADLINE_ENV,
    FinalizeWatchError,
    PRResult,
    deadline_secs,
    watch,
)
from vergil_tooling.lib.pr_provenance import Action, ProvenanceResult, Role

HEAD_A = "a" * 40
HEAD_B = "b" * 40
REQUIRED = frozenset({"test / unit", "CodeQL"})
AGENT_ENABLE = Action("x-vergil-user", Role.USER, "auto_merge_enabled")


def url(n: int) -> str:
    return f"https://github.com/o/r/pull/{n}"


def pr_state(n: int, **overrides: Any) -> dict[str, Any]:
    """One ``gh pr view`` snapshot of PR *n*: an open develop PR, auto-merge on, checks pending."""
    state: dict[str, Any] = {
        "id": f"PR_{n}",
        "url": url(n),
        "title": f"feat: change {n}",
        "state": "OPEN",
        "baseRefName": "develop",
        "headRefName": f"feature/{n}-thing",
        "headRefOid": HEAD_A,
        "autoMergeRequest": {"mergeMethod": "SQUASH"},
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "test / unit",
                "status": "IN_PROGRESS",
                "conclusion": "",
                "detailsUrl": f"https://github.com/o/r/actions/runs/1/job/{n}",
            }
        ],
    }
    state.update(overrides)
    return state


def merged(n: int, **overrides: Any) -> dict[str, Any]:
    return pr_state(n, state="MERGED", autoMergeRequest=None, **overrides)


def failing_check(name: str, n: int = 1) -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "status": "COMPLETED",
        "conclusion": "FAILURE",
        "detailsUrl": f"https://github.com/o/r/actions/runs/2/job/{n}",
    }


@dataclass
class FakeGitHub:
    """Each PR's scripted states, the provenance answers, and every call made."""

    states: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    violations: dict[tuple[str, str], list[Action]] = field(default_factory=dict)
    required: frozenset[str] | None = REQUIRED
    failed: dict[str, list[str]] = field(default_factory=dict)
    disable_works: bool = True
    clock: float = 0.0
    views: list[str] = field(default_factory=list)
    provenance_checks: list[tuple[str, str]] = field(default_factory=list)
    disables: list[str] = field(default_factory=list)
    graphql_queries: list[str] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    auto_merge_off: set[str] = field(default_factory=set)
    last: dict[str, dict[str, Any]] = field(default_factory=dict)

    def script(self, n: int, *states: dict[str, Any]) -> None:
        self.states[url(n)] = list(states)

    def _current(self, pr: str) -> dict[str, Any]:
        """The state the watch last read for *pr*."""
        return self.last[pr]

    def read_json(self, *args: str) -> object:
        assert args[:2] == ("pr", "view"), args
        pr = args[2]
        if pr.isdigit():
            pr = url(int(pr))
        self.views.append(pr)
        sequence = self.states[pr]
        state = sequence.pop(0) if len(sequence) > 1 else sequence[0]
        if pr in self.auto_merge_off:
            state = {**state, "autoMergeRequest": None}
        self.last[pr] = state
        return dict(state)

    def check_pr(self, pr: str) -> ProvenanceResult:
        head = str(self._current(pr)["headRefOid"])
        self.provenance_checks.append((pr, head))
        return ProvenanceResult(violations=list(self.violations.get((pr, head), [])))

    def required_check_names(self, pr: str) -> frozenset[str] | None:
        assert pr.startswith("https://"), pr
        return self.required

    def failed_check_names(self, pr: str) -> list[str]:
        return list(self.failed.get(pr, []))

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        self.graphql_queries.append(query)
        assert "disablePullRequestAutoMerge" in query, query
        node_id = str(variables["id"])
        pr = url(int(node_id.removeprefix("PR_")))
        self.disables.append(pr)
        if self.disable_works:
            self.auto_merge_off.add(pr)
        return {"disablePullRequestAutoMerge": {"pullRequest": {"number": 1}}}

    def auto_merge_enabled(self, pr: str) -> bool:
        return pr not in self.auto_merge_off and self._current(pr)["autoMergeRequest"] is not None

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.clock += secs


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    api = FakeGitHub()
    monkeypatch.delenv(DEADLINE_ENV, raising=False)
    for name in ("read_json", "graphql", "required_check_names", "failed_check_names"):
        monkeypatch.setattr(github, name, getattr(api, name))
    monkeypatch.setattr(pr_provenance, "check_pr", api.check_pr)
    monkeypatch.setattr(auto_merge, "auto_merge_enabled", api.auto_merge_enabled)
    clock = SimpleNamespace(monotonic=api.monotonic, sleep=api.sleep)
    monkeypatch.setattr(finalize_watch, "time", clock)
    return api


def run(prs: list[str], *, allow: bool = False, poll: float = 15.0) -> list[PRResult]:
    return watch(prs, allow_provenance_violation=allow, poll_interval=poll)


# --- start-up ------------------------------------------------------------------


def test_already_merged_counts(fake: FakeGitHub) -> None:
    fake.script(1, merged(1))
    fake.script(2, pr_state(2), pr_state(2), merged(2, headRefOid=HEAD_A))

    results = run([url(1), url(2)])

    assert results == [
        PRResult(pr=url(1), outcome="merged", detail="", head=HEAD_A),
        PRResult(pr=url(2), outcome="merged", detail="", head=HEAD_A),
    ]
    # The merged PR ended at start-up; only the open one was polled.
    assert fake.views.count(url(1)) == 1
    assert (url(1), HEAD_A) in fake.provenance_checks


def test_merged_pr_with_violation_flagged_not_disabled(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.script(1, merged(1))
    fake.violations[url(1), HEAD_A] = [AGENT_ENABLE]

    [result] = run([url(1)])

    assert result.outcome == "merged"
    assert result.provenance_violation is True
    assert "x-vergil-user (user): auto_merge_enabled" in result.detail
    assert fake.disables == []
    assert "PROVENANCE VIOLATION" in capsys.readouterr().err


def test_release_branch_not_watched(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, headRefName="release/1.2.0"))

    [result] = run([url(1)])

    assert result.outcome == "failed: not a develop PR"
    assert "release/1.2.0" in result.detail
    assert fake.provenance_checks == []
    assert fake.disables == []
    assert fake.sleeps == []


def test_non_develop_base_not_watched(fake: FakeGitHub) -> None:
    fake.script(1, merged(1, baseRefName="main"))

    [result] = run([url(1)])

    assert result.outcome == "failed: not a develop PR"
    assert "main" in result.detail
    assert fake.provenance_checks == []


def test_auto_merge_off_at_start_is_exception(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, autoMergeRequest=None))

    [result] = run([url(1)])

    assert result.outcome == "failed: auto-merge off"
    assert "investigate" in result.detail
    # Never turned back on: no GraphQL call at all, so no enable.
    assert fake.graphql_queries == []


def test_startup_violation_disables_and_fails(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1))
    fake.violations[url(1), HEAD_A] = [AGENT_ENABLE]

    [result] = run([url(1)])

    assert result == PRResult(
        pr=url(1),
        outcome="failed: provenance",
        detail="x-vergil-user (user): auto_merge_enabled",
        head=HEAD_A,
        provenance_violation=True,
    )
    assert fake.disables == [url(1)]
    assert fake.sleeps == []


def test_closed_at_start(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, state="CLOSED", autoMergeRequest=None))

    [result] = run([url(1)])

    assert result.outcome == "failed: closed"


def test_pr_number_is_resolved_to_its_url(fake: FakeGitHub) -> None:
    fake.script(4, merged(4))

    [result] = run(["4"])

    assert result.pr == url(4)


# --- polling and terminal states -------------------------------------------------


def test_ci_failure_names_check(fake: FakeGitHub) -> None:
    fake.script(
        1,
        pr_state(1),
        pr_state(1, statusCheckRollup=[failing_check("test / unit"), failing_check("lint", 2)]),
    )
    fake.failed[url(1)] = ["test / unit", "lint"]

    [result] = run([url(1)])

    assert result.outcome == "failed: CI"
    assert result.detail == "required check failed: test / unit"


def test_ci_failure_on_a_superseded_run_is_not_terminal(fake: FakeGitHub) -> None:
    rollup = [failing_check("test / unit")]
    fake.script(1, pr_state(1, statusCheckRollup=rollup), merged(1))
    fake.failed[url(1)] = []  # the confirmation leaves the superseded run out

    [result] = run([url(1)])

    assert result.outcome == "merged"


def test_non_required_check_failure_is_not_terminal(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, statusCheckRollup=[failing_check("lint")]), merged(1))
    fake.failed[url(1)] = ["lint"]

    [result] = run([url(1)])

    assert result.outcome == "merged"


def test_unreadable_required_checks_count_every_failed_check(fake: FakeGitHub) -> None:
    fake.required = None
    fake.script(1, pr_state(1, statusCheckRollup=[failing_check("lint")]))
    fake.failed[url(1)] = ["lint"]

    [result] = run([url(1)])

    assert result.outcome == "failed: CI"
    assert "lint" in result.detail


def test_failed_status_context_is_ci_failure(fake: FakeGitHub) -> None:
    status = {"__typename": "StatusContext", "context": "CodeQL", "state": "ERROR"}
    fake.script(1, pr_state(1, statusCheckRollup=[status]))
    fake.failed[url(1)] = ["CodeQL"]

    [result] = run([url(1)])

    assert result.outcome == "failed: CI"
    assert "CodeQL" in result.detail


def test_conflict(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1), pr_state(1, mergeStateStatus="DIRTY"))

    [result] = run([url(1)])

    assert result.outcome == "failed: conflict"


def test_closed(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1), pr_state(1, state="CLOSED", autoMergeRequest=None))

    [result] = run([url(1)])

    assert result.outcome == "failed: closed"


def test_auto_merge_turned_off_midwatch(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1), pr_state(1), pr_state(1, autoMergeRequest=None))

    [result] = run([url(1)])

    assert result.outcome == "failed: auto-merge off"
    assert fake.graphql_queries == []


def test_base_changed_midwatch_not_a_develop_pr(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1), pr_state(1, baseRefName="main"))

    [result] = run([url(1)])

    assert result.outcome == "failed: not a develop PR"


def test_provenance_rechecked_on_new_head(fake: FakeGitHub) -> None:
    fake.script(
        1,
        pr_state(1),
        pr_state(1),
        pr_state(1, headRefOid=HEAD_B),
        pr_state(1, headRefOid=HEAD_B),
        merged(1, headRefOid=HEAD_B),
    )

    [result] = run([url(1)])

    assert result.outcome == "merged"
    # Start-up, the new head, and the merge: never twice for one unmerged head.
    assert fake.provenance_checks == [(url(1), HEAD_A), (url(1), HEAD_B), (url(1), HEAD_B)]


def test_provenance_violation_disables_auto_merge(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1), pr_state(1, headRefOid=HEAD_B))
    fake.violations[url(1), HEAD_B] = [AGENT_ENABLE]

    [result] = run([url(1)])

    assert result.outcome == "failed: provenance"
    assert result.head == HEAD_B
    assert result.provenance_violation is True
    assert fake.disables == [url(1)]


def test_merged_pr_with_violation_flagged_alike_with_the_flag(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    """The override does not cover a PR that has already merged (vergil-tooling#4662)."""
    fake.script(1, merged(1))
    fake.violations[url(1), HEAD_A] = [AGENT_ENABLE]

    [result] = run([url(1)], allow=True)

    assert result.outcome == "merged"
    assert result.provenance_violation is True
    assert fake.disables == []
    err = capsys.readouterr().err
    assert "PROVENANCE VIOLATION" in err
    assert "investigate before any release" in err


def test_violation_found_at_merge_is_flagged(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.script(1, pr_state(1), merged(1))
    # The start-up check is clean; the check at merge finds the action.
    checks = iter([ProvenanceResult(), ProvenanceResult(violations=[AGENT_ENABLE])])
    monkeypatch.setattr(pr_provenance, "check_pr", lambda pr: next(checks))

    [result] = run([url(1)])

    assert result.outcome == "merged"
    assert result.provenance_violation is True
    assert fake.disables == []


def test_disable_that_does_not_take_raises(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1))
    fake.violations[url(1), HEAD_A] = [AGENT_ENABLE]
    fake.disable_works = False

    with pytest.raises(FinalizeWatchError, match="still on"):
        run([url(1)])


def test_disable_refused_raises(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.script(1, pr_state(1))
    fake.violations[url(1), HEAD_A] = [AGENT_ENABLE]

    def refuse(query: str, **variables: object) -> dict[str, object]:
        raise github.GitHubAPIError(1, ("gh", "api", "graphql"), stderr="forbidden")

    monkeypatch.setattr(github, "graphql", refuse)

    with pytest.raises(FinalizeWatchError, match="(?s)turning auto-merge off failed:.*forbidden"):
        run([url(1)])


def test_violation_with_auto_merge_already_off_is_not_disabled(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, autoMergeRequest=None))
    fake.violations[url(1), HEAD_A] = [AGENT_ENABLE]

    [result] = run([url(1)])

    assert result.outcome == "failed: provenance"
    assert fake.disables == []


def test_allow_provenance_violation(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    fake.script(1, pr_state(1), merged(1))
    fake.violations[url(1), HEAD_A] = [AGENT_ENABLE]

    [result] = run([url(1)], allow=True)

    assert result.outcome == "merged"
    assert result.provenance_violation is True
    assert fake.disables == []
    err = capsys.readouterr().err
    assert "--allow-provenance-violation" in err
    assert "still not a full success" in err


def test_watch_never_enables_auto_merge() -> None:
    source = inspect.getsource(finalize_watch)
    assert "enablePullRequestAutoMerge" not in source
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Attribute):
            imported.add(node.attr)
    assert "enable_auto_merge" not in imported


# --- the deadline ------------------------------------------------------------------


def test_deadline_times_out_pending_only(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "60")
    fake.script(1, pr_state(1))
    fake.script(2, pr_state(2), merged(2))

    results = run([url(1), url(2)], poll=15)

    assert [r.outcome for r in results] == ["timed out", "merged"]
    assert "60s" in results[0].detail
    assert sum(fake.sleeps) == 60
    assert fake.disables == []


def test_deadline_secs_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEADLINE_ENV, raising=False)
    assert deadline_secs() == 7200
    monkeypatch.setenv(DEADLINE_ENV, " 30 ")
    assert deadline_secs() == 30


@pytest.mark.parametrize("raw", ["abc", "0", "-5", "1.5"])
def test_deadline_env_malformed_raises_before_polling(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv(DEADLINE_ENV, raw)
    fake.script(1, pr_state(1))

    with pytest.raises(ValueError, match=DEADLINE_ENV):
        run([url(1)])

    assert fake.views == []
    assert fake.sleeps == []


def test_non_positive_poll_interval_raises(fake: FakeGitHub) -> None:
    with pytest.raises(ValueError, match="poll_interval"):
        run([url(1)], poll=0)


def test_one_poll_per_interval_for_ten_prs(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "150")
    for n in range(1, 11):
        fake.script(n, pr_state(n))

    results = run([url(n) for n in range(1, 11)], poll=15)

    assert {r.outcome for r in results} == {"timed out"}
    intervals = len(fake.sleeps)
    assert intervals == 10
    # One view per PR at start-up and one per PR per interval, nothing more.
    assert len(fake.views) == 10 * (1 + intervals)
    # Provenance runs once per PR head, not once per poll.
    assert len(fake.provenance_checks) == 10


def test_duplicate_prs_are_watched_once(fake: FakeGitHub) -> None:
    fake.script(1, merged(1))

    results = run([url(1), "1"])

    assert len(results) == 1


def test_unreadable_snapshot_raises(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: str) -> object:
        raise subprocess.CalledProcessError(1, ("gh", *args), stderr="HTTP 502")

    monkeypatch.setattr(github, "read_json", fail)

    with pytest.raises(FinalizeWatchError, match="HTTP 502"):
        run([url(1)])


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("state", "DRAFT"),
        ("headRefOid", None),
        ("title", ""),
        ("autoMergeRequest", "on"),
        ("statusCheckRollup", {"x": 1}),
        ("mergeStateStatus", 3),
    ],
)
def test_malformed_snapshot_raises(fake: FakeGitHub, field_name: str, value: object) -> None:
    fake.script(1, pr_state(1, **{field_name: value}))

    with pytest.raises(FinalizeWatchError, match=field_name):
        run([url(1)])


def test_non_object_snapshot_raises(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json", lambda *args: [])

    with pytest.raises(FinalizeWatchError, match="gh pr view returned"):
        run([url(1)])


def test_null_rollup_is_no_checks(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, statusCheckRollup=None), merged(1))

    [result] = run([url(1)])

    assert result.outcome == "merged"


def test_advisory_is_printed_not_a_violation(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.script(1, merged(1))
    advisory = Action("x-vergil-audit", Role.AUDIT, "approved")
    monkeypatch.setattr(
        pr_provenance, "check_pr", lambda pr: ProvenanceResult(advisories=[advisory])
    )

    [result] = run([url(1)])

    assert result.provenance_violation is False
    assert "ADVISORY" in capsys.readouterr().err


def test_disable_read_back_failure_raises(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.script(1, pr_state(1))
    fake.violations[url(1), HEAD_A] = [AGENT_ENABLE]

    def unreadable(pr: str) -> bool:
        raise auto_merge.AutoMergeError(f"{pr}: reading auto-merge failed")

    monkeypatch.setattr(auto_merge, "auto_merge_enabled", unreadable)

    with pytest.raises(FinalizeWatchError, match="reading it back failed"):
        run([url(1)])


# --- the status table (issue #4677) ------------------------------------------------


def _tables(out: str) -> list[str]:
    """Each status table printed in *out*, from its header line to its totals line."""
    tables = []
    for block in out.split("\nWatch status after ")[1:]:
        lines = block.splitlines()
        end = next(i for i, line in enumerate(lines) if "left before the batch deadline" in line)
        tables.append("\n".join(["Watch status after " + lines[0], *lines[1 : end + 1]]))
    return tables


def test_status_table_for_a_mixed_batch(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "30")
    fake.script(1, pr_state(1), merged(1))
    fake.script(2, pr_state(2))
    fake.script(3, pr_state(3), pr_state(3, statusCheckRollup=[failing_check("test / unit", 3)]))
    fake.failed[url(3)] = ["test / unit"]

    results = run([url(1), url(2), url(3)])

    assert [r.outcome for r in results] == ["merged", "timed out", "failed: CI"]
    # One table after start-up and one after each of the two polls.
    assert _tables(capsys.readouterr().out) == [
        "Watch status after 0m00s:\n"
        "  PR  TITLE           STATE               WAITING\n"
        "  #1  feat: change 1  checks pending (2)  0m00s\n"
        "  #2  feat: change 2  checks pending (2)  0m00s\n"
        "  #3  feat: change 3  checks pending (2)  0m00s\n"
        "  0 merged, 3 pending, 0 failed; 0m30s left before the batch deadline",
        "Watch status after 0m15s:\n"
        "  PR  TITLE           STATE               WAITING\n"
        "  #1  feat: change 1  merged              0m15s\n"
        "  #2  feat: change 2  checks pending (2)  0m15s\n"
        "  #3  feat: change 3  failed: CI          0m15s\n"
        "  1 merged, 1 pending, 1 failed; 0m15s left before the batch deadline",
        # The ended PRs keep their rows, their waiting time frozen.
        "Watch status after 0m30s:\n"
        "  PR  TITLE           STATE               WAITING\n"
        "  #1  feat: change 1  merged              0m15s\n"
        "  #2  feat: change 2  checks pending (2)  0m30s\n"
        "  #3  feat: change 3  failed: CI          0m15s\n"
        "  1 merged, 1 pending, 1 failed; 0m00s left before the batch deadline",
    ]


@pytest.mark.parametrize(
    ("overrides", "state"),
    [
        # test / unit still running, CodeQL not registered yet.
        ({}, "checks pending (2)"),
        (
            {
                "statusCheckRollup": [
                    {"__typename": "StatusContext", "context": "test / unit", "state": "PENDING"},
                    {"__typename": "CheckRun", "name": "CodeQL", "conclusion": "SUCCESS"},
                    {"__typename": "CheckRun", "name": "lint", "status": "QUEUED"},
                ]
            },
            "checks pending (2)",
        ),
        (
            {
                "statusCheckRollup": [
                    {"__typename": "StatusContext", "context": "test / unit", "state": "SUCCESS"},
                    {"__typename": "CheckRun", "name": "CodeQL", "conclusion": "SUCCESS"},
                ],
                "mergeStateStatus": "CLEAN",
            },
            "checks green, merging",
        ),
        (
            {
                "statusCheckRollup": [
                    {"__typename": "CheckRun", "name": "test / unit", "conclusion": "SUCCESS"},
                    {"__typename": "CheckRun", "name": "CodeQL", "conclusion": "SUCCESS"},
                ],
                "mergeStateStatus": "BEHIND",
            },
            "behind develop",
        ),
        (
            {
                "statusCheckRollup": [
                    {"__typename": "CheckRun", "name": "test / unit", "conclusion": "SUCCESS"},
                    {"__typename": "CheckRun", "name": "CodeQL", "conclusion": "SUCCESS"},
                ],
                "mergeStateStatus": "BLOCKED",
            },
            "waiting (blocked)",
        ),
        ({"statusCheckRollup": None, "mergeStateStatus": ""}, "checks pending (2)"),
    ],
)
def test_status_table_pending_states(
    fake: FakeGitHub,
    capsys: pytest.CaptureFixture[str],
    overrides: dict[str, Any],
    state: str,
) -> None:
    fake.script(1, pr_state(1, **overrides), merged(1))

    run([url(1)])

    first = _tables(capsys.readouterr().out)[0]
    assert f"  #1  feat: change 1  {state}  0m00s\n" in first


def test_status_table_with_unknown_required_checks(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.required = None
    fake.script(1, pr_state(1, statusCheckRollup=[], mergeStateStatus=""), merged(1))

    run([url(1)])

    assert "  #1  feat: change 1  waiting (no merge state)  0m00s\n" in capsys.readouterr().out


def test_status_table_shortens_long_titles_and_counts_hours(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "7200")
    title = "feat(finalize): print a status table on every poll of the watch phase"
    fake.script(1, pr_state(1, title=title), merged(1, title=title))

    run([url(1)], poll=3725.0)

    first, second = _tables(capsys.readouterr().out)
    assert "  #1  feat(finalize): print a status table on every p...  " in first
    assert title not in first
    assert "2h00m00s left before the batch deadline" in first
    assert "Watch status after 1h02m05s:" in second
    assert "merged  1h02m05s\n" in second
    assert "failed; 57m55s left before the batch deadline" in second


def test_status_table_when_every_pr_ended_at_start_up(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.script(1, merged(1))
    fake.script(2, pr_state(2, state="CLOSED"))

    run([url(1), url(2)])

    assert _tables(capsys.readouterr().out) == [
        "Watch status after 0m00s:\n"
        "  PR  TITLE           STATE           WAITING\n"
        "  #1  feat: change 1  merged          0m00s\n"
        "  #2  feat: change 2  failed: closed  0m00s\n"
        "  1 merged, 0 pending, 1 failed; 2h00m00s left before the batch deadline"
    ]
