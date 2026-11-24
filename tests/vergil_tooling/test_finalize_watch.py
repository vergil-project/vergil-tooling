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
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vergil_tooling.lib import (
    agent_gates,
    auto_merge,
    finalize_watch,
    git,
    github,
    pr_provenance,
    progress,
    trust,
)
from vergil_tooling.lib.change_fingerprint import change_fingerprint
from vergil_tooling.lib.finalize_watch import (
    DEADLINE_ENV,
    FinalizeWatchError,
    PRResult,
    deadline_secs,
    watch,
)
from vergil_tooling.lib.pr_provenance import Action, ProvenanceResult, Role
from vergil_tooling.lib.trusted_authors import TrustedAuthors

# The re-check's real functions, before any fixture stubs them.
REAL_RECHECK = finalize_watch.recheck_gates
REAL_FETCH = finalize_watch._fetch_for_gates
REAL_READY_STATE = finalize_watch.ready_state

HEAD_A = "a" * 40
HEAD_B = "b" * 40
REQUIRED = frozenset({"test / unit", "CodeQL"})
CLONE = Path("/clones/o/r")
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
        "labels": [],
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


def _meta(n: int) -> dict[str, Any]:
    """The title, branch and labels a :class:`PRResult` for :func:`pr_state` PR *n* carries."""
    return {"title": f"feat: change {n}", "branch": f"feature/{n}-thing", "labels": ()}


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
    run_reads: list[tuple[str, str]] = field(default_factory=list)
    last_pr: str = ""
    branch_lookups: list[tuple[str, str, str]] = field(default_factory=list)
    gate_failures: dict[tuple[str, str], str] = field(default_factory=dict)
    gate_checks: list[tuple[str, str, str]] = field(default_factory=list)

    def recheck_gates(self, snap: finalize_watch.Snapshot, branch: str, clone: Path) -> str:
        """The gates' answer for the PR's head: ``gate_failures``, else a pass."""
        assert clone == CLONE
        self.gate_checks.append((snap.url, snap.head, branch))
        return self.gate_failures.get((snap.url, snap.head), "")

    def prs_for_branch(
        self, repo: str, branch: str, *, state: str
    ) -> tuple[list[trust.PRRef], list[trust.Skipped]]:
        """The trust layer's answer: the PR last read, unless its state says ``_fork``."""
        self.branch_lookups.append((repo, branch, state))
        current = self._current(self.last_pr)
        number = int(self.last_pr.rsplit("/", 1)[-1])
        if current.get("_fork"):
            skip = trust.Skipped(repo, number, "pr", "mallory", "t", "head", trust.CROSS_REPOSITORY)
            return [], [skip]
        ref = trust.PRRef(
            number=number,
            url=self.last_pr,
            title=str(current["title"]),
            head=branch,
            head_oid=str(current["headRefOid"]),
            base=str(current["baseRefName"]),
            author_role=trust.Role.AGENTS,
            state=str(current["state"]),
        )
        return [ref], []

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
        self.last_pr = pr
        return {k: v for k, v in state.items() if not k.startswith("_")}

    def check_pr(self, pr: str) -> ProvenanceResult:
        head = str(self._current(pr)["headRefOid"])
        self.provenance_checks.append((pr, head))
        return ProvenanceResult(violations=list(self.violations.get((pr, head), [])))

    def required_check_names(self, pr: str) -> frozenset[str] | None:
        assert pr.startswith("https://"), pr
        return self.required

    def failed_check_names(self, pr: str) -> list[str]:
        """A state's own ``_failed`` list, else the PR's; gh's answer for that read."""
        return list(self._current(pr).get("_failed", self.failed.get(pr, [])))

    def head_workflow_runs(self, repo: str, sha: str) -> dict[int, github.WorkflowRun]:
        """The ``_runs`` of the state last read: the Actions runs on its head."""
        self.run_reads.append((repo, sha))
        state = self._current(self.last_pr)
        assert sha == state["headRefOid"]
        return {run.run_id: run for run in state.get("_runs", [])}

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
    for name in (
        "read_json",
        "graphql",
        "required_check_names",
        "failed_check_names",
        "head_workflow_runs",
    ):
        monkeypatch.setattr(github, name, getattr(api, name))
    monkeypatch.setattr(pr_provenance, "check_pr", api.check_pr)
    monkeypatch.setattr(trust, "prs_for_branch", api.prs_for_branch)
    monkeypatch.setattr(auto_merge, "auto_merge_enabled", api.auto_merge_enabled)
    monkeypatch.setattr(finalize_watch, "recheck_gates", api.recheck_gates)
    clock = SimpleNamespace(monotonic=api.monotonic, sleep=api.sleep)
    monkeypatch.setattr(finalize_watch, "time", clock)
    return api


def run(prs: list[str], *, poll: float = 15.0) -> list[PRResult]:
    return watch(prs, clone=CLONE, poll_interval=poll)


# --- start-up ------------------------------------------------------------------


def test_already_merged_counts(fake: FakeGitHub) -> None:
    fake.script(1, merged(1))
    fake.script(2, pr_state(2), pr_state(2), merged(2, headRefOid=HEAD_A))

    results = run([url(1), url(2)])

    assert results == [
        PRResult(pr=url(1), outcome="merged", detail="", head=HEAD_A, **_meta(1)),
        PRResult(pr=url(2), outcome="merged", detail="", head=HEAD_A, **_meta(2)),
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


def test_head_branch_confirmed_once_through_the_trust_layer(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1), pr_state(1), merged(1))

    [result] = run([url(1)])

    assert result.outcome == "merged"
    assert fake.branch_lookups == [("o/r", "feature/1-thing", "all")]


