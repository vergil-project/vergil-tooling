"""Tests for the release-group rules and the holder check.

Epic vergil-project/.github#1672, phase 3 Task 10 (spec §5.1, §5.5). The GitHub reads the
rules make are stubbed at their module boundaries: the epic's kind and its tasks' parents
(``epics``), the trust layer's body read, the proxy links (``epic_audit``, ``proxies``), the
title read (``github``), and the open-PR, relay, local and batch-marker reads (``in_flight``,
``batch_marker``). :class:`World` answers each from a table, so a test names only what it
changes; a failure is named by its reader and raised from it.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import epic_audit, epics, github, in_flight, proxies, trust
from vergil_tooling.lib.epics import BlockerState, IssueRef, TaskEpic
from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.pr_workflow.batch_rules import MAX_BATCH
from vergil_tooling.lib.release_groups.rules import (
    GroupRuleReadError,
    Holder,
    RuleBreak,
    check_group_rules,
    dependency_paths,
    holders,
)

if TYPE_CHECKING:
    from pathlib import Path

GROUP = "G1"
EPIC = IssueRef("org", "repo", 40)
PROXY = IssueRef("org", "repo", 60)
OTHER_EPIC = IssueRef("org", "repo", 50)
PRIMARY = IssueRef("dev", "lib", 9)
OUTSIDE = IssueRef("org", "repo", 30)
ELSEWHERE = IssueRef("other", "lib", 9)
DOC_BODY = "Plan task: doc-review\n\nSweep the docs."


def ref(number: int) -> IssueRef:
    """A task of the test epic's repo."""
    return IssueRef("org", "repo", number)


def _rules(breaks: list[RuleBreak]) -> set[str]:
    return {b.rule for b in breaks}


