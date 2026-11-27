"""Tests for vergil_tooling.lib.pr_path, the one PR path (epic vergil-project/.github#1664).

GitHub is a fake standing in for the ``github`` reads and writes the path and
the modules it drives make: ``read_json`` (``gh pr view``, for the path's own
read, the watch's snapshot, the provenance check and the auto-merge helper),
``read_json_paginated`` (the PR's reviews and timeline), ``graphql`` (the
enable mutation) and ``merge`` (the direct merge). The trust layer's branch
lookup and body read are faked too. The provenance check, the involvement rule
and the merge-method rule run for real; the gates' evaluator is a spy, and the
clones the gates judge in are real git repositories.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import (
    agent_gates,
    finalize_watch,
    github,
    identity_mode,
    pr_path,
    pr_provenance,
    retry,
    trust,
)
from vergil_tooling.lib.pr_path import (
    ArmResult,
    PRPathError,
    PushResult,
    accepts,
    arm,
    merge_method,
    push_to_open_pr,
)
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from pathlib import Path

PR = "https://github.com/o/r/pull/7"
HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
HUMAN = "human"
AGENT = "human-vergil-user"
AUTHORS = TrustedAuthors(human=(HUMAN,), agents=(AGENT,))
CLEAN_STDERR = (
    "GraphQL: Pull request Pull request is in clean status (enablePullRequestAutoMerge)\n"
)
BATCH_BODY = "## Summary\n\nTwo fixes.\n\nCloses #1\nCloses #2\n"


def _not_found() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1, ("gh", "pr", "view", PR), output="", stderr="GraphQL: Could not resolve to a PullRequest"
    )


@dataclass
class FakeGitHub:
    """The PR as GitHub holds it, and every write made to it."""

    head: str = HEAD
    head_ref: str = "feature/7-fix"
    base: str = "develop"
    state: str = "OPEN"
    draft: bool = False
    labels: tuple[str, ...] = ()
    author: str = AGENT
    author_is_bot: bool = True
    cross_repo: bool = False
    # Whether the trust layer confirms the PR (a same-repo PR with a trusted
    # author on its branch).
    confirmed: bool = True
    body: str = "## Summary\n\nA fix.\n\nRef #5\n"
    timeline: list[dict[str, object]] = field(default_factory=list)
    not_found_reads: int = 0
    clean: bool = False
    auto_merge: dict[str, object] | None = None
    mutations: list[dict[str, object]] = field(default_factory=list)
    merges: list[tuple[str, str, str | None]] = field(default_factory=list)

    def read_json(self, *args: str) -> object:
        assert args[:3] == ("pr", "view", PR), args
        fields = args[args.index("--json") + 1]
        if fields == "isDraft":
            if self.not_found_reads:
                self.not_found_reads -= 1
                raise _not_found()
            return {"isDraft": self.draft}
        if fields == finalize_watch._VIEW_FIELDS:  # noqa: SLF001 - the watch's snapshot read
            return {
                "id": "PR_kw7",
                "url": PR,
                "title": "fix: a fix",
                "state": self.state,
                "baseRefName": self.base,
                "headRefName": self.head_ref,
                "headRefOid": self.head,
                "autoMergeRequest": self.auto_merge,
                "mergeStateStatus": "CLEAN" if self.clean else "BLOCKED",
                "statusCheckRollup": [],
                "mergeCommit": None,
                "labels": [{"name": name} for name in self.labels],
            }
        if fields == pr_provenance._VIEW_FIELDS:  # noqa: SLF001 - the provenance check's read
            return {
                "author": {"login": self.author, "is_bot": self.author_is_bot},
                "isCrossRepository": self.cross_repo,
                "headRepositoryOwner": {"login": "fork" if self.cross_repo else "o"},
                "headRepository": {"name": "r"},
            }
        if fields == "number,url":
            return {"number": 7, "url": PR}
        if fields == "id":
            return {"id": "PR_kw7"}
        if fields == "state":
            return {"state": self.state}
        if fields == "state,headRefOid":
            return {"state": self.state, "headRefOid": self.head}
        assert fields == "autoMergeRequest", fields
        return {"autoMergeRequest": self.auto_merge}

    def read_json_paginated(self, path: str) -> list[dict[str, object]]:
        if path.endswith("/reviews"):
            return []
        assert path.endswith("/timeline"), path
        return self.timeline

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        assert "enablePullRequestAutoMerge" in query
        self.mutations.append(variables)
        if self.clean:
            raise github.GitHubAPIError(1, ("gh", "api", "graphql"), output="", stderr=CLEAN_STDERR)
        self.auto_merge = {"mergeMethod": variables["method"]}
        return {"enablePullRequestAutoMerge": {"pullRequest": {"number": 7}}}

    def merge(self, pr: str, *, strategy: str, match_head: str | None = None) -> None:
        self.merges.append((pr, strategy, match_head))
        self.state = "MERGED"

    def prs_for_branch(
        self, repo: str, branch: str, *, state: str
    ) -> tuple[list[trust.PRRef], list[trust.Skipped]]:
        assert (repo, branch) == ("o/r", self.head_ref)
        assert state in ("all", "open"), state
        if not self.confirmed or (state == "open" and self.state != "OPEN"):
            return [], []
        ref = trust.PRRef(
            number=7,
            url=PR,
            title="fix: a fix",
            head=self.head_ref,
            head_oid=self.head,
            base=self.base,
            author_role=trust.Role.AGENTS,
            state=self.state,
        )
        return [ref], []

    def read_issue(self, repo: str, number: int, *, require_body: bool) -> trust.IssueContent:
        assert (repo, number, require_body) == ("o/r", 7, True)
        return trust.IssueContent(
            body=self.body, body_role=trust.Role.AGENTS, author_role=None, comments=(), skipped=()
        )


@dataclass
class Gates:
    """Every call of the gates' evaluator, and what it answers."""

    ok: bool = True
    calls: list[tuple[Path, str, str | None]] = field(default_factory=list)

    def evaluate(
        self, repo: Path, *, base: str, head: str, issue_refs: list[str], authors: TrustedAuthors
    ) -> agent_gates.GateResult:
        assert (base, issue_refs, authors) == ("base-sha", ["o/r#5"], AUTHORS)
        origin = subprocess.run(
            ("git", "-C", str(repo), "remote", "get-url", "origin"),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.calls.append((repo, head, github.github_remote_repo(origin)))
        if self.ok:
            return agent_gates.GateResult(ok=True, reasons=())
        return agent_gates.GateResult(ok=False, reasons=("merge review: no record covers it",))


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    api = FakeGitHub()
    for name in ("read_json", "read_json_paginated", "graphql", "merge"):
        monkeypatch.setattr(github, name, getattr(api, name))
    monkeypatch.setattr(trust, "prs_for_branch", api.prs_for_branch)
    monkeypatch.setattr(trust, "read_issue", api.read_issue)
    monkeypatch.setattr(pr_provenance, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(finalize_watch, "load_trusted_authors", lambda: AUTHORS)
    return api


@pytest.fixture
def gates(monkeypatch: pytest.MonkeyPatch) -> Gates:
    spy = Gates()
    monkeypatch.setattr(agent_gates, "evaluate", spy.evaluate)
    monkeypatch.setattr(finalize_watch, "ready_state", lambda _clone, _repo, _branch: None)
    monkeypatch.setattr(finalize_watch, "task_issue_refs", lambda _repo, _n, _ready: ["o/r#5"])
    monkeypatch.setattr(finalize_watch, "_fetch_for_gates", lambda _clone, _head: "base-sha")
    return spy


def _repo_with_origin(where: Path, url: str) -> Path:
    where.mkdir()
    subprocess.run(("git", "init", "--quiet", str(where)), check=True)
    subprocess.run(("git", "-C", str(where), "remote", "add", "origin", url), check=True)
    return where


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    return _repo_with_origin(tmp_path / "clone", "git@github.com:o/r.git")


def _human_pr(fake: FakeGitHub) -> None:
    fake.author, fake.author_is_bot = HUMAN, False


# --- the merge method ---------------------------------------------------------------


def test_method_merge_for_trusted_post_release_head(fake: FakeGitHub) -> None:
    _human_pr(fake)
    fake.head_ref = "release/post-2.1.9"
    assert merge_method(PR) == "MERGE"


def test_method_merge_for_trusted_dependency_update_pr(fake: FakeGitHub) -> None:
    fake.labels = ("dependency-update",)
    assert merge_method(PR) == "MERGE"


def test_method_rebase_for_batch_pr(fake: FakeGitHub) -> None:
    fake.body = BATCH_BODY
    assert merge_method(PR) == "REBASE"


def test_method_squash_otherwise(fake: FakeGitHub) -> None:
    assert merge_method(PR) == "SQUASH"


def test_method_squash_for_one_closes_line(fake: FakeGitHub) -> None:
    fake.body = "Closes #1\n"
    assert merge_method(PR) == "SQUASH"


def test_method_refuses_post_release_head_from_untrusted_author(fake: FakeGitHub) -> None:
    fake.head_ref = "release/post-2.1.9"
    fake.confirmed = False
    with pytest.raises(PRPathError, match=r"release/post-2\.1\.9 is a release's back-merge branch"):
        merge_method(PR)


def test_method_refuses_dependency_update_pr_from_untrusted_author(fake: FakeGitHub) -> None:
    fake.labels = ("dependency-update",)
    fake.confirmed = False
    with pytest.raises(PRPathError, match="carries the dependency-update label"):
        merge_method(PR)


def test_method_refuses_an_untrusted_body(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def untrusted(*_args: object, **_kwargs: object) -> trust.IssueContent:
        skip = trust.Skipped("o/r", 7, "pr", "outsider", "now", "body", trust.UNTRUSTED)
        raise trust.UntrustedContentError(skip, "the pull request body is untrusted")

    monkeypatch.setattr(trust, "read_issue", untrusted)
    with pytest.raises(PRPathError, match="its merge method cannot be worked out"):
        merge_method(PR)


# --- what the path accepts ----------------------------------------------------------


def test_accepts_a_trusted_same_repo_pr(fake: FakeGitHub) -> None:
    assert accepts(PR) is None


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"confirmed": False}, "not a same-repo PR with a trusted author"),
        ({"draft": True}, "it is a draft"),
        ({"base": "main"}, "its base is main, not develop"),
        ({"state": "CLOSED"}, "it is closed, not open"),
    ],
)
def test_accepts_says_why_not(fake: FakeGitHub, change: dict[str, object], reason: str) -> None:
    for name, value in change.items():
        setattr(fake, name, value)
    assert reason in (accepts(PR) or "")