def test_unconfirmed_pr_not_watched(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A PR the trust layer does not confirm on its branch is never watched or named by branch."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake.script(1, pr_state(1, _fork=True))

    [result] = run([url(1)])

    assert result.outcome == "failed: not a develop PR"
    assert result.detail == "it is not a same-repo PR with a trusted author on its branch"
    assert result.branch == ""
    assert fake.provenance_checks == []
    assert fake.sleeps == []
    assert "feature/1-thing" not in capsys.readouterr().out


def test_trust_read_failure_stops_the_watch(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_a: object, **_kw: object) -> object:
        raise trust.TrustReadError("prs_for_branch o/r feature/1-thing", "no repository")

    monkeypatch.setattr(trust, "prs_for_branch", broken)
    fake.script(1, pr_state(1))

    with pytest.raises(FinalizeWatchError, match="confirming the PR through the trust layer"):
        run([url(1)])


def test_unparseable_url_stops_the_watch(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, url="https://example.com/not-a-pr"))

    with pytest.raises(FinalizeWatchError, match="cannot read the repository and number"):
        run([url(1)])


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
        **_meta(1),
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


# --- a superseded CI run is not a failure (#4692) ---------------------------------
#
# The shapes are those GitHub reported for PR vergil-project/.github#1324 on its
# head 5b32ced: CI run 44335987154 was cancelled by concurrency when run
# 44336022050 started on the same head, and the PR merged on the green run.

OLD_RUN = 44335987154
NEW_RUN = 44336022050
CI_WORKFLOW = 276918787
SEMGREP = "security / semgrep"
EVIDENCE = "quality / evidence"


def ci_run(
    run_id: int, status: str, conclusion: str = "", *, event: str = "pull_request"
) -> github.WorkflowRun:
    return github.WorkflowRun(run_id, CI_WORKFLOW, event, status, conclusion)


def ci_check(name: str, run_id: int, job: int, conclusion: str = "") -> dict[str, Any]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": "CI",
        "status": "COMPLETED" if conclusion else "IN_PROGRESS",
        "conclusion": conclusion,
        "detailsUrl": f"https://github.com/o/r/actions/runs/{run_id}/job/{job}",
    }