@dataclass
class World:
    """The tracker the rules read: a table row per answer, and a failure per named reader."""

    kinds: dict[IssueRef, tuple[IssueRef, str]] = field(
        default_factory=lambda: {
            EPIC: (EPIC, epics.DEVELOPMENT_KIND),
            PRIMARY: (PRIMARY, epics.DEVELOPMENT_KIND),
        }
    )
    parents: dict[IssueRef, IssueRef | None] = field(default_factory=dict)
    states: dict[IssueRef, str] = field(default_factory=dict)
    non_tasks: dict[IssueRef, str] = field(default_factory=dict)
    operational: dict[IssueRef, str] = field(default_factory=dict)
    titles: dict[IssueRef, str] = field(default_factory=dict)
    bodies: dict[IssueRef, str] = field(default_factory=dict)
    blockers: dict[IssueRef, list[IssueRef]] = field(default_factory=dict)
    native: dict[IssueRef, list[BlockerState]] = field(default_factory=dict)
    proxy_problems: list[str] = field(default_factory=list)
    primary_error: Exception | None = None
    raw_title: object | None = None
    open_prs: dict[int, frozenset[int]] = field(default_factory=dict)
    relays: dict[str, frozenset[int]] = field(default_factory=dict)
    local: dict[str, frozenset[int]] = field(default_factory=dict)
    marker: batch_marker.Marker | None = None
    failures: dict[str, Exception] = field(default_factory=dict)

    def under(self, epic: IssueRef, *tasks: IssueRef) -> None:
        """Link *tasks* under *epic* as their native parent."""
        for task in tasks:
            self.parents[task] = epic

    def _fail(self, reader: str) -> None:
        if reader in self.failures:
            raise self.failures[reader]

    def epic_kind(self, epic: IssueRef) -> tuple[IssueRef, str]:
        self._fail("epic_kind")
        return self.kinds.get(epic, (epic, epics.NOT_EPIC_KIND))

    def task_epic(self, task: IssueRef) -> TaskEpic:
        self._fail("task_epic")
        head = self.parents.get(task, EPIC)
        return TaskEpic(task, head, epics.DEVELOPMENT_KIND, head)

    def issue_state(self, issue: IssueRef) -> str:
        self._fail("issue_state")
        return self.states.get(issue, "OPEN")

    def non_task_kind(self, issue: IssueRef) -> str | None:
        self._fail("non_task_kind")
        return self.non_tasks.get(issue)

    def operational_kind(self, issue: IssueRef) -> str | None:
        self._fail("operational_kind")
        return self.operational.get(issue)

    def blockers_of(self, task: IssueRef) -> list[IssueRef]:
        self._fail("blockers_of")
        return list(self.blockers.get(task, []))

    def native_blockers_of(self, task: IssueRef) -> tuple[BlockerState, ...]:
        self._fail("native_blockers_of")
        return tuple(self.native.get(task, []))

    def proxy_link_problems(self, epic: IssueRef) -> list[str]:
        self._fail("proxy_link_problems")
        return list(self.proxy_problems)

    def primary_of(self, proxy: IssueRef) -> IssueRef:
        self._fail("primary_of")
        if self.primary_error is not None:
            raise self.primary_error
        return PRIMARY

    def read_issue(self, repo: str, number: int, **_: object) -> trust.IssueContent:
        self._fail("read_issue")
        owner, name = repo.split("/")
        body = self.bodies.get(IssueRef(owner, name, number), "")
        return trust.IssueContent(
            body=body, body_role=None, author_role=None, comments=(), skipped=()
        )

    def read_json(self, *args: str, **_: object) -> object:
        """The title read: ``gh issue view <N> --repo <owner/name> --json title``."""
        self._fail("read_json")
        if self.raw_title is not None:
            return self.raw_title
        owner, name = args[4].split("/")
        return {"title": self.titles.get(IssueRef(owner, name, int(args[2])), "")}

    def open_pr_marks(self, repo: str) -> dict[int, frozenset[int]]:
        self._fail("open_pr_marks")
        return dict(self.open_prs)

    def ready_relays(self, repo: str, clone: Path) -> dict[str, frozenset[int]]:
        self._fail("ready_relays")
        return dict(self.relays)

    def ready_local_branches(self, repo: str, clone: Path) -> dict[str, frozenset[int]]:
        self._fail("ready_local_branches")
        return dict(self.local)

    def read_marker(self, root: Path, issue: str) -> batch_marker.Marker | None:
        self._fail("read_marker")
        return self.marker


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    stub = World()
    monkeypatch.setattr(epics, "epic_kind", stub.epic_kind)
    monkeypatch.setattr(epics, "task_epic", stub.task_epic)
    monkeypatch.setattr(epics, "issue_state", stub.issue_state)
    monkeypatch.setattr(epics, "non_task_kind", stub.non_task_kind)
    monkeypatch.setattr(epics, "operational_kind", stub.operational_kind)
    monkeypatch.setattr(epics, "blockers_of", stub.blockers_of)
    monkeypatch.setattr(epics, "native_blockers_of", stub.native_blockers_of)
    monkeypatch.setattr(epic_audit, "proxy_link_problems", stub.proxy_link_problems)
    monkeypatch.setattr(proxies, "primary_of", stub.primary_of)
    monkeypatch.setattr(trust, "read_issue", stub.read_issue)
    monkeypatch.setattr(github, "read_json", stub.read_json)
    monkeypatch.setattr(in_flight, "open_pr_marks", stub.open_pr_marks)
    monkeypatch.setattr(in_flight, "ready_relays", stub.ready_relays)
    monkeypatch.setattr(in_flight, "ready_local_branches", stub.ready_local_branches)
    monkeypatch.setattr(batch_marker, "read_marker", stub.read_marker)
    return stub


def _untrusted() -> trust.UntrustedContentError:
    item = trust.Skipped(
        repo="org/repo",
        number=2,
        kind="issue",
        login="drive-by",
        at="2026-11-01T09:30:00Z",
        field="body",
        cause=trust.UNTRUSTED,
    )
    return trust.UntrustedContentError(item, "the body is untrusted")


# --- a valid group, and the rules, each refused once ------------------------------------


def test_valid_group_has_no_breaks(world: World) -> None:
    assert check_group_rules(EPIC, GROUP, [ref(1), ref(2)]) == []


def test_one_member_group_valid(world: World) -> None:
    assert check_group_rules(EPIC, GROUP, [ref(1)]) == []


def test_rule_open_code_task_refused(world: World) -> None:
    world.states[ref(2)] = "CLOSED"
    breaks = check_group_rules(EPIC, GROUP, [ref(1), ref(2)])
    assert _rules(breaks) == {"open code task"}
    assert breaks[0].group == GROUP
    assert "org/repo#2" in breaks[0].detail