# --- arming -------------------------------------------------------------------------


def test_arm_refuses_fork_pr(fake: FakeGitHub, gates: Gates, clone: Path) -> None:
    fake.cross_repo = True
    fake.confirmed = False  # the trust layer never confirms a fork PR

    result = arm(PR, clone=clone)

    assert result.outcome == "refused"
    assert "not a same-repo PR with a trusted author" in result.detail
    assert fake.mutations == []
    assert fake.merges == []
    assert gates.calls == []


def test_arm_refuses_draft_pr(fake: FakeGitHub, gates: Gates, clone: Path) -> None:
    fake.draft = True

    result = arm(PR, clone=clone)

    assert result.outcome == "refused"
    assert "it is a draft" in result.detail
    assert fake.mutations == []
    assert gates.calls == []


def test_arm_refuses_failing_gates(fake: FakeGitHub, gates: Gates, clone: Path) -> None:
    gates.ok = False

    result = arm(PR, clone=clone)

    assert result == ArmResult(
        outcome="refused",
        pr_url=PR,
        head=HEAD,
        method="SQUASH",
        detail=f"the gates refused its head {HEAD[:12]}: merge review: no record covers it",
    )
    assert fake.mutations == []
    assert fake.merges == []


def test_arm_refuses_a_provenance_violation(fake: FakeGitHub, gates: Gates, clone: Path) -> None:
    fake.timeline = [{"event": "renamed", "actor": {"login": AGENT, "type": "Bot"}}]

    result = arm(PR, clone=clone)

    assert result.outcome == "refused"
    assert result.detail.startswith("provenance violations found")
    assert fake.mutations == []
    assert gates.calls == []