def test_issue_1324_superseded_cancel_then_green_run_merges(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.required = frozenset({SEMGREP, EVIDENCE})
    cancelled_semgrep = ci_check(SEMGREP, OLD_RUN, 134912726267, "CANCELLED")
    failed_evidence = ci_check(EVIDENCE, OLD_RUN, 134913028205, "FAILURE")
    fake.script(
        1,
        pr_state(1, statusCheckRollup=[ci_check(SEMGREP, OLD_RUN, 134912726267)]),
        # 15:28:28: semgrep cancelled; the newer run is queued, with no check-run yet.
        pr_state(
            1,
            statusCheckRollup=[cancelled_semgrep, ci_check(EVIDENCE, OLD_RUN, 134913028205)],
            _failed=[SEMGREP],
            _runs=[ci_run(OLD_RUN, "in_progress"), ci_run(NEW_RUN, "queued")],
        ),
        # 15:28:49: the cancelled run's evidence job failed as it wound down.
        pr_state(
            1,
            statusCheckRollup=[cancelled_semgrep, failed_evidence],
            _failed=[SEMGREP, EVIDENCE],
            _runs=[ci_run(OLD_RUN, "completed", "cancelled"), ci_run(NEW_RUN, "queued")],
        ),
        # The newer run's check-runs registered; gh reports the newest of each name.
        pr_state(
            1,
            statusCheckRollup=[
                cancelled_semgrep,
                failed_evidence,
                ci_check(SEMGREP, NEW_RUN, 134913147542),
                ci_check(EVIDENCE, NEW_RUN, 134914580766),
            ],
            _failed=[],
            _runs=[ci_run(OLD_RUN, "completed", "cancelled"), ci_run(NEW_RUN, "in_progress")],
        ),
        merged(1),
    )

    [result] = run([url(1)])

    assert result.outcome == "merged"
    assert ("o/r", HEAD_A) in fake.run_reads
    assert "failed" not in "".join(
        line for line in capsys.readouterr().out.splitlines() if line.startswith("  #1")
    )


def test_check_that_fails_between_the_two_reads_waits_for_the_next_snapshot(
    fake: FakeGitHub,
) -> None:
    # The snapshot still shows evidence running on the cancelled run; gh, read a
    # moment later, already reports it failed. The next snapshot shows it superseded.
    fake.required = frozenset({SEMGREP, EVIDENCE})
    runs = [ci_run(OLD_RUN, "in_progress"), ci_run(NEW_RUN, "queued")]
    cancelled_semgrep = ci_check(SEMGREP, OLD_RUN, 1, "CANCELLED")
    fake.script(
        1,
        pr_state(
            1,
            statusCheckRollup=[cancelled_semgrep, ci_check(EVIDENCE, OLD_RUN, 2)],
            _failed=[SEMGREP, EVIDENCE],
            _runs=runs,
        ),
        pr_state(
            1,
            statusCheckRollup=[cancelled_semgrep, ci_check(EVIDENCE, OLD_RUN, 2, "FAILURE")],
            _failed=[SEMGREP, EVIDENCE],
            _runs=runs,
        ),
        merged(1),
    )

    [result] = run([url(1)])

    assert result.outcome == "merged"


def test_cancelled_check_with_no_newer_run_ends_after_the_grace(fake: FakeGitHub) -> None:
    fake.required = frozenset({SEMGREP, EVIDENCE})
    fake.script(
        1,
        pr_state(
            1,
            statusCheckRollup=[
                ci_check(SEMGREP, OLD_RUN, 1, "CANCELLED"),
                ci_check(EVIDENCE, OLD_RUN, 2, "FAILURE"),
            ],
            _failed=[SEMGREP, EVIDENCE],
            _runs=[ci_run(OLD_RUN, "completed", "cancelled")],
        ),
    )

    [result] = watch([url(1)], clone=CLONE, cancel_grace=60)

    assert result.outcome == "failed: cancelled"
    assert result.detail == (
        "required check cancelled: security / semgrep, quality / evidence; "
        "no newer run started within 1m00s"
    )
    # Held pending for the grace, from the first read that saw it.
    assert fake.sleeps == [15.0] * 4


def test_cancelled_check_is_pending_in_the_status_table(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.required = frozenset({SEMGREP})
    cancelled = pr_state(
        1,
        statusCheckRollup=[ci_check(SEMGREP, OLD_RUN, 1, "CANCELLED")],
        _failed=[SEMGREP],
        _runs=[ci_run(OLD_RUN, "completed", "cancelled")],
    )
    fake.script(1, cancelled, merged(1))

    [result] = run([url(1)])

    assert result.outcome == "merged"
    assert "checks cancelled (1), awaiting a re-run" in capsys.readouterr().out


def test_cancelled_check_on_a_run_not_yet_completed_is_pending(fake: FakeGitHub) -> None:
    # A re-run of the cancelled run keeps its run id, and the run is in progress again.
    fake.required = frozenset({SEMGREP})
    rerun = pr_state(
        1,
        statusCheckRollup=[ci_check(SEMGREP, OLD_RUN, 1, "CANCELLED")],
        _failed=[SEMGREP],
        _runs=[ci_run(OLD_RUN, "in_progress")],
    )
    fake.script(1, rerun, rerun, rerun, merged(1))

    [result] = watch([url(1)], clone=CLONE, cancel_grace=1)

    assert result.outcome == "merged"


def test_failure_on_the_latest_run_fails_at_once(fake: FakeGitHub) -> None:
    fake.required = frozenset({EVIDENCE})
    fake.script(
        1,
        pr_state(
            1,
            statusCheckRollup=[ci_check(EVIDENCE, NEW_RUN, 1, "FAILURE")],
            _failed=[EVIDENCE],
            _runs=[ci_run(OLD_RUN, "completed", "cancelled"), ci_run(NEW_RUN, "in_progress")],
        ),
    )

    [result] = run([url(1)])

    assert result.outcome == "failed: CI"
    assert result.detail == "required check failed: quality / evidence"
    assert fake.sleeps == []


def test_a_newer_run_for_another_event_does_not_supersede(fake: FakeGitHub) -> None:
    fake.required = frozenset({SEMGREP})
    fake.script(
        1,
        pr_state(
            1,
            statusCheckRollup=[ci_check(SEMGREP, OLD_RUN, 1, "FAILURE")],
            _failed=[SEMGREP],
            _runs=[
                ci_run(OLD_RUN, "completed", "failure"),
                ci_run(NEW_RUN, "queued", event="push"),
            ],
        ),
    )

    [result] = run([url(1)])

    assert result.outcome == "failed: CI"


def test_newest_failed_occurrence_of_a_check_decides(fake: FakeGitHub) -> None:
    # The old run's cancel is superseded, but the newer run failed the same check.
    fake.required = frozenset({SEMGREP})
    fake.script(
        1,
        pr_state(
            1,
            statusCheckRollup=[
                ci_check(SEMGREP, NEW_RUN, 2, "FAILURE"),
                ci_check(SEMGREP, OLD_RUN, 1, "CANCELLED"),
            ],
            _failed=[SEMGREP],
            _runs=[ci_run(OLD_RUN, "completed", "cancelled"), ci_run(NEW_RUN, "completed")],
        ),
    )

    [result] = run([url(1)])

    assert result.outcome == "failed: CI"


def test_unreadable_workflow_runs_raise(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.script(1, pr_state(1, statusCheckRollup=[failing_check("test / unit")]))
    fake.failed[url(1)] = ["test / unit"]

    def refused(repo: str, sha: str) -> dict[int, github.WorkflowRun]:
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")

    monkeypatch.setattr(github, "head_workflow_runs", refused)

    with pytest.raises(FinalizeWatchError, match="reading the workflow runs"):
        run([url(1)])


def test_unreadable_actions_link_raises(fake: FakeGitHub) -> None:
    bad = {**failing_check("test / unit"), "detailsUrl": "https://example.com/actions/runs/2/job/1"}
    fake.script(1, pr_state(1, statusCheckRollup=[bad]))
    fake.failed[url(1)] = ["test / unit"]

    with pytest.raises(FinalizeWatchError, match="cannot read the repository"):
        run([url(1)])


def test_unreadable_actions_link_of_a_check_not_required_is_not_read(fake: FakeGitHub) -> None:
    bad = {**failing_check("lint"), "detailsUrl": "https://example.com/actions/runs/2/job/1"}
    fake.script(1, pr_state(1, statusCheckRollup=[bad]), merged(1))
    fake.failed[url(1)] = ["lint"]

    [result] = run([url(1)])

    assert result.outcome == "merged"


def test_a_pr_failed_on_its_checks_that_then_merges_is_merged(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, statusCheckRollup=[failing_check("test / unit")]), merged(1))
    fake.failed[url(1)] = ["test / unit"]
    fake.script(2, pr_state(2), pr_state(2), merged(2))

    results = run([url(1), url(2)])

    assert [r.outcome for r in results] == ["merged", "merged"]
    # The merged read ran the provenance check on it, as for any merged PR.
    assert fake.provenance_checks.count((url(1), HEAD_A)) == 2


def test_a_pr_failed_on_its_checks_that_stays_open_stays_failed(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, statusCheckRollup=[failing_check("test / unit")]))
    fake.failed[url(1)] = ["test / unit"]
    fake.script(2, pr_state(2), merged(2))

    results = run([url(1), url(2)])

    assert [r.outcome for r in results] == ["failed: CI", "merged"]
    assert fake.views.count(url(1)) == 2


def test_new_head_of_a_pr_failed_on_its_checks_failing_the_gates_disarms(
    fake: FakeGitHub,
) -> None:
    """A fix pushed to a PR that ended on its checks gets the gates while the watch goes on."""
    failing = [failing_check("test / unit")]
    fake.script(
        1,
        pr_state(1, statusCheckRollup=failing),
        pr_state(1, statusCheckRollup=failing),
        pr_state(1, headRefOid=HEAD_B),
    )
    fake.failed[url(1)] = ["test / unit"]
    fake.gate_failures[url(1), HEAD_B] = "protected file .github/workflows/ci.yml unapproved"
    fake.script(2, pr_state(2), pr_state(2), pr_state(2), merged(2))

    results = run([url(1), url(2)])

    assert [r.outcome for r in results] == ["failed: gates", "merged"]
    assert results[0].head == HEAD_B
    assert results[0].detail == "protected file .github/workflows/ci.yml unapproved"
    assert fake.disables == [url(1)]
    assert (url(1), HEAD_B, "feature/1-thing") in fake.gate_checks


def test_new_head_of_a_pr_failed_on_its_checks_with_a_violation_disarms(
    fake: FakeGitHub,
) -> None:
    failing = [failing_check("test / unit")]
    fake.script(
        1,
        pr_state(1, statusCheckRollup=failing),
        pr_state(1, statusCheckRollup=failing),
        pr_state(1, headRefOid=HEAD_B),
    )
    fake.failed[url(1)] = ["test / unit"]
    fake.violations[url(1), HEAD_B] = [AGENT_ENABLE]
    fake.script(2, pr_state(2), pr_state(2), pr_state(2), merged(2))

    results = run([url(1), url(2)])

    assert [r.outcome for r in results] == ["failed: provenance", "merged"]
    assert results[0].provenance_violation is True
    assert fake.disables == [url(1)]


def test_new_head_of_a_pr_failed_on_its_checks_passing_the_gates_can_still_merge(
    fake: FakeGitHub,
) -> None:
    failing = [failing_check("test / unit")]
    fake.script(
        1,
        pr_state(1, statusCheckRollup=failing),
        pr_state(1, statusCheckRollup=failing),
        pr_state(1, headRefOid=HEAD_B),
        merged(1, headRefOid=HEAD_B),
    )
    fake.failed[url(1)] = ["test / unit"]
    fake.script(2, pr_state(2), pr_state(2), pr_state(2), pr_state(2), merged(2))

    results = run([url(1), url(2)])

    assert [r.outcome for r in results] == ["merged", "merged"]
    assert fake.disables == []
    # The new head was checked once while open, never again until it merged.
    assert [c for c in fake.gate_checks if c[0] == url(1)] == [
        (url(1), HEAD_A, "feature/1-thing"),
        (url(1), HEAD_B, "feature/1-thing"),
    ]


def test_conflict(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1), pr_state(1, mergeStateStatus="DIRTY"))

    [result] = run([url(1)])

    assert result.outcome == "failed: conflict"
    assert result.detail == ("the PR can no longer merge cleanly (reported at its head for 2m00s)")
    # Held pending for the settle time, from the first read that saw it.
    assert fake.sleeps == [15.0] * 9


