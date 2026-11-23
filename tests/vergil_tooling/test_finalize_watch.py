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
import re
import subprocess
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from vergil_tooling.lib import auto_merge, finalize_watch, github, pr_provenance, progress, trust
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
    clock = SimpleNamespace(monotonic=api.monotonic, sleep=api.sleep)
    monkeypatch.setattr(finalize_watch, "time", clock)
    return api


def run(prs: list[str], *, poll: float = 15.0) -> list[PRResult]:
    return watch(prs, poll_interval=poll)


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

    [result] = watch([url(1)], cancel_grace=60)

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

    [result] = watch([url(1)], cancel_grace=1)

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
