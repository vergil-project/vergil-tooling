"""Tests for vergil_tooling.lib.pr_path, the one PR path (epic vergil-project/.github#1664).

GitHub is a fake standing in for the ``github`` reads and writes the path and
the modules it drives make: ``read_json`` (``gh pr view``, for the path's own
read, the watch's snapshot, the provenance check and the auto-merge helper),
``read_json_paginated`` (the PR's reviews and timeline), ``graphql`` (the
enable mutation) and ``merge`` (the direct merge). The trust layer's branch
lookup and body read are faked too, as are the release lock and the PR's arming
ref (:class:`FakeLock`, free by default). The whole-group rule's reads (a PR's
closing references and the live group records) are :class:`GroupRule`, which
answers no closing reference unless a test sets one. The provenance check, the
involvement rule and the merge-method rule run for real; the gates' evaluator is
a spy, and the clones the gates judge in are real git repositories.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from tests.vergil_tooling.group_rule_stubs import GroupRule
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
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.pr_path import (
    ArmResult,
    PRPathError,
    PushResult,
    accepts,
    arm,
    merge_method,
    push_to_open_pr,
)
from vergil_tooling.lib.release_groups.record import Group
from vergil_tooling.lib.release_lock import arming
from vergil_tooling.lib.release_lock import refs as lock_refs
from vergil_tooling.lib.release_lock.model import (
    ARM_CALL_BOUND_SECS,
    ARMING_COLLISION_POLL_SECS,
    ARMING_COLLISION_WAIT_SECS,
    ARMING_EXPIRY_SECS,
    BASE_MOVED_ATTEMPTS,
    LockRecord,
    RefReadError,
    RefWriteError,
)
from vergil_tooling.lib.release_lock.refs import LockState
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

PR = "https://github.com/o/r/pull/7"
HEAD = "0123456789abcdef0123456789abcdef01234567"
#: Another caller's arming ref for PR 7.
OTHER = "f" * 40
OTHER_HEAD = "fedcba9876543210fedcba9876543210fedcba98"
HUMAN = "human"
AGENT = "human-vergil-user"
AUTHORS = TrustedAuthors(human=(HUMAN,), agents=(AGENT,))
CLEAN_STDERR = (
    "GraphQL: Pull request Pull request is in clean status (enablePullRequestAutoMerge)\n"
)
BASE_MOVED_STDERR = (
    "GraphQL: Base branch was modified. Review and try the merge again. (mergePullRequest)\n"
)
BASE_MOVED_ENABLE_STDERR = BASE_MOVED_STDERR.replace(
    "mergePullRequest", "enablePullRequestAutoMerge"
)
NOT_OPEN_STDERR = "GraphQL: Pull request is not open (enablePullRequestAutoMerge)\n"
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
    # The enable and merge calls, in order; a test shares it with the lock's.
    events: list[str] = field(default_factory=list)
    # How many auto-merge state reads see the PR unarmed before another caller arms it.
    other_arms_after: int | None = None
    # Whether the other caller merged the PR directly instead.
    other_merges: bool = False
    state_reads: int = 0
    # How many direct merges GitHub refuses with "Base branch was modified" first.
    base_moved_merges: int = 0
    # How many enable mutations GitHub refuses with "Base branch was modified" first.
    base_moved_enables: int = 0
    # Whether another caller merges the PR at the moment such an enable is refused.
    merged_on_base_moved: bool = False

    def read_json(self, *args: str) -> object:
        assert args[:3] == ("pr", "view", PR), args
        fields = args[args.index("--json") + 1]
        if fields == "state,autoMergeRequest":
            self.events.append("read_auto_merge")
            self.state_reads += 1
            if self.other_arms_after is not None and self.state_reads > self.other_arms_after:
                self.auto_merge = {"mergeMethod": "SQUASH"}
            if self.other_merges:
                self.state = "MERGED"
            return {"state": self.state, "autoMergeRequest": self.auto_merge}
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
        self.events.append("enable")
        self.mutations.append(variables)
        if self.state == "MERGED":
            raise github.GitHubAPIError(
                1, ("gh", "api", "graphql"), output="", stderr=NOT_OPEN_STDERR
            )
        if self.base_moved_enables:
            self.base_moved_enables -= 1
            if self.merged_on_base_moved:
                self.state = "MERGED"
            raise github.GitHubAPIError(
                1, ("gh", "api", "graphql"), output="", stderr=BASE_MOVED_ENABLE_STDERR
            )
        if self.clean:
            raise github.GitHubAPIError(1, ("gh", "api", "graphql"), output="", stderr=CLEAN_STDERR)
        self.auto_merge = {"mergeMethod": variables["method"]}
        return {"enablePullRequestAutoMerge": {"pullRequest": {"number": 7}}}

    def merge(self, pr: str, *, strategy: str, match_head: str | None = None) -> None:
        self.events.append("merge")
        self.merges.append((pr, strategy, match_head))
        if self.base_moved_merges:
            self.base_moved_merges -= 1
            raise github.GitHubAPIError(
                1, ("gh", "pr", "merge"), output="", stderr=BASE_MOVED_STDERR
            )
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


def _record(holder: str = "tok-holder", heartbeat: str = "2026-11-27T10:00:00Z") -> LockRecord:
    return LockRecord(
        holder_identity=HUMAN,
        holder_host="mac",
        version="2.1.400",
        worktree="",
        tracking_issue="",
        stage="quiesce",
        health="running",
        broken_stage="",
        started_at="2026-11-27T09:59:00Z",
        heartbeat_at=heartbeat,
        token=holder,
    )


@dataclass
class FakeLock:
    """The release lock and PR 7's arming ref on origin, and every call made to them.

    A test that reads the order of the handshake shares *events* with
    :class:`FakeGitHub`'s (``fake.events = lock.events``). *on_create*
    runs right after a create lands (a release taking the lock then, say).
    Another caller's ref is a value already in *arming* (:data:`OTHER`), so
    the create collides with it. *on_current* runs before each read of the
    ref (another caller renewing or deleting it), *on_renew* before each
    renewal.
    """

    record: LockRecord | None = None
    unreadable: str = ""
    read_error: str = ""
    arming: dict[int, str] = field(default_factory=dict)
    renew_finds_gone: bool = False
    write_error: str = ""
    renew_error: str = ""
    delete_error: str = ""
    current_error: str = ""
    on_create: Callable[[], None] | None = None
    on_current: Callable[[], None] | None = None
    on_renew: Callable[[], None] | None = None
    events: list[str] = field(default_factory=list)
    serial: int = 0

    def _sha(self) -> str:
        self.serial += 1
        return f"{self.serial:040x}"

    def read_lock(self, repo: str) -> LockState:
        assert repo == "o/r"
        self.events.append("read_lock")
        if self.read_error:
            raise RefReadError(self.read_error)
        if self.unreadable:
            return LockState(sha="a" * 40, record=None, error=self.unreadable)
        if self.record is None:
            return LockState(sha=None, record=None, error="")
        return LockState(sha="b" * 40, record=self.record, error="")

    def create(self, repo: str, pr: int) -> str | None:
        assert (repo, pr) == ("o/r", 7)
        self.events.append("create")
        if self.write_error:
            raise RefWriteError(self.write_error)
        if pr in self.arming:
            return None
        self.arming[pr] = self._sha()
        if self.on_create is not None:
            self.on_create()
        return self.arming[pr]

    def current(self, repo: str, pr: int) -> str | None:
        assert (repo, pr) == ("o/r", 7)
        self.events.append("current")
        if self.current_error:
            raise RefReadError(self.current_error)
        if self.on_current is not None:
            self.on_current()
        return self.arming.get(pr)

    def renew(self, repo: str, pr: int, *, expected: str) -> str | None:
        assert (repo, pr) == ("o/r", 7)
        self.events.append("renew")
        if self.renew_error:
            raise RefWriteError(self.renew_error)
        if self.on_renew is not None:
            self.on_renew()
        if self.renew_finds_gone:
            self.arming.pop(pr, None)
        if self.arming.get(pr) != expected:
            return None
        self.arming[pr] = self._sha()
        return self.arming[pr]

    def delete(self, repo: str, pr: int, *, expected: str) -> bool:
        assert (repo, pr) == ("o/r", 7)
        self.events.append("delete")
        if self.delete_error:
            raise RefWriteError(self.delete_error)
        if pr not in self.arming:
            return True
        if self.arming[pr] != expected:
            return False
        del self.arming[pr]
        return True


@pytest.fixture(autouse=True)
def lock(monkeypatch: pytest.MonkeyPatch) -> FakeLock:
    """A free release lock by default: every arm makes the handshake against this fake."""
    fake_lock = FakeLock()
    monkeypatch.setattr(lock_refs, "read_lock", fake_lock.read_lock)
    for name in ("create", "current", "renew", "delete"):
        monkeypatch.setattr(arming, name, getattr(fake_lock, name))
    return fake_lock


@pytest.fixture(autouse=True)
def group_rule(monkeypatch: pytest.MonkeyPatch) -> GroupRule:
    """The whole-group rule's reads: no closing reference and no group, unless a test sets one."""
    return GroupRule().install(monkeypatch)


