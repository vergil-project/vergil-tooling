"""Tests for the batch rules: the hard rules and the commit-per-issue invariant.

GitHub reads are faked at the ``epics`` boundary (one in-memory issue world);
the trusted authors are faked at the loader. The commit checks run real git on
a temporary repository.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.pr_workflow import batch_rules
from vergil_tooling.lib.pr_workflow.batch_rules import (
    MAX_BATCH,
    MIN_BATCH,
    BatchRuleError,
    check_commit_per_issue,
    check_hard_rules,
    is_batch,
)
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.trusted_authors import TrustedAuthors, TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from pathlib import Path

REPO = "acme/tools"
EPIC = IssueRef("acme", ".github", 7)
OTHER_EPIC = IssueRef("acme", ".github", 8)
INTAKE_EPIC = IssueRef("acme", ".github", 2)
AGENT = "acme-vergil-user"
HUMAN = "alice"


def _ref(number: int) -> IssueRef:
    return IssueRef("acme", "tools", number)


@dataclass
class Issue:
    labels: set[str] = field(default_factory=set)
    native_parent: IssueRef | None = EPIC
    reflink_parent: IssueRef | None = None
    author: str = f"app/{AGENT}"
    blockers: list[IssueRef] = field(default_factory=list)
    state: str = "OPEN"


class FakeGitHub:
    """An in-memory issue world behind the ``epics`` readers the rules use."""

    def __init__(self) -> None:
        self.issues: dict[IssueRef, Issue] = {
            EPIC: Issue(labels={"epic"}, native_parent=None),
            OTHER_EPIC: Issue(labels={"epic"}, native_parent=None),
            INTAKE_EPIC: Issue(labels={"epic", "intake"}, native_parent=None),
        }
        self.failing: set[str] = set()

    def _get(self, what: str, ref: IssueRef) -> Issue:
        if what in self.failing:
            raise subprocess.CalledProcessError(1, ["gh", what], stderr=f"{what} read failed")
        return self.issues[ref]

    def labels(self, ref: IssueRef) -> set[str]:
        return self._get("labels", ref).labels

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(epics, "_labels", self.labels)
        monkeypatch.setattr(
            epics, "native_parent_of", lambda ref: self._get("parent", ref).native_parent
        )

        def parent_of(ref: IssueRef) -> IssueRef | None:
            issue = self._get("parent", ref)
            return issue.native_parent or issue.reflink_parent

        monkeypatch.setattr(epics, "parent_of", parent_of)
        monkeypatch.setattr(epics, "issue_author", lambda ref: self._get("author", ref).author)
        monkeypatch.setattr(epics, "blockers_of", lambda ref: self._get("blockers", ref).blockers)
        monkeypatch.setattr(epics, "issue_state", lambda ref: self._get("state", ref).state)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    for number in range(10, 20):
        fake.issues[_ref(number)] = Issue()
    fake.install(monkeypatch)
    monkeypatch.setattr(
        batch_rules,
        "load_trusted_authors",
        lambda: TrustedAuthors(human=(HUMAN,), agents=(AGENT,)),
    )
    return fake


def _refused(issues: list[str], rule: str) -> str:
    with pytest.raises(BatchRuleError) as info:
        check_hard_rules(issues, repo=REPO)
    assert info.value.rule == rule
    return str(info.value)


# -- the hard rules --------------------------------------------------------------


def test_batch_rule_error_is_a_workflow_error() -> None:
    assert issubclass(BatchRuleError, WorkflowError)
    assert (MIN_BATCH, MAX_BATCH) == (2, 8)


def test_is_batch() -> None:
    assert not is_batch(["#10"])
    assert is_batch(["#10", "#11"])


def test_hard_rules_accept_two_same_epic_tasks(world: FakeGitHub) -> None:
    check_hard_rules(["#10", "#11"], repo=REPO)


def test_hard_rules_accept_eight_and_a_human_author(world: FakeGitHub) -> None:
    world.issues[_ref(12)].author = HUMAN
    check_hard_rules([f"#{n}" for n in range(10, 18)], repo=REPO)


def test_hard_rules_accept_the_full_ref_of_this_repo(world: FakeGitHub) -> None:
    check_hard_rules(["#10", "acme/tools#11"], repo=REPO)


def test_hard_rules_refuse_one_issue(world: FakeGitHub) -> None:
    message = _refused(["#10"], "batch size")
    assert "2 to 8 issues, got 1" in message


def test_hard_rules_refuse_nine_issues(world: FakeGitHub) -> None:
    message = _refused([f"#{n}" for n in range(10, 19)], "batch size")
    assert "got 9" in message


def test_hard_rules_refuse_duplicate(world: FakeGitHub) -> None:
    message = _refused(["#10", "#11", "#10"], "no duplicate")
    assert "#10 is listed more than once" in message


def test_hard_rules_refuse_duplicate_spelled_two_ways(world: FakeGitHub) -> None:
    message = _refused(["#10", "acme/tools#10"], "no duplicate")
    assert "acme/tools#10 is listed more than once (first as #10)" in message


def test_hard_rules_refuse_mixed_parents(world: FakeGitHub) -> None:
    world.issues[_ref(11)].native_parent = OTHER_EPIC
    message = _refused(["#10", "#11"], "same epic")
    assert "#11 is under acme/.github#8" in message
    assert "#10 is under acme/.github#7" in message


def test_hard_rules_refuse_queue_parent(world: FakeGitHub) -> None:
    # A reflink-only parent that is a queue epic: the native-parent queue
    # check cannot see it, so the shared-parent rule must.
    for number in (10, 11):
        world.issues[_ref(number)].native_parent = None
        world.issues[_ref(number)].reflink_parent = INTAKE_EPIC
    message = _refused(["#10", "#11"], "same epic")
    assert "acme/.github#2 is the intake queue epic" in message


def test_hard_rules_refuse_parent_that_is_not_an_epic(world: FakeGitHub) -> None:
    archive = IssueRef("acme", ".github", 9)
    world.issues[archive] = Issue(labels={"archive", "ad-hoc"}, native_parent=None)
    for number in (10, 11):
        world.issues[_ref(number)].native_parent = archive
    message = _refused(["#10", "#11"], "same epic")
    assert "acme/.github#9 is not an epic" in message


def test_hard_rules_refuse_unparented(world: FakeGitHub) -> None:
    world.issues[_ref(11)].native_parent = None
    message = _refused(["#10", "#11"], "same epic")
    assert "#11 has no parent epic" in message


def test_hard_rules_refuse_other_repo(world: FakeGitHub) -> None:
    message = _refused(["#10", "acme/other#11"], "same repo")
    assert "acme/other#11 is in acme/other" in message


def test_hard_rules_refuse_malformed_ref(world: FakeGitHub) -> None:
    message = _refused(["#10", "11"], "issue ref")
    assert "'11'" in message


def test_hard_rules_refuse_operational_issue(world: FakeGitHub) -> None:
    world.issues[_ref(11)].labels = {"validation"}
    message = _refused(["#10", "#11"], "not operational")
    assert "#11 is an operational task" in message


@pytest.mark.parametrize("kind", ["epic", "archive"])
def test_hard_rules_refuse_non_task_issue(world: FakeGitHub, kind: str) -> None:
    world.issues[_ref(11)].labels = {kind}
    message = _refused(["#10", "#11"], "task")
    assert epics.non_task_refusal_message("--issue", "#11", kind) in message


def test_hard_rules_refuse_queue_item(world: FakeGitHub) -> None:
    world.issues[_ref(10)].native_parent = INTAKE_EPIC
    message = _refused(["#10", "#11"], "not queued")
    assert epics.queue_refusal_message("#10", "intake") in message


def test_hard_rules_refuse_untrusted_author(world: FakeGitHub) -> None:
    world.issues[_ref(11)].author = "mallory"
    message = _refused(["#10", "#11"], "trusted author")
    assert "#11 was opened by mallory" in message


def test_hard_rules_refuse_an_app_prefixed_untrusted_author(world: FakeGitHub) -> None:
    world.issues[_ref(11)].author = "app/mallory-bot"
    message = _refused(["#10", "#11"], "trusted author")
    assert "app/mallory-bot" in message


@pytest.mark.parametrize(
    "error",
    [
        TrustedAuthorsNotConfiguredError("trusted authors are not configured"),
        ValueError("trusted_authors: 'human' is empty"),
    ],
)
def test_hard_rules_refuse_when_trusted_authors_fails(
    world: FakeGitHub, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail() -> TrustedAuthors:
        raise error

    monkeypatch.setattr(batch_rules, "load_trusted_authors", fail)
    message = _refused(["#10", "#11"], "trusted author")
    assert "could not read the trusted authors" in message
    assert str(error) in message


@pytest.mark.parametrize(
    ("what", "rule", "named"),
    [
        ("labels", "task", "the labels of #10"),
        ("parent", "not queued", "the parent of #10"),
        ("author", "trusted author", "the author of #10"),
        ("blockers", "no open deployment blocker", "the Blocked-by lines of #10"),
    ],
)
def test_hard_rules_refuse_on_read_failure(
    world: FakeGitHub, what: str, rule: str, named: str
) -> None:
    world.failing.add(what)
    message = _refused(["#10", "#11"], rule)
    assert f"could not read {named}" in message
    assert f"{what} read failed" in message


def test_hard_rules_refuse_on_a_blocker_state_read_failure(world: FakeGitHub) -> None:
    deploy = _ref(30)
    world.issues[deploy] = Issue(labels={"deployment"})
    world.issues[_ref(11)].blockers = [deploy]
    world.failing.add("state")
    message = _refused(["#10", "#11"], "no open deployment blocker")
    assert "could not read the state of #11's blocker acme/tools#30" in message


def test_hard_rules_refuse_on_a_graphql_error(
    world: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(ref: IssueRef) -> IssueRef | None:
        raise RuntimeError("GraphQL errors: boom")

    monkeypatch.setattr(epics, "parent_of", fail)
    message = _refused(["#10", "#11"], "same epic")
    assert "could not read the parent of #10: GraphQL errors: boom" in message


def test_hard_rules_let_a_missing_token_reach_the_cli(
    world: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(ref: IssueRef) -> set[str]:
        raise github.MissingGitHubTokenError(1, ["gh"], stderr="GH_TOKEN environment variable")

    monkeypatch.setattr(epics, "_labels", fail)
    with pytest.raises(github.MissingGitHubTokenError):
        check_hard_rules(["#10", "#11"], repo=REPO)


def test_hard_rules_refuse_open_deployment_blocker(world: FakeGitHub) -> None:
    deploy = IssueRef("acme", "other", 30)
    world.issues[deploy] = Issue(labels={"deployment"})
    world.issues[_ref(11)].blockers = [deploy]
    message = _refused(["#10", "#11"], "no open deployment blocker")
    assert "#11 is Blocked-by acme/other#30, an open deployment task" in message


def test_hard_rules_accept_closed_deployment_blocker(world: FakeGitHub) -> None:
    deploy = IssueRef("acme", "other", 30)
    world.issues[deploy] = Issue(labels={"deployment"}, state="CLOSED")
    world.issues[_ref(11)].blockers = [deploy]
    check_hard_rules(["#10", "#11"], repo=REPO)


def test_hard_rules_accept_open_non_deployment_blocker(world: FakeGitHub) -> None:
    task = IssueRef("acme", "other", 31)
    world.issues[task] = Issue()
    world.issues[_ref(11)].blockers = [task]
    check_hard_rules(["#10", "#11"], repo=REPO)


# -- the commit-per-issue invariant ----------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        [
            "git",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            *args,
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, subject: str) -> str:
    path = repo / "log.txt"
    with path.open("a") as handle:
        handle.write(f"{subject}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    _commit(path, "base")
    _git(path, "checkout", "-q", "-b", "feature/10-batch")
    monkeypatch.chdir(path)
    return path


def _commit_refused(issues: list[str]) -> str:
    with pytest.raises(BatchRuleError) as info:
        check_commit_per_issue(issues, base="develop", head="HEAD")
    return str(info.value)


def test_commits_accept_one_per_issue_in_order(repo: Path) -> None:
    _commit(repo, "fix(a): first (#10)")
    _commit(repo, "feat(b): second (#11)")
    _commit(repo, "docs: third (#12)")
    check_commit_per_issue(["#10", "#11", "acme/tools#12"], base="develop", head="HEAD")


def test_commits_refuse_out_of_order(repo: Path) -> None:
    sha = _commit(repo, "feat(b): second (#11)")
    _commit(repo, "fix(a): first (#10)")
    message = _commit_refused(["#10", "#11"])
    assert sha[:12] in message
    assert "carries (#11), out of the listed order" in message
    assert "it should end in (#10) for #10" in message


@pytest.mark.parametrize(
    ("subject", "found"),
    [
        ("feat(b): second", "carries no issue ref"),
        ("feat(b): second (#99)", "carries (#99), an issue not in the batch"),
        ("feat(b): second (#11) and more", "carries no issue ref"),
        ("feat(b): second(#11)", "carries no issue ref"),
    ],
)
def test_commits_refuse_missing_or_foreign_ref(repo: Path, subject: str, found: str) -> None:
    _commit(repo, "fix(a): first (#10)")
    sha = _commit(repo, subject)
    message = _commit_refused(["#10", "#11"])
    assert sha[:12] in message
    assert found in message
    assert "it should end in (#11) for #11" in message


def test_commits_refuse_merge_commit(repo: Path) -> None:
    _commit(repo, "fix(a): first (#10)")
    _git(repo, "checkout", "-q", "develop")
    _commit(repo, "moved develop")
    _git(repo, "checkout", "-q", "feature/10-batch")
    _git(repo, "merge", "-q", "--no-edit", "develop")
    merge = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(BatchRuleError) as info:
        check_commit_per_issue(["#10", "#11"], base="develop~1", head="HEAD")
    assert info.value.rule == "no merge commits"
    assert merge[:12] in str(info.value)
    assert "rebase it onto develop~1" in str(info.value)


def test_commits_refuse_extra_commit(repo: Path) -> None:
    _commit(repo, "fix(a): first (#10)")
    _commit(repo, "feat(b): second (#11)")
    extra = _commit(repo, "feat(b): more (#11)")
    message = _commit_refused(["#10", "#11"])
    assert "need 2 commits since develop, found 3" in message
    assert extra[:12] in message


def test_commits_refuse_missing_commit(repo: Path) -> None:
    _commit(repo, "fix(a): first (#10)")
    message = _commit_refused(["#10", "#11"])
    assert "need 2 commits since develop, found 1" in message


def test_commits_refuse_fixup_left(repo: Path) -> None:
    _commit(repo, "fix(a): first (#10)")
    fixup = _commit(repo, "fixup! fix(a): first (#10)")
    message = _commit_refused(["#10", "#11"])
    assert fixup[:12] in message
    assert "unfolded fixup" in message
    assert "vrg-git rebase -i --autosquash develop" in message


def test_commits_refuse_an_unknown_base(repo: Path) -> None:
    message = _commit_refused_at(["#10", "#11"], base="no-such-ref")
    assert "could not read the commits no-such-ref..HEAD" in message


@pytest.mark.parametrize("base", ["", "--all"])
def test_commits_refuse_an_option_as_a_revision(repo: Path, base: str) -> None:
    message = _commit_refused_at(["#10", "#11"], base=base)
    assert "not a git revision" in message


def _commit_refused_at(issues: list[str], *, base: str) -> str:
    with pytest.raises(BatchRuleError) as info:
        check_commit_per_issue(issues, base=base, head="HEAD")
    return str(info.value)