def test_a_conflict_that_clears_before_it_settles_is_no_failure(fake: FakeGitHub) -> None:
    """Just after a push GitHub may still report the conflict the push resolved;
    a status that clears within the settle time never fails the PR (#4998)."""
    dirty = pr_state(1, mergeStateStatus="DIRTY")
    fake.script(1, dirty, dirty, pr_state(1, mergeStateStatus="CLEAN"), merged(1))

    [result] = run([url(1)])

    assert result.outcome == "merged"


def test_a_new_head_starts_the_conflict_settle_again(fake: FakeGitHub) -> None:
    dirty_a = pr_state(1, mergeStateStatus="DIRTY")
    dirty_b = pr_state(1, mergeStateStatus="DIRTY", headRefOid=HEAD_B)
    fake.script(1, dirty_a, dirty_a, dirty_b)

    [result] = watch([url(1)], clone=CLONE, conflict_settle=30)

    assert result.outcome == "failed: conflict"
    assert result.head == HEAD_B
    # HEAD_B is first read at 30s, so the conflict settles at 60s, not 30s.
    assert fake.sleeps == [15.0] * 4


def test_a_settling_conflict_is_pending_in_the_status_table(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.script(1, pr_state(1, mergeStateStatus="DIRTY"), merged(1))

    run([url(1)])

    first = _tables(capsys.readouterr().out)[0]
    assert "  #1  feat: change 1  conflict reported, waiting for GitHub to settle it  0m00s\n" in (
        first
    )


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
        ("labels", None),
        ("labels", [{"id": "L_1"}]),
    ],
)
def test_malformed_snapshot_raises(fake: FakeGitHub, field_name: str, value: object) -> None:
    fake.script(1, pr_state(1, **{field_name: value}))

    with pytest.raises(FinalizeWatchError, match=field_name):
        run([url(1)])


def test_result_carries_title_branch_and_labels(fake: FakeGitHub) -> None:
    labels = [{"id": "L_1", "name": "dependency-update"}, {"id": "L_2", "name": "x"}]
    fake.script(1, merged(1, labels=labels))

    [result] = run([url(1)])

    assert (result.title, result.branch, result.labels) == (
        "feat: change 1",
        "feature/1-thing",
        ("dependency-update", "x"),
    )