def test_rule_open_code_task_refused_for_an_epic(world: World) -> None:
    world.non_tasks[ref(2)] = "epic"
    assert _rules(check_group_rules(EPIC, GROUP, [ref(1), ref(2)])) == {"open code task"}


def test_rule_one_repo_refused(world: World) -> None:
    other = IssueRef("org", "other", 2)
    assert _rules(check_group_rules(EPIC, GROUP, [ref(1), other])) == {"one repo"}


def test_rule_not_operational_refused(world: World) -> None:
    world.operational[ref(2)] = "validation"
    assert _rules(check_group_rules(EPIC, GROUP, [ref(1), ref(2)])) == {"not operational"}


def test_rule_not_a_documentation_review_refused(world: World) -> None:
    world.titles[ref(2)] = "Documentation review: batch-release-unit"
    assert _rules(check_group_rules(EPIC, GROUP, [ref(1), ref(2)])) == {
        "not a documentation review"
    }


def test_documentation_review_by_body_refused(world: World) -> None:
    world.bodies[ref(2)] = DOC_BODY
    assert _rules(check_group_rules(EPIC, GROUP, [ref(1), ref(2)])) == {
        "not a documentation review"
    }


def test_rule_dependency_path_refused(world: World) -> None:
    world.blockers[ref(1)] = [OUTSIDE]
    world.blockers[OUTSIDE] = [ref(2)]
    members = [ref(1), ref(2)]
    assert _rules(check_group_rules(EPIC, GROUP, members)) == {"dependency path"}
    assert dependency_paths(members) == [(ref(1), OUTSIDE, ref(2))]


def test_dependency_path_break_names_its_start(world: World) -> None:
    # The group audit filters breaks by member: a path's break is about the member it starts at.
    world.blockers[ref(1)] = [OUTSIDE]
    world.blockers[OUTSIDE] = [ref(2)]
    [path] = [
        b for b in check_group_rules(EPIC, GROUP, [ref(1), ref(2)]) if b.rule == "dependency path"
    ]
    assert path.member == ref(1)


def test_breaks_name_their_member_or_none(world: World) -> None:
    # A per-member break names its member; a break about the group as a whole names none.
    world.operational[ref(2)] = "validation"
    other = IssueRef("org", "other", 2)
    pairs = {(b.rule, b.member) for b in check_group_rules(EPIC, GROUP, [ref(1), ref(2), other])}
    assert ("not operational", ref(2)) in pairs
    assert ("one repo", None) in pairs


@pytest.mark.parametrize("count", [0, MAX_BATCH + 1], ids=["none", "too many"])
def test_rule_size_refused(world: World, count: int) -> None:
    members = [ref(n) for n in range(1, count + 1)]
    assert _rules(check_group_rules(EPIC, GROUP, members)) == {"size"}


@pytest.mark.parametrize(
    "kind",
    [epics.ADHOC.key, epics.PLANNING.key, epics.ARCHIVE_KIND, epics.NOT_EPIC_KIND],
    ids=["ad-hoc", "management", "archive", "not an epic"],
)
def test_rule_groups_epic_refused(world: World, kind: str) -> None:
    world.kinds[EPIC] = (EPIC, kind)
    assert _rules(check_group_rules(EPIC, GROUP, [ref(1), ref(2)])) == {"group's epic"}


@pytest.mark.parametrize("parent", [OTHER_EPIC, None], ids=["another epic", "no epic"])
def test_rule_same_epic_refused(world: World, parent: IssueRef | None) -> None:
    world.parents[ref(2)] = parent
    assert _rules(check_group_rules(EPIC, GROUP, [ref(1), ref(2)])) == {"same epic"}


# --- proxies ------------------------------------------------------------------------------


def test_proxy_epic_group_valid(world: World) -> None:
    world.kinds[PROXY] = (PROXY, epics.PROXY_LABEL)
    world.under(PROXY, ref(1), ref(2))
    assert check_group_rules(PROXY, GROUP, [ref(1), ref(2)]) == []