def _untrusted_body(*_args: object, **_kwargs: object) -> trust.IssueContent:
    skip = trust.Skipped("o/r", 7, "pr", "outsider", "now", "body", trust.UNTRUSTED)
    raise trust.UntrustedContentError(skip, "the pull request body is untrusted")


def test_arm_refuses_a_method_it_cannot_work_out(
    fake: FakeGitHub, gates: Gates, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trust, "read_issue", _untrusted_body)

    result = arm(PR, clone=clone)

    assert result.outcome == "refused"
    assert result.method is None
    assert "its merge method cannot be worked out" in result.detail
    assert fake.mutations == []
    assert gates.calls == []


def test_arm_refuses_an_untrusted_deps_pr_before_its_method(fake: FakeGitHub, gates: Gates) -> None:
    fake.labels = ("dependency-update",)
    fake.confirmed = False

    result = arm(PR)

    assert result.outcome == "refused"
    assert "not a same-repo PR with a trusted author" in result.detail
    assert fake.mutations == []


def test_arm_refuses_a_pr_url_with_no_repo_and_number(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = fake.read_json

    def odd_url(*args: str) -> object:
        data = real(*args)
        if isinstance(data, dict) and "headRefOid" in data and "title" in data:
            return {**data, "url": "https://example.com/not-a-pr"}
        return data

    monkeypatch.setattr(github, "read_json", odd_url)

    with pytest.raises(PRPathError, match="cannot read the repository and number"):
        arm(PR)


def test_arm_refuses_a_view_with_no_draft_flag(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "read_json", lambda *_args: {"isDraft": None})

    with pytest.raises(PRPathError, match=f"{PR}: gh pr view returned no isDraft"):
        arm(PR)


def test_arm_read_failure_raises_naming_the_pr(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: str) -> object:
        raise subprocess.CalledProcessError(1, ("gh",), output="", stderr="HTTP 502: Bad Gateway")

    monkeypatch.setattr(github, "read_json", fail)

    with pytest.raises(PRPathError, match=f"(?s){PR}: reading the PR failed: .*502"):
        arm(PR)


def test_arm_provenance_check_that_cannot_run_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: str) -> list[dict[str, object]]:
        raise ValueError("a malformed timeline page")

    monkeypatch.setattr(github, "read_json_paginated", fail)

    with pytest.raises(PRPathError, match=f"{PR}: the provenance check could not run: a malformed"):
        arm(PR, clone=clone)
    assert fake.mutations == []