ISSUE_900 = IssueRef("o", "r", 900)
ISSUE_901 = IssueRef("o", "r", 901)
PAIR = Group(
    epic=IssueRef("o", "r", 1672),
    group="g-pair",
    rev=1,
    members=(ISSUE_900, ISSUE_901),
    dissolved=False,
    comment_id="100",
)


def _half_of_a_pair(group_rule: GroupRule) -> None:
    """PR 7 closes #900 and not #901, its group's other member: the group would ship in part."""
    group_rule.closes = [("o/r", 900)]
    group_rule.groups = {ISSUE_900: [PAIR], ISSUE_901: [PAIR]}


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

    monkeypatch.setattr(finalize_watch, "gates_at_head", fail)

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


def test_arm_direct_merge_refused_once_for_a_moved_base_merges_on_retry(
    fake: FakeGitHub, gates: Gates, clone: Path, no_wait: list[float]
) -> None:
    fake.clean = True
    fake.base_moved_merges = 1

    result = arm(PR, clone=clone)

    assert result.outcome == "merged"
    assert len(fake.merges) == 2
    assert len(fake.mutations) == 2
    assert no_wait[0] > 0


def test_arm_enable_refused_for_a_moved_base_then_merged_meanwhile_is_a_success(
    fake: FakeGitHub, gates: Gates, clone: Path, no_wait: list[float]
) -> None:
    fake.base_moved_enables = 1
    fake.merged_on_base_moved = True

    result = arm(PR, clone=clone)

    assert (result.outcome, result.head) == ("merged", HEAD)
    # The retry reads the PR as merged at its head: no second enable, no merge.
    assert len(fake.mutations) == 1
    assert fake.merges == []
    assert no_wait[0] > 0