def test_proxy_with_broken_link_refused(world: World) -> None:
    world.kinds[PROXY] = (PROXY, epics.PROXY_LABEL)
    world.under(PROXY, ref(1), ref(2))
    world.proxy_problems = ["the Primary line names no epic"]
    assert _rules(check_group_rules(PROXY, GROUP, [ref(1), ref(2)])) == {"group's epic"}


def test_proxy_with_broken_primary_refused(world: World) -> None:
    world.kinds[PROXY] = (PROXY, epics.PROXY_LABEL)
    world.under(PROXY, ref(1), ref(2))
    world.primary_error = proxies.ProxyLinkError("no Primary line")
    assert _rules(check_group_rules(PROXY, GROUP, [ref(1), ref(2)])) == {"group's epic"}


def test_proxy_of_a_non_development_epic_refused(world: World) -> None:
    world.kinds[PROXY] = (PROXY, epics.PROXY_LABEL)
    world.kinds[PRIMARY] = (PRIMARY, epics.ADHOC.key)
    world.under(PROXY, ref(1), ref(2))
    assert _rules(check_group_rules(PROXY, GROUP, [ref(1), ref(2)])) == {"group's epic"}


# --- dependency paths ----------------------------------------------------------------------


def test_dependency_path_through_open_deployment(world: World) -> None:
    world.operational[OUTSIDE] = "deployment"
    world.blockers[ref(1)] = [OUTSIDE]
    world.blockers[OUTSIDE] = [ref(2)]
    assert dependency_paths([ref(1), ref(2)]) == [(ref(1), OUTSIDE, ref(2))]


def test_dependency_path_through_closed_task_ignored(world: World) -> None:
    world.states[OUTSIDE] = "CLOSED"
    world.blockers[ref(1)] = [OUTSIDE]
    world.blockers[OUTSIDE] = [ref(2)]
    assert dependency_paths([ref(1), ref(2)]) == []
    assert check_group_rules(EPIC, GROUP, [ref(1), ref(2)]) == []


def test_native_path_through_closed_task_ignored(world: World) -> None:
    world.native[ref(1)] = [BlockerState(OUTSIDE, "CLOSED")]
    world.native[OUTSIDE] = [BlockerState(ref(2), "OPEN")]
    assert dependency_paths([ref(1), ref(2)]) == []


def test_dependency_path_across_epics(world: World) -> None:
    world.under(OTHER_EPIC, ELSEWHERE)
    world.native[ref(1)] = [BlockerState(ELSEWHERE, "OPEN")]
    world.native[ELSEWHERE] = [BlockerState(ref(2), "OPEN")]
    assert dependency_paths([ref(1), ref(2)]) == [(ref(1), ELSEWHERE, ref(2))]
    assert _rules(check_group_rules(EPIC, GROUP, [ref(1), ref(2)])) == {"dependency path"}


def test_outside_task_shared_by_two_members_reports_both_paths(world: World) -> None:
    world.blockers[ref(1)] = [OUTSIDE]
    world.blockers[ref(2)] = [OUTSIDE]
    world.blockers[OUTSIDE] = [ref(3)]
    assert dependency_paths([ref(1), ref(2), ref(3)]) == [
        (ref(1), OUTSIDE, ref(3)),
        (ref(2), OUTSIDE, ref(3)),
    ]


def test_direct_link_between_members_is_not_a_path(world: World) -> None:
    world.blockers[ref(2)] = [ref(1)]
    assert dependency_paths([ref(1), ref(2)]) == []


def test_dependency_path_walk_is_cycle_safe(world: World) -> None:
    world.blockers[ref(1)] = [OUTSIDE]
    world.blockers[OUTSIDE] = [ref(1), ELSEWHERE]
    world.blockers[ELSEWHERE] = [OUTSIDE, ref(2)]
    assert dependency_paths([ref(1), ref(2)]) == [(ref(1), OUTSIDE, ref(2))]


# --- the holder check ----------------------------------------------------------------------


def test_holder_open_pr(world: World, tmp_path: Path) -> None:
    world.open_prs = {1: frozenset({7, 8})}
    assert holders(ref(1), main_root=tmp_path) == [
        Holder(ref(1), "open PR", "https://github.com/org/repo/pull/7"),
        Holder(ref(1), "open PR", "https://github.com/org/repo/pull/8"),
    ]