def test_arm_fresh_clone_that_cannot_be_made_raises(
    fake: FakeGitHub, gates: Gates, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: str) -> str:
        raise subprocess.CalledProcessError(1, ("git", *args), output="", stderr="no space")

    monkeypatch.setattr(pr_path.git, "read_output", fail)

    with pytest.raises(PRPathError, match="could not make a fresh clone of o/r for the gates"):
        arm(PR)
    assert fake.mutations == []


def test_arm_gates_recheck_error_raises(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object) -> str:
        raise finalize_watch.FinalizeWatchError(f"{PR}: the gates cannot be re-checked")

    monkeypatch.setattr(finalize_watch, "recheck_gates", fail)

    with pytest.raises(PRPathError, match="the gates cannot be re-checked"):
        arm(PR, clone=clone)
    assert fake.mutations == []


def test_arm_is_pinned_to_current_head(fake: FakeGitHub, gates: Gates, clone: Path) -> None:
    fake.head = OTHER_HEAD

    result = arm(PR, clone=clone)

    assert result == ArmResult(outcome="armed", pr_url=PR, head=OTHER_HEAD, method="SQUASH")
    assert fake.mutations == [{"id": "PR_kw7", "head": OTHER_HEAD, "method": "SQUASH"}]
    assert gates.calls == [(clone, OTHER_HEAD, "o/r")]