def test_non_object_snapshot_raises(fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_json", lambda *args: [])

    with pytest.raises(FinalizeWatchError, match="gh pr view returned"):
        run([url(1)])


def test_null_rollup_is_no_checks(fake: FakeGitHub) -> None:
    fake.script(1, pr_state(1, statusCheckRollup=None), merged(1))

    [result] = run([url(1)])

    assert result.outcome == "merged"


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


@dataclass
class _Block:
    lines: list[str]
    changed: bool
    heartbeat: str

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def blocks(monkeypatch: pytest.MonkeyPatch) -> list[_Block]:
    """Every status block the watch sends to the progress display, in order."""
    sent: list[_Block] = []

    def record(lines: list[str], *, changed: bool, heartbeat: str) -> None:
        sent.append(_Block(list(lines), changed, heartbeat))

    monkeypatch.setattr(progress, "status_block", record)
    return sent


def test_status_table_for_a_mixed_batch(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, blocks: list[_Block]
) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "30")
    fake.script(1, pr_state(1), merged(1))
    fake.script(2, pr_state(2))
    fake.script(3, pr_state(3), pr_state(3, statusCheckRollup=[failing_check("test / unit", 3)]))
    fake.failed[url(3)] = ["test / unit"]

    results = run([url(1), url(2), url(3)])

    assert [r.outcome for r in results] == ["merged", "timed out", "failed: CI"]
    # One table after start-up and one after each of the two polls.
    assert [b.text for b in blocks] == [
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
    # The last poll changed no PR's state, only the waiting times (issue #4694).
    assert [b.changed for b in blocks] == [True, True, False]
    assert blocks[2].heartbeat == (
        "Watch status after 0m30s: no change; "
        "1 merged, 1 pending, 1 failed; 0m00s left before the batch deadline"
    )


def test_status_table_change_of_pending_state_is_a_change(
    fake: FakeGitHub, blocks: list[_Block]
) -> None:
    clean = pr_state(
        1,
        statusCheckRollup=[
            {"__typename": "CheckRun", "name": "test / unit", "conclusion": "SUCCESS"},
            {"__typename": "CheckRun", "name": "CodeQL", "conclusion": "SUCCESS"},
        ],
        mergeStateStatus="CLEAN",
    )
    fake.script(1, pr_state(1), pr_state(1), clean, merged(1))

    run([url(1)])

    states = [re.split(r"\s{2,}", b.lines[2].strip())[2] for b in blocks]
    assert states == ["checks pending (2)", "checks pending (2)", "checks green, merging", "merged"]
    assert [b.changed for b in blocks] == [True, False, True, True]


def test_status_table_off_a_tty_prints_changes_only(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DEADLINE_ENV, "60")
    fake.script(1, pr_state(1))

    run([url(1)])

    # Five tables were built (start-up and four polls), but no PR's state
    # changed after the first; outside a progress stage only changes print.
    assert len(_tables(capsys.readouterr().out)) == 1


def test_summarize() -> None:
    def result(outcome: finalize_watch.Outcome) -> PRResult:
        return PRResult(pr=url(1), outcome=outcome, detail="", head=HEAD_A, **_meta(1))

    assert finalize_watch.summarize([result("merged")] * 4 + [result("failed: CI")]) == (
        "4 merged, 1 failed"
    )
    assert finalize_watch.summarize([result("merged"), result("timed out")]) == (
        "1 merged, 0 failed, 1 timed out"
    )


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


# --- the gates' re-check (epic vergil-project/.github#1384, plan Task 11) ------------

AUTHORS = TrustedAuthors(human=("human",), agents=("human-vergil-user",))
AGENT_ID = trust.Identity("human-vergil-user", "Bot")
HUMAN_ID = trust.Identity("human", "User")
TASK = "o/r#7"
LINKED_BODY = f"# Pull Request\n\n## Issue Linkage\n\n- Ref {TASK}\n"
WORKFLOW = ".github/workflows/ci.yml"


def _sh(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _sh(repo, "add", "-A")
    _sh(repo, "commit", "-q", "-m", message)
    return _sh(repo, "rev-parse", "HEAD")


@dataclass
class Gated:
    """A clone of o/r with ``develop`` and ``feature``, and the gates' GitHub reads stubbed.

    *records* are the fingerprints the merge-review records on the task
    issue cover; *actors* the PR's GitHub actors; *ready* its ready state;
    *body* its trusted body. *evaluations* records each evaluation's
    ``(base, head, issue_refs)``.
    """

    clone: Path
    records: list[str] = field(default_factory=list)
    actors: tuple[trust.Identity, ...] = (AGENT_ID,)
    ready: finalize_watch.ReadyState | None = None
    body: str = LINKED_BODY
    evaluations: list[tuple[str, str, list[str]]] = field(default_factory=list)
    actor_reads: list[str] = field(default_factory=list)

    def develop(self) -> str:
        return _sh(self.clone, "rev-parse", "develop")

    def fingerprint(self) -> str:
        return change_fingerprint(self.clone, "develop", "feature")


@pytest.fixture
def gated(fake: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Gated:
    """The real :func:`recheck_gates` and evaluator over a local clone; GitHub stubbed."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    clone = tmp_path / "clone"
    clone.mkdir()
    _sh(clone, "init", "-q", "-b", "develop")
    _commit(
        clone, {"a.txt": "one\ntwo\nthree\n", "b.txt": "uno\ndos\n", WORKFLOW: "on: push\n"}, "base"
    )
    _sh(clone, "checkout", "-q", "-b", "feature")
    state = Gated(clone=clone)
    monkeypatch.setenv(DEADLINE_ENV, "60")

    monkeypatch.setattr(finalize_watch, "recheck_gates", REAL_RECHECK)
    monkeypatch.setattr(
        git, "remote_url", lambda remote="origin", repo=None: "https://github.com/o/r.git"
    )
    monkeypatch.setattr(finalize_watch, "load_trusted_authors", lambda: AUTHORS)

    def actors(pr: str) -> tuple[trust.Identity, ...]:
        state.actor_reads.append(pr)
        return state.actors

    monkeypatch.setattr(pr_provenance, "pr_actors", actors)
    monkeypatch.setattr(finalize_watch, "ready_state", lambda clone, repo, branch: state.ready)
    monkeypatch.setattr(
        trust,
        "read_issue",
        lambda repo, number, **_kw: SimpleNamespace(body=state.body),
    )

    def fetch(fetched: Path, head: str) -> str:
        assert fetched == clone
        _sh(clone, "cat-file", "-e", f"{head}^{{commit}}")
        return state.develop()

    monkeypatch.setattr(finalize_watch, "_fetch_for_gates", fetch)
    monkeypatch.setattr(
        agent_gates,
        "records",
        lambda ref, *, authors: [SimpleNamespace(fingerprint=f) for f in state.records],
    )
    monkeypatch.setattr(agent_gates, "verified_approvals", lambda ref, *, authors: [])
    monkeypatch.setattr(agent_gates, "overrides", lambda ref, *, authors: [])
    real_evaluate = agent_gates.evaluate

    def evaluate(repo: Path, *, base: str, head: str, issue_refs: list[str], authors: Any) -> Any:
        state.evaluations.append((base, head, list(issue_refs)))
        return real_evaluate(repo, base=base, head=head, issue_refs=issue_refs, authors=authors)

    monkeypatch.setattr(agent_gates, "evaluate", evaluate)
    return state


def _watch_gated(gated: Gated) -> list[PRResult]:
    return watch([url(1)], clone=gated.clone)


def test_new_head_touching_workflow_without_approval_disarms(
    fake: FakeGitHub, gated: Gated, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    gated.records.append(gated.fingerprint())
    second = _commit(gated.clone, {WORKFLOW: "on: [push, pull_request]\n"}, "touch CI")
    fake.script(1, pr_state(1, headRefOid=first), pr_state(1, headRefOid=second))

    [result] = _watch_gated(gated)

    assert result.outcome == "failed: gates"
    assert result.head == second
    assert f"protected file {WORKFLOW}" in result.detail
    assert "no approved protected-change approval on o/r#7" in result.detail
    assert fake.disables == [url(1)]
    assert [head for _base, head, _refs in gated.evaluations] == [first, second]
    assert "GATES NOT PASSED" in capsys.readouterr().err


def test_clean_rebase_keeps_auto_merge(fake: FakeGitHub, gated: Gated) -> None:
    first = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    gated.records.append(gated.fingerprint())
    _sh(gated.clone, "checkout", "-q", "develop")
    _commit(gated.clone, {"b.txt": "uno\ndos\ntres\n"}, "develop moves on")
    _sh(gated.clone, "checkout", "-q", "feature")
    _sh(gated.clone, "rebase", "-q", "develop")
    rebased = _sh(gated.clone, "rev-parse", "HEAD")
    assert rebased != first
    fake.script(
        1,
        pr_state(1, headRefOid=first),
        pr_state(1, headRefOid=rebased),
        merged(1, headRefOid=rebased),
    )

    [result] = _watch_gated(gated)

    assert result.outcome == "merged"
    assert fake.disables == []
    # Both heads were evaluated, the rebased one against the moved develop.
    assert [head for _base, head, _refs in gated.evaluations] == [first, rebased]
    assert gated.evaluations[1][0] == gated.develop()


def test_gates_failing_at_start_disarm(fake: FakeGitHub, gated: Gated) -> None:
    head = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    fake.script(1, pr_state(1, headRefOid=head))

    [result] = _watch_gated(gated)

    assert result.outcome == "failed: gates"
    assert "no merge-review record" in result.detail
    assert fake.disables == [url(1)]
    assert fake.sleeps == []


def test_gates_failing_with_auto_merge_already_off_disable_nothing(
    fake: FakeGitHub, gated: Gated
) -> None:
    head = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    fake.script(1, pr_state(1, headRefOid=head, autoMergeRequest=None))

    [result] = _watch_gated(gated)

    assert result.outcome == "failed: gates"
    assert fake.disables == []


def test_provenance_violation_ends_before_the_gates(fake: FakeGitHub, gated: Gated) -> None:
    head = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    fake.script(1, pr_state(1, headRefOid=head))
    fake.violations[url(1), head] = [AGENT_ENABLE]

    [result] = _watch_gated(gated)

    assert result.outcome == "failed: provenance"
    assert gated.evaluations == []


def test_merged_pr_is_not_rechecked(fake: FakeGitHub, gated: Gated) -> None:
    head = _commit(gated.clone, {WORKFLOW: "on: [push, pull_request]\n"}, "touch CI")
    fake.script(1, merged(1, headRefOid=head))

    [result] = _watch_gated(gated)

    assert result.outcome == "merged"
    assert gated.evaluations == []
    assert gated.actor_reads == []


@pytest.mark.parametrize(
    "broken",
    ["actors", "body", "fetch", "untrusted body"],
)
def test_recheck_read_failure_disarms(
    fake: FakeGitHub, gated: Gated, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    """A read the re-check cannot complete turns auto-merge off, never left armed."""
    head = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    gated.records.append(gated.fingerprint())
    fake.script(1, pr_state(1, headRefOid=head))

    def api_error(*_args: object, **_kwargs: object) -> object:
        raise github.GitHubAPIError(1, ("gh", "api"), stderr="HTTP 502")

    def untrusted(*_args: object, **_kwargs: object) -> object:
        item = trust.Skipped("o/r", 1, "pr", "drive-by", "t", "body", trust.CROSS_REPOSITORY)
        raise trust.UntrustedContentError(item, "the PR's body is untrusted")

    def fetch_failed(*_args: object) -> str:
        raise subprocess.CalledProcessError(128, ("git", "fetch"), stderr="fatal: unreachable")

    target, replacement = {
        "actors": ((pr_provenance, "pr_actors"), api_error),
        "body": ((trust, "read_issue"), api_error),
        "fetch": ((finalize_watch, "_fetch_for_gates"), fetch_failed),
        "untrusted body": ((trust, "read_issue"), untrusted),
    }[broken]
    monkeypatch.setattr(*target, replacement)

    [result] = _watch_gated(gated)

    assert result.outcome == "failed: gates"
    assert result.detail.startswith(f"the gates could not be re-checked at {head[:12]}: ")
    assert fake.disables == [url(1)]
    assert gated.evaluations == []


def test_recheck_refuses_other_repo_clone(
    fake: FakeGitHub, gated: Gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    fake.script(1, pr_state(1, headRefOid=head))
    monkeypatch.setattr(
        git, "remote_url", lambda remote="origin", repo=None: "git@github.com:o/other.git"
    )

    with pytest.raises(
        FinalizeWatchError, match=r"whose origin is o/other, not the PR's repository o/r"
    ):
        _watch_gated(gated)

    assert gated.actor_reads == []
    assert gated.evaluations == []
    assert fake.disables == []


def test_recheck_refuses_a_clone_with_no_readable_origin(
    fake: FakeGitHub, gated: Gated, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    fake.script(1, pr_state(1, headRefOid=head))

    def no_origin(remote: str = "origin", repo: Path | None = None) -> str:
        raise subprocess.CalledProcessError(2, ("git", "remote"), stderr="error: No such remote")

    monkeypatch.setattr(git, "remote_url", no_origin)

    with pytest.raises(FinalizeWatchError, match="cannot read the origin of"):
        _watch_gated(gated)

    assert gated.actor_reads == []
    assert fake.disables == []


def test_human_only_pr_skips_the_gates(fake: FakeGitHub, gated: Gated) -> None:
    head = _commit(gated.clone, {WORKFLOW: "on: [push, pull_request]\n"}, "touch CI")
    gated.actors = (HUMAN_ID,)
    fake.script(1, pr_state(1, headRefOid=head), merged(1, headRefOid=head))

    [result] = _watch_gated(gated)

    assert result.outcome == "merged"
    assert gated.evaluations == []


def test_recorded_by_human_never_skips_the_gates_of_an_agent_pr(
    fake: FakeGitHub, gated: Gated
) -> None:
    """A forged ``recorded_by: human`` does not make an agent's PR human-only."""
    head = _commit(gated.clone, {WORKFLOW: "on: [push, pull_request]\n"}, "touch CI")
    gated.actors = (HUMAN_ID, AGENT_ID)
    gated.ready = finalize_watch.ReadyState(trusted=True, issues=(TASK,), recorded_by="human")
    fake.script(1, pr_state(1, headRefOid=head))

    [result] = _watch_gated(gated)

    assert result.outcome == "failed: gates"
    assert len(gated.evaluations) == 1
    assert fake.disables == [url(1)]


@pytest.mark.parametrize(
    "ready",
    [
        finalize_watch.ReadyState(trusted=True, issues=(TASK,)),
        finalize_watch.ReadyState(trusted=True, issues=(TASK,), recorded_by="user"),
        finalize_watch.ReadyState(trusted=True, issues=(TASK,), recorded_by="audit"),
        finalize_watch.ReadyState(trusted=True, issues=(TASK,), recorded_by="robot"),
        finalize_watch.ReadyState(trusted=True, issues=(TASK,), recorded_by=["human"]),
        finalize_watch.ReadyState(trusted=False),
    ],
    ids=["missing", "user", "audit", "unknown", "not a string", "untrusted"],
)
def test_ready_state_makes_a_human_actors_pr_agent_involved(
    fake: FakeGitHub, gated: Gated, ready: finalize_watch.ReadyState
) -> None:
    """GitHub records no pusher: the ready state adds the involvement it cannot show."""
    head = _commit(gated.clone, {WORKFLOW: "on: [push, pull_request]\n"}, "touch CI")
    gated.actors = (HUMAN_ID,)
    gated.ready = ready
    fake.script(1, pr_state(1, headRefOid=head))

    [result] = _watch_gated(gated)

    assert result.outcome == "failed: gates"
    assert len(gated.evaluations) == 1


def test_ready_state_recorded_by_human_with_human_actors_is_human_only(
    fake: FakeGitHub, gated: Gated
) -> None:
    head = _commit(gated.clone, {WORKFLOW: "on: [push, pull_request]\n"}, "touch CI")
    gated.actors = (HUMAN_ID,)
    gated.ready = finalize_watch.ReadyState(trusted=True, issues=(TASK,), recorded_by="human")
    fake.script(1, pr_state(1, headRefOid=head), merged(1, headRefOid=head))

    [result] = _watch_gated(gated)

    assert result.outcome == "merged"
    assert gated.evaluations == []


def test_task_issues_come_from_the_trusted_body(fake: FakeGitHub, gated: Gated) -> None:
    head = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    gated.body = "## Issue Linkage\n\n- Closes #7\n- Closes #8\n"
    gated.ready = finalize_watch.ReadyState(trusted=True, issues=("#9",), recorded_by="user")
    fake.script(1, pr_state(1, headRefOid=head))

    _watch_gated(gated)

    assert gated.evaluations[0][2] == ["o/r#7", "o/r#8"]


@pytest.mark.parametrize(
    ("ready", "expected"),
    [
        (finalize_watch.ReadyState(trusted=True, issues=("#9",), recorded_by="user"), ["o/r#9"]),
        (finalize_watch.ReadyState(trusted=False), []),
        (None, []),
    ],
    ids=["trusted ready state", "untrusted ready state", "no ready state"],
)
def test_task_issues_fall_back_to_a_trusted_ready_state(
    fake: FakeGitHub,
    gated: Gated,
    ready: finalize_watch.ReadyState | None,
    expected: list[str],
) -> None:
    head = _commit(gated.clone, {"a.txt": "one\nTWO\nthree\n"}, "change")
    gated.body = "# Pull Request\n\nNo linkage here.\n"
    gated.ready = ready
    fake.script(1, pr_state(1, headRefOid=head))

    [result] = _watch_gated(gated)

    assert gated.evaluations[0][2] == expected
    assert result.outcome == "failed: gates"
    if not expected:
        assert "resolves to no task issue" in result.detail


# --- the re-check's git reads, against a local origin ---------------------------------


@pytest.fixture
def remote(gated: Gated, tmp_path: Path) -> Path:
    """A bare origin the clone fetches from, holding develop and the clone's history."""
    bare = tmp_path / "origin.git"
    _sh(tmp_path, "init", "-q", "--bare", str(bare))
    _sh(gated.clone, "remote", "add", "origin", str(bare))
    _sh(gated.clone, "push", "-q", "origin", "develop")
    return bare


def test_recheck_fetches_unseen_head(
    fake: FakeGitHub, gated: Gated, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A head made outside the clone is fetched by SHA; no feature tracking ref moves."""
    monkeypatch.setattr(finalize_watch, "_fetch_for_gates", REAL_FETCH)
    other = tmp_path / "elsewhere"
    _sh(tmp_path, "clone", "-q", "-b", "develop", str(remote), str(other))
    _sh(other, "checkout", "-q", "-b", "feature/1-thing")
    head = _commit(other, {"a.txt": "one\nTWO\nthree\n"}, "change")
    _sh(other, "push", "-q", "origin", "feature/1-thing")
    gated.records.append(change_fingerprint(other, "develop", head))
    with pytest.raises(subprocess.CalledProcessError):
        _sh(gated.clone, "cat-file", "-e", f"{head}^{{commit}}")
    fake.script(1, pr_state(1, headRefOid=head))

    [result] = _watch_gated(gated)

    assert result.outcome != "failed: gates", result.detail
    assert gated.evaluations == [(gated.develop(), head, [TASK])]
    _sh(gated.clone, "cat-file", "-e", f"{head}^{{commit}}")
    tracking = _sh(gated.clone, "for-each-ref", "--format=%(refname)", "refs/remotes/")
    assert tracking.splitlines() == ["refs/remotes/origin/develop"]


def _relay(remote: Path, tmp_path: Path, branch: str, payload: object) -> str:
    """Write *payload* as *branch*'s relay ref in *remote*, as report-ready does."""
    work = tmp_path / "relay"
    work.mkdir(exist_ok=True)
    blob = subprocess.run(
        ["git", "-C", str(remote), "hash-object", "-w", "--stdin"],
        input=json.dumps(payload),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(remote), "mktree"],
        input=f"100644 blob {blob}\tpr-workflow.json\n",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    commit = _sh(work, "-C", str(remote), "commit-tree", tree, "-m", "vergil: pr-workflow relay")
    _sh(work, "-C", str(remote), "update-ref", f"refs/vergil/pr-workflow/{branch}", commit)
    return commit


def _payload(**extra: object) -> dict[str, object]:
    return {
        "schema_version": 2,
        "issue": "#7",
        "branch": "feature/1-thing",
        "base": "develop",
        "status": "ready",
        "created_at": "t",
        "updated_at": "t",
        "git": {},
        **extra,
    }


def test_ready_state_reads_the_relay_ref(
    gated: Gated, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    judged: list[tuple[str, str]] = []
    commit = _relay(remote, tmp_path, "feature/1-thing", _payload(recorded_by="human"))
    monkeypatch.setattr(
        trust,
        "require_trusted_commit",
        lambda repo, oid, *, field: judged.append((repo, oid)) or trust.Role.HUMAN,
    )

    ready = REAL_READY_STATE(gated.clone, "o/r", "feature/1-thing")

    assert ready == finalize_watch.ReadyState(trusted=True, issues=("#7",), recorded_by="human")
    assert judged == [("o/r", commit)]
    assert REAL_READY_STATE(gated.clone, "o/r", "feature/2-other") is None


def test_ready_state_with_an_untrusted_author_is_not_parsed(
    gated: Gated, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _relay(remote, tmp_path, "feature/1-thing", "not even a state")

    def untrusted(repo: str, oid: str, *, field: str) -> trust.Role:
        item = trust.Skipped(repo, 0, "commit", "drive-by", "t", field, trust.CROSS_REPOSITORY)
        raise trust.UntrustedContentError(item, "untrusted author")

    monkeypatch.setattr(trust, "require_trusted_commit", untrusted)

    assert REAL_READY_STATE(gated.clone, "o/r", "feature/1-thing") == finalize_watch.ReadyState(
        trusted=False
    )


def test_ready_state_malformed_payload_raises(
    gated: Gated, remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _relay(remote, tmp_path, "feature/1-thing", ["not", "an", "object"])
    monkeypatch.setattr(
        trust, "require_trusted_commit", lambda repo, oid, *, field: trust.Role.HUMAN
    )

    with pytest.raises(TypeError, match="carries no JSON object"):
        REAL_READY_STATE(gated.clone, "o/r", "feature/1-thing")