def test_arm_direct_merge_refused_past_the_bound_fails_naming_it(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    fake.clean = True
    fake.base_moved_merges = 100

    with pytest.raises(PRPathError, match="Base branch was modified") as info:
        arm(PR, clone=clone)

    assert f"{BASE_MOVED_ATTEMPTS} times" in str(info.value)
    assert len(fake.merges) == BASE_MOVED_ATTEMPTS
    # The refusal landed nothing, so its arming ref is deleted.
    assert lock.arming == {}


def test_arm_retry_for_a_moved_base_stays_inside_the_arm_call_bound(
    fake: FakeGitHub,
    gates: Gates,
    clone: Path,
    no_wait: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.clean = True
    fake.base_moved_merges = 100
    monkeypatch.setattr(pr_path, "BASE_MOVED_RETRY_SECS", ARM_CALL_BOUND_SECS)

    with pytest.raises(PRPathError, match=f"ran past its bound of {ARM_CALL_BOUND_SECS}s"):
        arm(PR, clone=clone)

    assert len(fake.merges) == 1


def test_describe_state_reads_the_prs_actual_state(fake: FakeGitHub) -> None:
    assert pr_path.describe_state(PR) == "open, auto-merge off"
    fake.auto_merge = {"mergeMethod": "SQUASH"}
    assert pr_path.describe_state(PR) == "open, auto-merge on (SQUASH)"
    fake.state = "MERGED"
    assert pr_path.describe_state(PR) == "merged"


def test_describe_state_names_a_failed_read_never_assumes(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: str) -> object:
        raise subprocess.CalledProcessError(1, ("gh",), output="", stderr="boom")

    monkeypatch.setattr(github, "read_json", broken)

    text = pr_path.describe_state(PR)
    assert "state could not be read" in text
    assert "boom" in text


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
    fake: FakeGitHub,
    gates: Gates,
    clone: Path,
    lock: FakeLock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(_query: str, **_variables: object) -> dict[str, object]:
        raise github.GitHubAPIError(1, ("gh", "api", "graphql"), output="", stderr="forbidden")

    monkeypatch.setattr(github, "graphql", refuse)

    with pytest.raises(PRPathError, match=f"{PR}: enabling auto-merge failed"):
        arm(PR, clone=clone)

    assert lock.events[-1] == "delete"
    assert lock.arming == {}


# --- the whole-group rule (epic vergil-project/.github#1672, spec §5.2) -------------


def test_single_issue_of_group_refused_at_arm(
    fake: FakeGitHub, group_rule: GroupRule, lock: FakeLock, gates: Gates, clone: Path
) -> None:
    _half_of_a_pair(group_rule)

    result = arm(PR, clone=clone)

    assert result.outcome == "refused"
    assert result.detail.startswith("the whole-group rule refuses it: ")
    assert "is missing o/r#901" in result.detail
    # Nothing turns on: no auto-merge enable, no merge, no arming ref, no gate run.
    assert fake.mutations == []
    assert fake.merges == []
    assert lock.events == []
    assert gates.calls == []


def test_arm_applies_rule_without_agent_involvement(
    fake: FakeGitHub, group_rule: GroupRule, lock: FakeLock
) -> None:
    _human_pr(fake)
    _half_of_a_pair(group_rule)

    result = arm(PR)

    assert result.outcome == "refused"
    assert result.detail.startswith("the whole-group rule refuses it: ")
    assert ISSUE_900 in group_rule.group_reads
    assert fake.mutations == []
    assert lock.events == []


def test_arm_reads_github_closing_refs_not_body_only(
    fake: FakeGitHub, group_rule: GroupRule, lock: FakeLock
) -> None:
    _human_pr(fake)
    _half_of_a_pair(group_rule)
    # GitHub lists #900 as closed by the PR, and the body has no Closes line.
    group_rule.body = "## Summary\n\nA fix.\n\nRef #900\n"

    result = arm(PR)

    assert result.outcome == "refused"
    assert "is missing o/r#901" in result.detail
    # The body alone names #900 too: a body reference refuses as well.
    group_rule.closes = []
    group_rule.body = "Fixes o/r#900\n"

    result = arm(PR)

    assert result.outcome == "refused"
    assert "is missing o/r#901" in result.detail
    assert fake.mutations == []
    assert lock.events == []


def test_arm_closing_read_failure_raises_naming_the_pr(
    fake: FakeGitHub, group_rule: GroupRule, lock: FakeLock
) -> None:
    _human_pr(fake)
    group_rule.read_error = trust.TrustReadError(
        "read_pr_closing o/r#7", "its closing references are truncated"
    )

    with pytest.raises(PRPathError, match=f"{PR}: .*reading its closing references or group"):
        arm(PR)
    assert fake.mutations == []
    assert lock.events == []


def test_back_merge_holder_pass_reads_no_group(
    fake: FakeGitHub, group_rule: GroupRule, lock: FakeLock, gates: Gates
) -> None:
    """A back-merge PR closes no issue: under the holder's pass it reads no group record."""
    _human_pr(fake)
    fake.head_ref = "release/post-2.1.9"
    lock.record = _record("tok-holder")

    result = arm(PR, holder_pass="tok-holder")

    assert result == ArmResult(outcome="armed", pr_url=PR, head=HEAD, method="MERGE")
    assert group_rule.group_reads == []
    assert "create" not in lock.events
    assert fake.mutations == [{"id": "PR_kw7", "head": HEAD, "method": "MERGE"}]


# --- the arming gate and the holder's pass (spec §5.4) ----------------------------


def test_arm_lock_free_writes_then_deletes_arming_ref(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    fake.events = lock.events

    result = arm(PR, clone=clone)

    assert result == ArmResult(outcome="armed", pr_url=PR, head=HEAD, method="SQUASH")
    assert lock.events == ["create", "read_lock", "renew", "enable", "delete"]
    assert lock.arming == {}


def test_arm_lock_free_clean_status_merges_inside_the_handshake(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    fake.events = lock.events
    fake.clean = True

    result = arm(PR, clone=clone)

    assert result.outcome == "merged"
    assert lock.events == ["create", "read_lock", "renew", "enable", "merge", "delete"]
    assert lock.arming == {}


def test_arm_lock_held_leaves_pr_unarmed_reports_held(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    fake.events = lock.events
    lock.record = _record()

    result = arm(PR, clone=clone)

    assert result == ArmResult(
        outcome="held",
        pr_url=PR,
        head=HEAD,
        method="SQUASH",
        detail="held for release 2.1.400",
    )
    assert lock.events == ["create", "read_lock", "delete"]
    assert fake.mutations == []
    assert lock.arming == {}


def test_arm_unreadable_lock_is_held_naming_the_read_error(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.unreadable = "refs/vergil/release-lock is unreadable: the lock is not valid JSON"

    result = arm(PR, clone=clone)

    assert result.outcome == "held"
    assert result.detail == (
        "held: the release lock is held but cannot be read "
        "(refs/vergil/release-lock is unreadable: the lock is not valid JSON)"
    )
    assert fake.mutations == []
    assert lock.arming == {}


def test_arm_racing_lock_never_arms_under_lock(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    """Review Focus 2: the lock is taken between ``create`` and ``read_lock``."""
    fake.events = lock.events
    fake.clean = True  # a green PR: arming it would merge it at once

    def release_takes_the_lock() -> None:
        lock.record = _record()

    lock.on_create = release_takes_the_lock

    result = arm(PR, clone=clone)

    assert (result.outcome, result.detail) == ("held", "held for release 2.1.400")
    assert "enable" not in lock.events
    assert fake.mutations == []
    assert fake.merges == []
    assert lock.arming == {}


def test_arm_holder_pass_matching_skips_handshake(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    fake.events = lock.events
    lock.record = _record(holder="tok-holder")

    result = arm(PR, clone=clone, holder_pass="tok-holder")

    assert result.outcome == "armed"
    assert lock.events == ["read_lock", "enable"]
    assert lock.arming == {}


def test_arm_holder_pass_survives_heartbeat_writes(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.record = _record(holder="tok-holder", heartbeat="2026-11-27T10:00:00Z")
    holder_pass = lock.record.token
    # The heartbeat rewrites the lock (a new commit and sha, the same token).
    lock.record = _record(holder="tok-holder", heartbeat="2026-11-27T10:01:00Z")

    result = arm(PR, clone=clone, holder_pass=holder_pass)

    assert result.outcome == "armed"
    assert "create" not in lock.events
    assert fake.mutations == [{"id": "PR_kw7", "head": HEAD, "method": "SQUASH"}]


def test_arm_holder_pass_after_retake_is_ordinary_call(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.record = _record(holder="tok-retaken")

    result = arm(PR, clone=clone, holder_pass="tok-holder")

    assert (result.outcome, result.detail) == ("held", "held for release 2.1.400")
    assert lock.events == ["read_lock", "create", "read_lock", "delete"]
    assert fake.mutations == []


def test_arm_holder_pass_with_lock_free_is_ordinary_call(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    result = arm(PR, clone=clone, holder_pass="tok-holder")

    assert result.outcome == "armed"
    assert lock.events == ["read_lock", "create", "read_lock", "renew", "delete"]


def test_held_unarmed_lock_free_is_not_held(fake: FakeGitHub, lock: FakeLock) -> None:
    assert pr_path.held_unarmed(PR) == ""


def test_held_unarmed_lock_held_and_pr_unarmed(fake: FakeGitHub, lock: FakeLock) -> None:
    lock.record = _record()

    assert pr_path.held_unarmed(PR) == "held for release 2.1.400"


def test_held_unarmed_pr_still_armed_is_not_held(fake: FakeGitHub, lock: FakeLock) -> None:
    lock.record = _record()
    fake.auto_merge = {"mergeMethod": "SQUASH"}

    assert pr_path.held_unarmed(PR) == ""


def test_held_unarmed_lock_read_failure_raises(fake: FakeGitHub, lock: FakeLock) -> None:
    lock.read_error = "git ls-remote failed"

    with pytest.raises(PRPathError, match="reading the release lock failed: git ls-remote failed"):
        pr_path.held_unarmed(PR)


def test_arm_create_collision_waits_for_other_caller(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    fake.events = lock.events
    lock.arming = {7: OTHER}
    fake.other_arms_after = 2

    result = arm(PR, clone=clone)

    assert result == ArmResult(outcome="armed", pr_url=PR, head=HEAD, method="SQUASH")
    assert lock.events == [
        "create",
        "read_auto_merge",
        "current",
        "read_auto_merge",
        "current",
        "read_auto_merge",
    ]
    assert fake.mutations == []
    assert no_wait[0] == pytest.approx(2 * ARMING_COLLISION_POLL_SECS)
    assert lock.arming == {7: OTHER}


def test_arm_create_collision_with_merged_pr_is_merged(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    lock.arming = {7: OTHER}
    fake.other_merges = True

    result = arm(PR, clone=clone)

    assert result == ArmResult(outcome="merged", pr_url=PR, head=HEAD, method="SQUASH")
    assert fake.mutations == []
    assert fake.merges == []


def test_arm_create_collision_with_closed_pr_raises(
    fake: FakeGitHub,
    gates: Gates,
    clone: Path,
    lock: FakeLock,
    no_wait: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock.arming = {7: OTHER}
    snapshot = fake.read_json

    def read_json(*args: str) -> object:
        if args[args.index("--json") + 1] == "state,autoMergeRequest":
            fake.state = "CLOSED"  # closed while the other caller was arming it
        return snapshot(*args)

    monkeypatch.setattr(github, "read_json", read_json)

    with pytest.raises(pr_path.ArmingCollisionError, match="and it is closed"):
        arm(PR, clone=clone)

    assert lock.arming == {7: OTHER}
    assert fake.mutations == []


def _churn(lock: FakeLock) -> None:
    """Another caller rewrites PR 7's arming ref before every read of it."""
    lock.arming[7] = lock._sha()  # noqa: SLF001


def test_arm_create_collision_still_unarmed_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    lock.arming = {7: OTHER}
    lock.on_current = lambda: _churn(lock)  # a live caller: never stale

    with pytest.raises(pr_path.ArmingCollisionError, match="another caller is arming it") as info:
        arm(PR, clone=clone)

    assert isinstance(info.value, PRPathError)
    assert f"still unarmed after {ARMING_COLLISION_WAIT_SECS}s" in str(info.value)
    assert no_wait[0] == pytest.approx(ARMING_COLLISION_WAIT_SECS)
    assert fake.mutations == []
    assert "renew" not in lock.events


# --- a stale arming ref outside a release (vergil-tooling#5324) -------------------


def test_arm_takes_over_a_stale_arming_ref_and_arms(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    """A ref left by a crashed caller, or an arm call stopped at its bound."""
    fake.events = lock.events
    lock.arming = {7: OTHER}

    result = arm(PR, clone=clone)

    assert result == ArmResult(outcome="armed", pr_url=PR, head=HEAD, method="SQUASH")
    # Replaced only once seen unchanged for the full expiry, never sooner.
    assert no_wait[0] == pytest.approx(ARMING_EXPIRY_SECS)
    tail = lock.events[lock.events.index("renew") - 1 :]
    assert tail == ["read_lock", "renew", "read_lock", "renew", "enable", "delete"]
    polls = ARMING_EXPIRY_SECS // ARMING_COLLISION_POLL_SECS + 1
    assert lock.events.count("read_auto_merge") == polls
    assert fake.mutations == [{"id": "PR_kw7", "head": HEAD, "method": "SQUASH"}]
    assert lock.arming == {}


def test_arm_stale_ref_change_restarts_the_clock(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    lock.arming = {7: OTHER}
    reads = [0]

    def renew_once_at_the_third_read() -> None:
        reads[0] += 1
        if reads[0] == 3:
            _churn(lock)

    lock.on_current = renew_once_at_the_third_read

    result = arm(PR, clone=clone)

    assert result.outcome == "armed"
    # The new value was first seen at the third poll, so expires a full expiry later.
    assert no_wait[0] == pytest.approx(2 * ARMING_COLLISION_POLL_SECS + ARMING_EXPIRY_SECS)


def test_arm_stale_ref_with_lock_held_is_held_and_left(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    lock.arming = {7: OTHER}
    reads = [0]

    def release_takes_the_lock() -> None:
        reads[0] += 1
        if reads[0] == 2:
            lock.record = _record()

    lock.on_current = release_takes_the_lock

    result = arm(PR, clone=clone)

    assert (result.outcome, result.detail) == ("held", "held for release 2.1.400")
    assert "renew" not in lock.events
    assert lock.arming == {7: OTHER}  # the release's wait expires it
    assert fake.mutations == []


def test_arm_stale_ref_changed_at_the_swap_is_not_replaced(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    lock.arming = {7: OTHER}
    lock.on_renew = lambda: _churn(lock)  # renewed by its writer as the swap goes out

    with pytest.raises(pr_path.ArmingCollisionError, match="still unarmed"):
        arm(PR, clone=clone)

    assert lock.events.count("renew") == 1
    assert lock.arming[7] != OTHER
    assert fake.mutations == []


def test_arm_collision_ref_gone_creates_its_own_and_arms(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    """The other caller ended without arming (held, refused or failed) and deleted its ref."""
    fake.events = lock.events
    lock.arming = {7: OTHER}

    def other_caller_deletes_its_ref() -> None:
        lock.arming.pop(7, None)

    lock.on_current = other_caller_deletes_its_ref

    result = arm(PR, clone=clone)

    assert result.outcome == "armed"
    assert lock.events == [
        "create",
        "read_auto_merge",
        "current",
        "create",
        "read_lock",
        "renew",
        "enable",
        "delete",
    ]
    assert lock.arming == {}
    assert no_wait[0] == 0


def test_arm_collision_ref_gone_then_a_third_caller_wins_the_create(
    fake: FakeGitHub,
    gates: Gates,
    clone: Path,
    lock: FakeLock,
    no_wait: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock.arming = {7: OTHER}
    fake.other_arms_after = 2  # the third caller arms it
    creates = [0]
    real_create = lock.create

    def create(repo: str, pr: int) -> str | None:
        creates[0] += 1
        if creates[0] == 2:
            lock.arming[pr] = "e" * 40  # the third caller's create lands first
        return real_create(repo, pr)

    def other_caller_deletes_its_ref() -> None:
        if lock.arming.get(7) == OTHER:
            del lock.arming[7]

    monkeypatch.setattr(arming, "create", create)
    lock.on_current = other_caller_deletes_its_ref

    result = arm(PR, clone=clone)

    assert result.outcome == "armed"
    assert creates[0] == 2
    assert "renew" not in lock.events
    assert fake.mutations == []


def test_arm_collision_ref_read_failure_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    lock.arming = {7: OTHER}
    lock.current_error = "git ls-remote failed"

    with pytest.raises(PRPathError, match="reading its arming ref failed: git ls-remote failed"):
        arm(PR, clone=clone)


def test_arm_stale_ref_swap_failure_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock, no_wait: list[float]
) -> None:
    lock.arming = {7: OTHER}
    lock.renew_error = "git push failed"

    with pytest.raises(PRPathError, match="replacing its stale arming ref failed: git push failed"):
        arm(PR, clone=clone)

    assert lock.arming == {7: OTHER}


def test_arm_renew_finds_ref_gone_aborts(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.renew_finds_gone = True

    result = arm(PR, clone=clone)

    assert (result.outcome, result.detail) == ("refused", "arming ref expired")
    assert fake.mutations == []
    assert "delete" not in lock.events


def test_arm_judges_before_arming_ref_created(
    fake: FakeGitHub,
    gates: Gates,
    clone: Path,
    lock: FakeLock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every judgment runs before ``arming.create``; no fetch or gate run while the ref exists."""
    fake.events = lock.events
    check_pr, read_issue, evaluate = pr_provenance.check_pr, trust.read_issue, gates.evaluate

    def spy(name: str, fn: Callable[..., object]) -> Callable[..., object]:
        def wrapped(*args: object, **kwargs: object) -> object:
            lock.events.append(name)
            return fn(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(pr_provenance, "check_pr", spy("provenance", check_pr))
    monkeypatch.setattr(trust, "read_issue", spy("method", read_issue))
    monkeypatch.setattr(finalize_watch, "_fetch_for_gates", spy("fetch", lambda *_a: "base-sha"))
    monkeypatch.setattr(agent_gates, "evaluate", spy("gates", evaluate))

    result = arm(PR, clone=clone)

    assert result.outcome == "armed"
    assert lock.events == [
        "provenance",
        "method",
        "fetch",
        "gates",
        "create",
        "read_lock",
        "renew",
        "enable",
        "delete",
    ]


def test_arm_refused_by_a_judgment_writes_no_arming_ref(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    gates.ok = False

    result = arm(PR, clone=clone)

    assert result.outcome == "refused"
    assert lock.events == []


def test_arm_call_past_bound_stops_without_retry(
    fake: FakeGitHub,
    gates: Gates,
    clone: Path,
    lock: FakeLock,
    no_wait: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The enable, retries included, is bounded by ARM_CALL_BOUND_SECS (spec §5.4)."""
    attempts: list[float] = []

    def slow_enable(_query: str, **_variables: object) -> dict[str, object]:
        def attempt() -> dict[str, object]:
            attempts.append(no_wait[0])
            no_wait[0] += ARM_CALL_BOUND_SECS - 1
            raise subprocess.CalledProcessError(1, ("gh",), output="", stderr="HTTP 502")

        return retry.call_with_retry(attempt)

    monkeypatch.setattr(github, "graphql", slow_enable)

    with pytest.raises(PRPathError, match=f"ran past its bound of {ARM_CALL_BOUND_SECS}s") as info:
        arm(PR, clone=clone)

    assert len(attempts) == 1
    assert "not retried" in str(info.value)
    # The arming ref is left for the release's expiry: the stopped call may still land.
    assert 7 in lock.arming


def test_arm_arming_ref_write_failure_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.write_error = "git push failed: HTTP 500"

    with pytest.raises(PRPathError, match=r"writing its arming ref failed: git push failed"):
        arm(PR, clone=clone)

    assert fake.mutations == []


def test_arm_lock_read_failure_deletes_arming_ref_and_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.read_error = "git ls-remote failed"

    with pytest.raises(PRPathError, match=r"reading the release lock failed: git ls-remote failed"):
        arm(PR, clone=clone)

    assert fake.mutations == []
    assert lock.arming == {}


def test_arm_holder_pass_lock_read_failure_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.read_error = "git ls-remote failed"

    with pytest.raises(PRPathError, match=r"reading the release lock failed"):
        arm(PR, clone=clone, holder_pass="tok-holder")

    assert lock.events == ["read_lock"]


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


def test_single_issue_of_group_refused_at_push(
    fake: FakeGitHub, group_rule: GroupRule, gates: Gates, push: Pushes
) -> None:
    _half_of_a_pair(group_rule)

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert not result.pushed
    assert result.pr_url == PR
    assert result.detail.startswith("the whole-group rule refuses it: ")
    assert "is missing o/r#901" in result.detail
    assert result.detail.endswith("; it was not pushed")
    assert push.calls == []
    assert gates.calls == []


def test_push_applies_rule_without_agent_involvement(
    fake: FakeGitHub, group_rule: GroupRule, pusher: Pusher, push: Pushes
) -> None:
    _human_pr(fake)
    pusher.agent = False
    _half_of_a_pair(group_rule)

    result = push_to_open_pr(BRANCH, head=OTHER_HEAD, lease=None)

    assert not result.pushed
    assert "is missing o/r#901" in result.detail
    assert ISSUE_900 in group_rule.group_reads
    assert push.calls == []


def test_push_closing_read_failure_raises_naming_the_pr(
    fake: FakeGitHub, group_rule: GroupRule, push: Pushes
) -> None:
    group_rule.read_error = subprocess.CalledProcessError(
        1, ("gh", "api", "graphql"), output="", stderr="HTTP 502: Bad Gateway"
    )

    with pytest.raises(PRPathError, match=f"{PR}: .*reading its closing references or group"):
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


def test_arm_renew_failure_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.renew_error = "git push failed"

    with pytest.raises(PRPathError, match="renewing its arming ref failed: git push failed"):
        arm(PR, clone=clone)

    assert fake.mutations == []


def test_arm_delete_failure_after_arming_raises(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.delete_error = "git push failed"

    with pytest.raises(PRPathError, match="deleting its arming ref failed: git push failed"):
        arm(PR, clone=clone)


def test_arm_lock_read_and_delete_failures_both_named(
    fake: FakeGitHub, gates: Gates, clone: Path, lock: FakeLock
) -> None:
    lock.read_error = "git ls-remote failed"
    lock.delete_error = "git push failed"

    with pytest.raises(PRPathError) as info:
        arm(PR, clone=clone)

    assert "reading the release lock failed: git ls-remote failed" in str(info.value)
    assert "; then " in str(info.value)
    assert "deleting its arming ref failed: git push failed" in str(info.value)


def test_arm_collision_state_read_failure_raises(
    fake: FakeGitHub,
    gates: Gates,
    clone: Path,
    lock: FakeLock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock.arming = {7: OTHER}
    snapshot = fake.read_json

    def read_json(*args: str) -> object:
        if args[args.index("--json") + 1] == "state,autoMergeRequest":
            raise github.GitHubAPIError(1, ("gh",), output="", stderr="HTTP 403")
        return snapshot(*args)

    monkeypatch.setattr(github, "read_json", read_json)

    with pytest.raises(PRPathError, match="reading its auto-merge state failed"):
        arm(PR, clone=clone)


def test_arm_collision_state_read_not_an_object_raises(
    fake: FakeGitHub,
    gates: Gates,
    clone: Path,
    lock: FakeLock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock.arming = {7: OTHER}
    snapshot = fake.read_json

    def read_json(*args: str) -> object:
        if args[args.index("--json") + 1] == "state,autoMergeRequest":
            return []
        return snapshot(*args)

    monkeypatch.setattr(github, "read_json", read_json)

    with pytest.raises(PRPathError, match=r"returned \[\] for its auto-merge state"):
        arm(PR, clone=clone)


def test_held_unarmed_auto_merge_read_failure_raises(
    fake: FakeGitHub, lock: FakeLock, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock.record = _record()

    def refuse(*_args: str) -> object:
        raise github.GitHubAPIError(1, ("gh",), output="", stderr="HTTP 403")

    monkeypatch.setattr(github, "read_json", refuse)

    with pytest.raises(PRPathError, match="reading auto-merge failed"):
        pr_path.held_unarmed(PR)