def test_arm_refuses_a_head_other_than_the_expected_one(
    fake: FakeGitHub, gates: Gates, clone: Path
) -> None:
    fake.head = OTHER_HEAD

    result = arm(PR, clone=clone, head=HEAD)

    assert result.outcome == "refused"
    assert result.detail == f"its head is {OTHER_HEAD[:12]}, not the expected {HEAD[:12]}"
    assert fake.mutations == []


def test_arm_batch_pr_with_rebase(fake: FakeGitHub, gates: Gates, clone: Path) -> None:
    fake.body = BATCH_BODY

    result = arm(PR, clone=clone)

    assert (result.outcome, result.method) == ("armed", "REBASE")
    assert fake.mutations == [{"id": "PR_kw7", "head": HEAD, "method": "REBASE"}]


def test_arm_clean_status_merges_directly_at_head(
    fake: FakeGitHub, gates: Gates, clone: Path
) -> None:
    fake.clean = True
    fake.body = BATCH_BODY

    result = arm(PR, clone=clone)

    assert result == ArmResult(outcome="merged", pr_url=PR, head=HEAD, method="REBASE")
    assert fake.merges == [(PR, "rebase", HEAD)]


def test_arm_back_merge_pr_passes_without_task_issue(
    fake: FakeGitHub, gates: Gates, monkeypatch: pytest.MonkeyPatch
) -> None:
    _human_pr(fake)
    fake.head_ref = "release/post-2.1.9"
    monkeypatch.setattr(finalize_watch, "task_issue_refs", lambda *_a: [])

    result = arm(PR)

    assert result == ArmResult(outcome="armed", pr_url=PR, head=HEAD, method="MERGE")
    assert gates.calls == []
    assert fake.mutations == [{"id": "PR_kw7", "head": HEAD, "method": "MERGE"}]


def test_arm_deps_pr_passes_without_task_issue(
    fake: FakeGitHub, gates: Gates, monkeypatch: pytest.MonkeyPatch
) -> None:
    _human_pr(fake)
    fake.labels = ("dependency-update",)
    monkeypatch.setattr(finalize_watch, "task_issue_refs", lambda *_a: [])

    result = arm(PR)

    assert (result.outcome, result.method) == ("armed", "MERGE")
    assert gates.calls == []


def test_arm_agent_pr_runs_gates(fake: FakeGitHub, gates: Gates, clone: Path) -> None:
    result = arm(PR, clone=clone)

    assert result == ArmResult(outcome="armed", pr_url=PR, head=HEAD, method="SQUASH")
    assert gates.calls == [(clone, HEAD, "o/r")]


def test_arm_agent_armed_deps_pr_not_gated(fake: FakeGitHub, gates: Gates) -> None:
    _human_pr(fake)
    fake.labels = ("dependency-update",)
    fake.timeline = [
        {
            "event": "auto_merge_enabled",
            "actor": {"login": AGENT, "type": "Bot"},
            "created_at": "2026-11-20T10:00:00Z",
        },
        {
            "event": "auto_merge_disabled",
            "actor": {"login": HUMAN, "type": "User"},
            "created_at": "2026-11-21T10:00:00Z",
        },
    ]

    result = arm(PR)

    assert (result.outcome, result.method) == ("armed", "MERGE")
    assert gates.calls == []