def test_holder_handed_off_batch_relay(world: World, tmp_path: Path) -> None:
    world.relays = {"feature/1-batch": frozenset({1, 2})}
    assert holders(ref(1), main_root=tmp_path) == [
        Holder(ref(1), "handed-off batch", "feature/1-batch")
    ]
    assert holders(ref(3), main_root=tmp_path) == []


def test_holder_handed_off_batch_local(world: World, tmp_path: Path) -> None:
    world.local = {"feature/1-local": frozenset({1, 2})}
    assert holders(ref(1), main_root=tmp_path) == [
        Holder(ref(1), "handed-off batch", "feature/1-local")
    ]


def test_holder_batch_marker(world: World, tmp_path: Path) -> None:
    world.marker = batch_marker.Marker(issues=["#1", "#2"], base_sha="a" * 40)
    assert holders(ref(1), main_root=tmp_path) == [Holder(ref(1), "batch marker", "#1, #2")]


def test_holder_none_for_a_free_task(world: World, tmp_path: Path) -> None:
    assert holders(ref(1), main_root=tmp_path) == []


@pytest.mark.parametrize(
    ("reader", "error"),
    [
        ("open_pr_marks", in_flight.InFlightError("boom")),
        ("ready_relays", in_flight.InFlightError("boom")),
        ("ready_local_branches", in_flight.InFlightError("boom")),
        ("read_marker", batch_marker.BatchMarkerError("boom")),
    ],
)
def test_holder_read_failure_raises(
    world: World, tmp_path: Path, reader: str, error: Exception
) -> None:
    world.failures[reader] = error
    with pytest.raises(GroupRuleReadError, match="boom"):
        holders(ref(1), main_root=tmp_path)


# --- every read failure raises, never an empty list ----------------------------------------


@pytest.mark.parametrize(
    ("reader", "error"),
    [
        ("epic_kind", RuntimeError("boom")),
        ("task_epic", RuntimeError("boom")),
        ("issue_state", RuntimeError("boom")),
        ("non_task_kind", subprocess.CalledProcessError(1, "gh")),
        ("operational_kind", RuntimeError("boom")),
        ("blockers_of", trust.TrustReadError("gh", "boom")),
        ("native_blockers_of", RuntimeError("boom")),
        ("read_json", ValueError("boom")),
        ("read_issue", _untrusted()),
    ],
)
def test_rules_read_failure_raises(world: World, reader: str, error: Exception) -> None:
    world.failures[reader] = error
    with pytest.raises(GroupRuleReadError):
        check_group_rules(EPIC, GROUP, [ref(1), ref(2)])


def test_rules_read_failure_raises_for_the_dependency_walk(world: World) -> None:
    world.blockers[ref(1)] = [OUTSIDE]
    world.states[OUTSIDE] = "OPEN"
    for reader in ("blockers_of", "native_blockers_of", "issue_state"):
        world.failures = {reader: RuntimeError("boom")}
        with pytest.raises(GroupRuleReadError):
            dependency_paths([ref(1), ref(2)])


def test_proxy_link_read_failure_raises(world: World) -> None:
    world.kinds[PROXY] = (PROXY, epics.PROXY_LABEL)
    world.under(PROXY, ref(1), ref(2))
    world.failures["proxy_link_problems"] = RuntimeError("boom")
    with pytest.raises(GroupRuleReadError):
        check_group_rules(PROXY, GROUP, [ref(1), ref(2)])


def test_primary_read_failure_raises(world: World) -> None:
    world.kinds[PROXY] = (PROXY, epics.PROXY_LABEL)
    world.under(PROXY, ref(1), ref(2))
    world.failures["primary_of"] = RuntimeError("boom")
    with pytest.raises(GroupRuleReadError):
        check_group_rules(PROXY, GROUP, [ref(1), ref(2)])


def test_malformed_title_read_raises(world: World) -> None:
    world.raw_title = []
    with pytest.raises(GroupRuleReadError, match="title"):
        check_group_rules(EPIC, GROUP, [ref(1), ref(2)])


def test_missing_token_propagates(world: World) -> None:
    world.failures["issue_state"] = github.MissingGitHubTokenError(1, ["gh"])
    with pytest.raises(github.MissingGitHubTokenError):
        check_group_rules(EPIC, GROUP, [ref(1), ref(2)])