def test_arm_from_foreign_clone_judges_in_fresh_fetch(
    fake: FakeGitHub, gates: Gates, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = _repo_with_origin(tmp_path / "foreign", "git@github.com:o/other.git")
    monkeypatch.chdir(foreign)

    result = arm(PR)

    assert result.outcome == "armed"
    [(judged_in, head, origin)] = gates.calls
    assert judged_in != foreign
    assert head == HEAD
    assert origin == "o/r"
    assert not judged_in.exists()  # the path's own clone is removed afterwards


def test_arm_refuses_clone_of_wrong_repo(fake: FakeGitHub, gates: Gates, tmp_path: Path) -> None:
    other = _repo_with_origin(tmp_path / "other", "git@github.com:o/other.git")

    with pytest.raises(PRPathError, match="whose origin is o/other, not the PR's repository o/r"):
        arm(PR, clone=other)

    assert fake.mutations == []
    assert gates.calls == []


@pytest.fixture
def no_wait(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make the known-resource retry's sleeps instant, on a clock they advance."""
    now = [0.0]
    monkeypatch.setattr(retry.time, "sleep", lambda secs: now.__setitem__(0, now[0] + secs))
    monkeypatch.setattr(retry.time, "monotonic", lambda: now[0])
    return now


def test_arm_retries_brief_404_on_new_pr(
    fake: FakeGitHub, gates: Gates, clone: Path, no_wait: list[float]
) -> None:
    fake.not_found_reads = 2

    result = arm(PR, clone=clone)

    assert result.outcome == "armed"
    assert no_wait[0] > 0


def test_arm_404_past_the_retry_bound_raises_naming_the_pr(
    fake: FakeGitHub, gates: Gates, clone: Path, no_wait: list[float]
) -> None:
    fake.not_found_reads = 10_000

    with pytest.raises(PRPathError, match=f"PR {PR} was reported by GitHub but still returns 404"):
        arm(PR, clone=clone)

    assert no_wait[0] == pytest.approx(retry.KNOWN_RESOURCE_BUDGET_SECS)
    assert fake.mutations == []


def test_arm_enable_failure_raises_naming_the_pr(
    fake: FakeGitHub, gates: Gates, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_query: str, **_variables: object) -> dict[str, object]:
        raise github.GitHubAPIError(1, ("gh", "api", "graphql"), output="", stderr="forbidden")

    monkeypatch.setattr(github, "graphql", refuse)

    with pytest.raises(PRPathError, match=f"{PR}: enabling auto-merge failed"):
        arm(PR, clone=clone)


def test_held_is_an_outcome_for_the_arming_gate() -> None:
    assert "held" in pr_path.ArmOutcome.__args__


# --- push mode for an open PR's branch ----------------------------------------------

BRANCH = "feature/7-fix"


@dataclass
class Pushes:
    """Every ``git push`` the path runs, and whether it fails."""

    calls: list[tuple[str, ...]] = field(default_factory=list)
    fail: bool = False

    def run(self, *args: str) -> None:
        self.calls.append(args)
        if self.fail:
            raise subprocess.CalledProcessError(
                1, ("git", *args), output="", stderr="! [rejected] (stale info)"
            )


@dataclass
class Pusher:
    """Whether the identity running the push is an agent."""

    agent: bool = True


@pytest.fixture
def pusher(monkeypatch: pytest.MonkeyPatch) -> Pusher:
    who = Pusher()
    monkeypatch.setattr(identity_mode, "any_agent_signal", lambda: who.agent)
    return who


@pytest.fixture
def push(monkeypatch: pytest.MonkeyPatch, clone: Path, gates: Gates, pusher: Pusher) -> Pushes:
    """Run the push mode from *clone*, with its pushes recorded and nothing fetched."""
    spy = Pushes()
    monkeypatch.setattr(pr_path.git, "run", spy.run)
    monkeypatch.setattr(finalize_watch, "_fetch_develop_for_gates", lambda _c, _h: "base-sha")
    monkeypatch.chdir(clone)
    return spy


def test_push_mode_refuses_head_failing_gates(
    fake: FakeGitHub, gates: Gates, push: Pushes, clone: Path
) -> None:
    gates.ok = False

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert result == PushResult(
        pushed=False,
        pr_url=PR,
        detail=(
            f"the gates refused the head {OTHER_HEAD[:12]}: merge review: no record covers it; "
            "it was not pushed"
        ),
    )
    assert push.calls == []
    assert gates.calls == [(clone.resolve(), OTHER_HEAD, "o/r")]


def test_push_mode_pushes_passing_head(
    fake: FakeGitHub, gates: Gates, push: Pushes, clone: Path
) -> None:
    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert result.pushed
    assert result.pr_url == PR
    assert push.calls == [("push", "origin", f"{OTHER_HEAD}:refs/heads/{BRANCH}")]
    assert gates.calls == [(clone.resolve(), OTHER_HEAD, "o/r")]


def test_push_mode_rebased_head_uses_lease(fake: FakeGitHub, gates: Gates, push: Pushes) -> None:
    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=HEAD)

    assert result.pushed
    assert push.calls == [
        (
            "push",
            f"--force-with-lease={BRANCH}:{HEAD}",
            "origin",
            f"{OTHER_HEAD}:refs/heads/{BRANCH}",
        )
    ]


@pytest.mark.parametrize(("name", "value"), [("base", "main"), ("state", "CLOSED")])
def test_push_mode_refuses_branch_with_no_open_develop_pr(
    fake: FakeGitHub, gates: Gates, push: Pushes, name: str, value: str
) -> None:
    setattr(fake, name, value)

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert result == PushResult(
        pushed=False,
        pr_url="",
        detail=(
            f"{BRANCH} has no open PR into develop; push mode pushes only to an open PR's branch"
        ),
    )
    assert push.calls == []
    assert gates.calls == []


@pytest.mark.parametrize("passes", [True, False])
def test_push_mode_leaves_auto_merge_on(
    fake: FakeGitHub, gates: Gates, push: Pushes, *, passes: bool
) -> None:
    fake.auto_merge = {"mergeMethod": "SQUASH"}
    gates.ok = passes

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert result.pushed is passes
    assert fake.mutations == []  # neither an enable nor a disable
    assert fake.auto_merge == {"mergeMethod": "SQUASH"}


def test_push_mode_gates_a_human_pr_an_agent_pushes_to(
    fake: FakeGitHub, gates: Gates, push: Pushes, pusher: Pusher
) -> None:
    _human_pr(fake)
    pusher.agent = True

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert result.pushed
    assert [head for _repo, head, _origin in gates.calls] == [OTHER_HEAD]


def test_push_mode_human_push_to_a_human_pr_is_not_gated(
    fake: FakeGitHub, gates: Gates, push: Pushes, pusher: Pusher
) -> None:
    _human_pr(fake)
    pusher.agent = False

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert result.pushed
    assert gates.calls == []


def test_push_mode_refuses_a_provenance_violation(
    fake: FakeGitHub, gates: Gates, push: Pushes
) -> None:
    fake.timeline = [{"event": "renamed", "actor": {"login": AGENT, "type": "Bot"}}]

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert not result.pushed
    assert result.detail.startswith("provenance violations found")
    assert push.calls == []
    assert gates.calls == []


def test_push_mode_refuses_a_pr_the_path_does_not_accept(
    fake: FakeGitHub, gates: Gates, push: Pushes
) -> None:
    fake.draft = True

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert not result.pushed
    assert result.detail == "the one PR path does not accept it: it is a draft"
    assert push.calls == []


def test_push_mode_failed_push_raises_naming_the_pr(
    fake: FakeGitHub, gates: Gates, push: Pushes
) -> None:
    push.fail = True

    with pytest.raises(
        PRPathError, match=f"(?s){PR}: pushing {OTHER_HEAD[:12]} to {BRANCH}.*stale"
    ):
        push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=HEAD)


def test_push_mode_retries_brief_404(
    fake: FakeGitHub, gates: Gates, push: Pushes, no_wait: list[float]
) -> None:
    fake.not_found_reads = 2

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert result.pushed
    assert no_wait[0] > 0


def test_push_mode_gates_judge_the_local_head_without_fetching_it(
    fake: FakeGitHub, gates: Gates, push: Pushes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_fetch(*_args: object) -> str:
        raise AssertionError("a head not yet pushed is never fetched")

    monkeypatch.setattr(finalize_watch, "_fetch_for_gates", no_fetch)

    assert push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None).pushed


def test_push_mode_outside_a_clone_raises(
    fake: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.chdir(plain)

    with pytest.raises(PRPathError, match="cannot read the current clone and its origin"):
        push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)


def test_push_mode_from_a_clone_with_no_github_origin_raises(
    fake: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = _repo_with_origin(tmp_path / "local", str(tmp_path / "bare.git"))
    monkeypatch.chdir(local)

    with pytest.raises(PRPathError, match="is not a GitHub repository"):
        push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)


def test_push_mode_failed_pr_lookup_raises(
    fake: FakeGitHub, push: Pushes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise trust.TrustReadError("prs_for_branch o/r feature/7-fix", "HTTP 502")

    monkeypatch.setattr(trust, "prs_for_branch", fail)

    with pytest.raises(PRPathError, match=f"reading the open PRs on {BRANCH} failed: .*502"):
        push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)
    assert push.calls == []


def test_push_mode_refuses_several_open_develop_prs(
    fake: FakeGitHub, gates: Gates, push: Pushes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def two(*_args: object, **_kwargs: object) -> tuple[list[trust.PRRef], list[trust.Skipped]]:
        refs = [
            trust.PRRef(
                number=n,
                url=f"https://github.com/o/r/pull/{n}",
                title="t",
                head=BRANCH,
                head_oid=HEAD,
                base="develop",
                author_role=trust.Role.AGENTS,
                state="OPEN",
            )
            for n in (7, 8)
        ]
        return refs, []

    monkeypatch.setattr(trust, "prs_for_branch", two)

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert result == PushResult(
        pushed=False, pr_url="", detail=f"{BRANCH} has 2 open PRs into develop (#7, #8)"
    )
    assert push.calls == []
    assert gates.calls == []


def test_push_mode_refuses_a_pr_whose_branch_has_changed(
    fake: FakeGitHub, gates: Gates, push: Pushes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(finalize_watch, "confirmed_head", lambda _snap: "feature/8-other")

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert not result.pushed
    assert result.detail == f"its head branch is feature/8-other, not {BRANCH}"
    assert push.calls == []


def test_push_mode_gates_that_cannot_run_raise(
    fake: FakeGitHub, push: Pushes, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> str:
        raise finalize_watch.FinalizeWatchError(f"{PR}: the gates cannot be re-checked")

    monkeypatch.setattr(finalize_watch, "gates_before_push", fail)

    with pytest.raises(PRPathError, match="the gates cannot be re-checked"):
        push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)
    assert push.calls == []


# --- the open-PR lookup behind the tooling's other pushes -------------------------


def test_open_develop_pr_url_names_the_open_pr(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(clone)

    assert pr_path.open_develop_pr_url(BRANCH) == PR


@pytest.mark.parametrize(("name", "value"), [("base", "main"), ("state", "CLOSED")])
def test_open_develop_pr_url_none_without_an_open_develop_pr(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    setattr(fake, name, value)
    monkeypatch.chdir(clone)

    assert pr_path.open_develop_pr_url(BRANCH) is None


def test_open_develop_pr_url_lookup_failure_raises(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise trust.TrustReadError("prs_for_branch o/r feature/7-fix", "HTTP 502")

    monkeypatch.setattr(trust, "prs_for_branch", fail)
    monkeypatch.chdir(clone)

    with pytest.raises(PRPathError, match=f"reading the open PRs on {BRANCH} failed: .*502"):
        pr_path.open_develop_pr_url(BRANCH)


def test_open_develop_pr_url_several_open_prs_raise(
    fake: FakeGitHub, clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def two(*_args: object, **_kwargs: object) -> tuple[list[trust.PRRef], list[trust.Skipped]]:
        refs = [
            trust.PRRef(
                number=n,
                url=f"https://github.com/o/r/pull/{n}",
                title="t",
                head=BRANCH,
                head_oid=HEAD,
                base="develop",
                author_role=trust.Role.AGENTS,
                state="OPEN",
            )
            for n in (7, 8)
        ]
        return refs, []

    monkeypatch.setattr(trust, "prs_for_branch", two)
    monkeypatch.chdir(clone)

    with pytest.raises(PRPathError, match=r"has 2 open PRs into develop \(#7, #8\)"):
        pr_path.open_develop_pr_url(BRANCH)


def test_open_develop_pr_url_outside_a_github_clone_raises(
    fake: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = _repo_with_origin(tmp_path / "local", str(tmp_path / "bare.git"))
    monkeypatch.chdir(local)

    with pytest.raises(PRPathError, match="is not a GitHub repository"):
        pr_path.open_develop_pr_url(BRANCH)
