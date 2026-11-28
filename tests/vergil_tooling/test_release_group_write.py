"""Tests for the release-group writes: ``set`` and ``dissolve``.

Epic vergil-project/.github#1672, phase 3 Task 12 (spec §5.1, §5.5). The log is stubbed at
``record.read_log``, and the group rules run for real (``rules.check_group_rules``) over stubbed
issue-graph reads. The holders are stubbed at ``rules.holders``, the post at
``issue_body.post_comment`` and the clone's origin at ``git.remote_url``. :class:`World` answers
each read from a table, so a test names only what it changes; a failure is raised from its reader.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import epic_audit, epics, git, github, issue_body, proxies, trust
from vergil_tooling.lib.epics import BlockerState, IssueRef, TaskEpic
from vergil_tooling.lib.release_groups import record, rules, write
from vergil_tooling.lib.release_groups.record import Group, GroupLog, GroupRecordError
from vergil_tooling.lib.release_groups.rules import Holder

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

EPIC = IssueRef("org", "repo", 40)
PROXY = IssueRef("org", "repo", 60)
AD_HOC = IssueRef("org", "repo", 70)
PRIMARY = IssueRef("dev", "lib", 9)
OUTSIDE = IssueRef("org", "repo", 30)
MAIN_ROOT = Path("/clones/repo")
ORIGIN = "git@github.com:org/repo.git"


def ref(number: int) -> IssueRef:
    """A task of the test epic's repo."""
    return IssueRef("org", "repo", number)


def _group(
    gid: str, rev: int, *members: IssueRef, dissolved: bool = False, comment: str = ""
) -> Group:
    """The latest record of *gid*, on the test epic, as the log reads it."""
    return Group(
        epic=EPIC,
        group=gid,
        rev=rev,
        members=() if dissolved else members,
        dissolved=dissolved,
        comment_id=comment or str(rev),
    )


def _log(
    *groups: Group,
    broken: dict[str, GroupRecordError] | None = None,
    epic_error: GroupRecordError | None = None,
    epic: IssueRef = EPIC,
) -> GroupLog:
    """The log of *epic* holding *groups*, with *broken* groups refused."""
    broken = broken or {}
    return GroupLog(
        epic=epic,
        groups={g.group: g for g in groups},
        broken=broken,
        epic_error=epic_error,
        ids_used=frozenset({g.group for g in groups} | set(broken)),
    )


@dataclass
class World:
    """The stubbed reads: the log, each issue's kind and state, the rule readers, and the posts."""

    log: GroupLog | Exception = field(default_factory=_log)
    kinds: dict[IssueRef, tuple[IssueRef, str]] = field(
        default_factory=lambda: {
            EPIC: (EPIC, epics.DEVELOPMENT_KIND),
            PROXY: (PROXY, epics.PROXY_LABEL),
            AD_HOC: (AD_HOC, "ad-hoc"),
            PRIMARY: (PRIMARY, epics.DEVELOPMENT_KIND),
        }
    )
    parents: dict[IssueRef, IssueRef] = field(default_factory=dict)
    states: dict[IssueRef, str] = field(default_factory=dict)
    non_tasks: dict[IssueRef, str] = field(default_factory=dict)
    operational: dict[IssueRef, str] = field(default_factory=dict)
    blockers: dict[IssueRef, list[IssueRef]] = field(default_factory=dict)
    holds: dict[IssueRef, list[Holder]] = field(default_factory=dict)
    hold_error: Exception | None = None
    origin: str = ORIGIN
    origin_error: Exception | None = None
    post_error: Exception | None = None
    posts: list[tuple[IssueRef, str]] = field(default_factory=list)

    def read_log(self, epic: IssueRef, **_: object) -> GroupLog:
        if isinstance(self.log, Exception):
            raise self.log
        return self.log

    def epic_kind(self, epic: IssueRef) -> tuple[IssueRef, str]:
        return self.kinds.get(epic, (epic, epics.NOT_EPIC_KIND))

    def task_epic(self, task: IssueRef) -> TaskEpic:
        head = self.parents.get(task, EPIC)
        return TaskEpic(task, head, epics.DEVELOPMENT_KIND, head)

    def issue_state(self, issue: IssueRef) -> str:
        return self.states.get(issue, "OPEN")

    def non_task_kind(self, issue: IssueRef) -> str | None:
        return self.non_tasks.get(issue)

    def operational_kind(self, issue: IssueRef) -> str | None:
        return self.operational.get(issue)

    def blockers_of(self, task: IssueRef) -> list[IssueRef]:
        return list(self.blockers.get(task, []))

    def native_blockers_of(self, task: IssueRef) -> tuple[BlockerState, ...]:
        return ()

    def proxy_link_problems(self, epic: IssueRef) -> list[str]:
        return []

    def primary_of(self, proxy: IssueRef) -> IssueRef:
        return PRIMARY

    def read_issue(self, repo: str, number: int, **_: object) -> trust.IssueContent:
        content = trust.IssueContent(
            body="", body_role=None, author_role=None, comments=(), skipped=()
        )
        return content

    def read_json(self, *args: str, **_: object) -> object:
        return {"title": "A task"}

    def holders(self, task: IssueRef, *, main_root: Path) -> list[Holder]:
        if self.hold_error is not None:
            raise self.hold_error
        return list(self.holds.get(task, []))

    def remote_url(self, remote: str = "origin", repo: Path | None = None) -> str:
        if self.origin_error is not None:
            raise self.origin_error
        return self.origin

    def post_comment(self, owner: str, repo: str, number: int, body: str) -> None:
        if self.post_error is not None:
            raise self.post_error
        self.posts.append((IssueRef(owner, repo, number), body))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    stub = World()
    monkeypatch.setattr(record, "read_log", stub.read_log)
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
    monkeypatch.setattr(rules, "holders", stub.holders)
    monkeypatch.setattr(git, "remote_url", stub.remote_url)
    monkeypatch.setattr(issue_body, "post_comment", stub.post_comment)
    return stub


def _set(
    members: list[IssueRef],
    *,
    group: str = "G1",
    reason: str | None = "A reason",
    at_batch_stop: bool = False,
    epic: IssueRef = EPIC,
) -> write.WriteResult:
    return write.set_group(
        epic, group, members, reason=reason, at_batch_stop=at_batch_stop, main_root=MAIN_ROOT
    )


def _dissolve(
    *, group: str = "G1", reason: str | None = "A reason", at_batch_stop: bool = False
) -> write.WriteResult:
    return write.dissolve_group(
        EPIC, group, reason=reason, at_batch_stop=at_batch_stop, main_root=MAIN_ROOT
    )


def _refused(call: Callable[[], object]) -> str:
    """The refusal *call* raises, as one string."""
    with pytest.raises(write.WriteRefusedError) as exc:
        call()
    return str(exc.value)


def _marker(task: IssueRef, where: str) -> Holder:
    return Holder(task, "batch marker", where)


# --- a new group, a retry, and the record's shape -----------------------------------------


def test_set_new_group_rev_1(world: World) -> None:
    result = _set([ref(1), ref(2)])
    assert result == write.WriteResult(posted=True, rev=1)
    assert world.posts == [
        (EPIC, record.render_record("G1", 1, [ref(1), ref(2)], reason="A reason"))
    ]


def test_set_on_proxy_epic_posts_on_proxy(world: World) -> None:
    world.log = _log(epic=PROXY)
    world.parents = {ref(1): PROXY, ref(2): PROXY}
    result = write.set_group(
        PROXY, "G1", [ref(1), ref(2)], reason="r", at_batch_stop=False, main_root=MAIN_ROOT
    )
    assert result.posted
    assert [target for target, _ in world.posts] == [PROXY]


@pytest.mark.parametrize("kind", ["ad-hoc", "management"])
def test_set_on_ad_hoc_or_management_epic_refused(world: World, kind: str) -> None:
    world.kinds[AD_HOC] = (AD_HOC, kind)
    world.parents = {ref(1): AD_HOC}
    world.log = _log(epic=AD_HOC)
    message = _refused(lambda: _set([ref(1)], epic=AD_HOC))
    assert "group's epic" in message
    assert world.posts == []


def test_set_retry_posts_nothing(world: World) -> None:
    world.log = _log(_group("G1", 3, ref(1), ref(2)))
    result = _set([ref(2), ref(1)], reason=None)
    assert result == write.WriteResult(posted=False, rev=3)
    assert world.posts == []


def test_dissolve_retry_posts_nothing(world: World) -> None:
    world.log = _log(_group("G1", 2, dissolved=True))
    result = _dissolve(reason=None)
    assert result == write.WriteResult(posted=False, rev=2)
    assert world.posts == []


def test_set_dissolved_id_refused(world: World) -> None:
    world.log = _log(_group("G1", 2, dissolved=True))
    message = _refused(lambda: _set([ref(1)]))
    assert "dissolved" in message
    assert "use a new group ID" in message
    assert world.posts == []


def test_dissolve_unknown_group_refused(world: World) -> None:
    message = _refused(lambda: _dissolve(group="G9"))
    assert "nothing to dissolve" in message
    assert world.posts == []


def test_set_rejects_a_member_listed_twice(world: World) -> None:
    message = _refused(lambda: _set([ref(1), ref(1)]))
    assert "more than once" in message
    assert world.posts == []


# --- the group rules ----------------------------------------------------------------------


def test_set_rule_break_refused(world: World) -> None:
    world.operational[ref(2)] = "operational"
    message = _refused(lambda: _set([ref(1), ref(2)]))
    assert "not operational" in message
    assert world.posts == []


def test_set_dependency_path_refused(world: World) -> None:
    world.blockers = {ref(1): [OUTSIDE], OUTSIDE: [ref(2)]}
    message = _refused(lambda: _set([ref(1), ref(2)]))
    assert "dependency path" in message
    assert OUTSIDE.slug in message
    assert world.posts == []


@pytest.mark.parametrize("reason", [None, "", "   "])
@pytest.mark.parametrize("case", ["new group", "addition", "removal", "dissolve"])
def test_set_without_reason_refused(world: World, case: str, reason: str | None) -> None:
    if case != "new group":
        world.log = _log(_group("G1", 1, ref(1), ref(2)))
    change: dict[str, Callable[[], object]] = {
        "new group": lambda: _set([ref(1), ref(2)], reason=reason),
        "addition": lambda: _set([ref(1), ref(2), ref(3)], reason=reason),
        "removal": lambda: _set([ref(1)], reason=reason),
        "dissolve": lambda: _dissolve(reason=reason),
    }
    assert "needs a reason" in _refused(change[case])
    assert world.posts == []


def test_dissolve_records_no_members(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1), ref(2)))
    result = _dissolve(reason="Shipped elsewhere")
    assert result == write.WriteResult(posted=True, rev=2)
    assert world.posts == [
        (
            EPIC,
            record.render_record("G1", 2, (), dissolved=True, reason="Shipped elsewhere"),
        )
    ]
    assert world.posts[0][1] == "release-group G1 rev 2: dissolved\nReason: Shipped elsewhere"


def test_open_rules_skip_closed_members(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1), ref(2)))
    world.states[ref(2)] = "CLOSED"
    world.blockers = {ref(2): [OUTSIDE], OUTSIDE: [ref(3)]}
    result = _set([ref(1), ref(2), ref(3)])
    assert result == write.WriteResult(posted=True, rev=2)
    assert world.posts == [
        (EPIC, record.render_record("G1", 2, [ref(1), ref(2), ref(3)], reason="A reason"))
    ]


def test_open_member_dependency_path_still_refused(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1), ref(2)))
    world.blockers = {ref(2): [OUTSIDE], OUTSIDE: [ref(3)]}
    message = _refused(lambda: _set([ref(1), ref(2), ref(3)]))
    assert "dependency path" in message
    assert world.posts == []


# --- incoming tasks -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "holder",
    [
        Holder(ref(3), "open PR", "https://github.com/org/repo/pull/90"),
        Holder(ref(3), "handed-off batch", "feature/batch-1"),
        Holder(ref(3), "batch marker", "#7, #8"),
    ],
    ids=["open PR", "handed-off batch", "batch marker"],
)
def test_incoming_task_with_holder_refused(world: World, holder: Holder) -> None:
    world.log = _log(_group("G1", 1, ref(1)))
    world.holds[ref(3)] = [holder]
    message = _refused(lambda: _set([ref(1), ref(3)]))
    assert holder.where in message
    assert world.posts == []


def test_incoming_closed_task_refused_for_an_addition(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1)))
    world.states[ref(3)] = "CLOSED"
    message = _refused(lambda: _set([ref(1), ref(3)]))
    assert "org/repo#3 is closed, not open" in message
    assert world.posts == []


def test_incoming_closed_task_refused_for_a_merge(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1)), _group("G2", 1, ref(3), ref(4)))
    world.states[ref(4)] = "CLOSED"
    message = _refused(lambda: _set([ref(1), ref(3), ref(4)]))
    assert "org/repo#4 is closed, not open" in message
    assert "live group G2" in message
    assert world.posts == []


def test_incoming_task_in_another_live_group_refused(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1)), _group("G2", 1, ref(3)))
    message = _refused(lambda: _set([ref(1), ref(3)]))
    assert "org/repo#3 is a member of live group G2" in message
    assert world.posts == []


# --- frozen and started -------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["open PR", "handed-off batch"])
@pytest.mark.parametrize("at_batch_stop", [False, True])
def test_change_refused_while_frozen(
    world: World, kind: Literal["open PR", "handed-off batch"], at_batch_stop: bool
) -> None:
    world.log = _log(_group("G1", 1, ref(1), ref(2)))
    world.holds[ref(2)] = [Holder(ref(2), kind, "where-it-is")]
    message = _refused(lambda: _set([ref(1), ref(2), ref(3)], at_batch_stop=at_batch_stop))
    assert "frozen" in message
    assert "org/repo#2" in message
    assert "vrg-pr-workflow unfreeze" in message
    assert world.posts == []


def test_change_after_batch_start_stops_for_human(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1), ref(2)))
    world.holds[ref(1)] = [_marker(ref(1), "#1, #2")]
    message = _refused(lambda: _set([ref(1), ref(2), ref(3)]))
    assert "#1, #2" in message
    assert "--at-batch-stop" in message
    assert world.posts == []


def test_at_batch_stop_allows_marker_only(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1), ref(2)))
    world.holds[ref(1)] = [_marker(ref(1), "#1, #2")]
    world.holds[ref(2)] = [_marker(ref(2), "#1, #2")]
    result = _set([ref(1), ref(2), ref(3)], at_batch_stop=True)
    assert result == write.WriteResult(posted=True, rev=2)


def test_at_batch_stop_incoming_same_batch_marker_allowed(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1)))
    world.holds[ref(1)] = [_marker(ref(1), "#1, #2, #3")]
    world.holds[ref(3)] = [_marker(ref(3), "#1, #2, #3")]
    result = _set([ref(1), ref(3)], at_batch_stop=True)
    assert result == write.WriteResult(posted=True, rev=2)
    assert world.posts[0][0] == EPIC


@pytest.mark.parametrize(
    "holds",
    [
        [Holder(ref(3), "batch marker", "#7, #8")],
        [Holder(ref(3), "open PR", "https://github.com/org/repo/pull/90")],
        [Holder(ref(3), "handed-off batch", "feature/batch-1")],
        [Holder(ref(3), "batch marker", "#1, #2, #3"), Holder(ref(3), "open PR", "pr")],
    ],
    ids=["other batch", "open PR", "handed-off batch", "own marker and a PR"],
)
def test_at_batch_stop_incoming_other_holder_refused(world: World, holds: list[Holder]) -> None:
    world.log = _log(_group("G1", 1, ref(1)))
    world.holds[ref(1)] = [_marker(ref(1), "#1, #2, #3")]
    world.holds[ref(3)] = holds
    message = _refused(lambda: _set([ref(1), ref(3)], at_batch_stop=True))
    assert holds[0].where in message
    assert world.posts == []


# --- broken logs --------------------------------------------------------------------------


def test_set_refuses_on_conflict(world: World) -> None:
    world.log = _log(
        broken={
            "G1": GroupRecordError(
                "org/repo#40 group G1 cannot be read: comment 5 and comment 9 both set"
                " revision 2 (records: comment 5, comment 9)",
                unreadable=True,
            )
        }
    )
    message = _refused(lambda: _set([ref(1), ref(2)]))
    assert "comment 5" in message
    assert "comment 9" in message
    assert "delete the bad one" in message
    assert world.posts == []
    world.log = _log(_group("G1", 2, ref(1), comment="9"))
    assert _set([ref(1), ref(2)]) == write.WriteResult(posted=True, rev=3)


def test_set_refuses_on_edited_record(world: World) -> None:
    world.log = _log(
        broken={
            "G1": GroupRecordError(
                "org/repo#40 group G1 cannot be read: comment 7 was edited after it was posted"
                " (records: comment 7, comment 4)",
                edits=("comment 7 was edited after it was posted",),
                unreadable=False,
            )
        }
    )
    message = _refused(lambda: _set([ref(1), ref(2)]))
    assert "comment 7" in message
    assert "delete the bad one" in message
    assert world.posts == []
    world.log = _log(_group("G1", 3, ref(1), comment="4"))
    assert _set([ref(1), ref(2)]) == write.WriteResult(posted=True, rev=4)


def test_skipped_edit_refuses_with_recovery(world: World) -> None:
    world.log = _log(
        epic_error=GroupRecordError(
            "org/repo#40 cannot be read: body was edited by an identity outside the human and"
            " agents roles (comment 12, login outsider)",
            edits=("body was edited by an identity outside the human and agents roles",),
            unreadable=False,
        )
    )
    message = _refused(lambda: _set([ref(1)]))
    assert "comment 12" in message
    assert "delete the bad one" in message
    assert world.posts == []


def test_failed_log_read_refuses_naming_it(world: World) -> None:
    world.log = RuntimeError("the comments could not be read")
    message = _refused(lambda: _set([ref(1)]))
    assert "could not read the group log of org/repo#40" in message
    assert "a human must look at it" in message
    assert world.posts == []


# --- splits, closed epics, clones and posts -----------------------------------------------


def test_split_is_one_record(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1), ref(2), ref(3)))
    result = _set([ref(1), ref(2)], reason="Split: 3 ships in its own group")
    assert result == write.WriteResult(posted=True, rev=2)
    assert len(world.posts) == 1
    assert world.posts[0][1] == record.render_record(
        "G1", 2, [ref(1), ref(2)], reason="Split: 3 ships in its own group"
    )


@pytest.mark.parametrize("change", ["set", "dissolve"])
def test_change_in_closed_epic_refused(world: World, change: str) -> None:
    world.log = _log(_group("G1", 1, ref(1)))
    world.states[EPIC] = "CLOSED"
    call = (lambda: _set([ref(1)])) if change == "set" else (lambda: _dissolve())
    message = _refused(call)
    assert "org/repo#40 is closed" in message
    assert world.posts == []


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("git@github.com:other/lib.git", "run this from the main clone of org/repo"),
        ("https://example.com/org/repo.git", "not a github.com repo"),
    ],
    ids=["another repo", "not GitHub"],
)
def test_run_outside_members_clone_refused(world: World, origin: str, expected: str) -> None:
    world.origin = origin
    message = _refused(lambda: _set([ref(1)]))
    assert expected in message
    assert world.posts == []


def test_failed_hold_read_refuses_naming_it(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1)))
    world.hold_error = rules.GroupRuleReadError("could not read the open PRs of org/repo: boom")
    message = _refused(lambda: _set([ref(1), ref(2)]))
    assert "could not read what holds org/repo#1" in message
    assert "boom" in message
    assert world.posts == []


def test_failed_post_names_what_may_have_landed(world: World) -> None:
    world.post_error = github.GitHubAPIError(1, ["gh", "issue", "comment"], output="HTTP 502")
    message = _refused(lambda: _set([ref(1)]))
    assert "could not post G1 rev 1 on org/repo#40" in message
    assert "may have landed" in message


def test_set_names_no_members_refused(world: World) -> None:
    message = _refused(lambda: _set([]))
    assert "names at least one member" in message
    assert world.posts == []


def test_set_refuses_a_group_id_that_is_not_one(world: World) -> None:
    message = _refused(lambda: _set([ref(1)], group="G01"))
    assert "not a group ID" in message
    assert world.posts == []


def test_multi_line_reason_refused(world: World) -> None:
    message = _refused(lambda: _set([ref(1)], reason="one line\nand another"))
    assert "the record cannot be written" in message
    assert world.posts == []


def test_unreadable_origin_refused(world: World) -> None:
    world.origin_error = subprocess.CalledProcessError(128, ["git", "remote", "get-url"])
    message = _refused(lambda: _set([ref(1)]))
    assert "its origin remote cannot be read" in message
    assert world.posts == []


def test_incoming_task_beside_another_live_group_allowed(world: World) -> None:
    world.log = _log(_group("G1", 1, ref(1)), _group("G2", 1, ref(5)))
    result = _set([ref(1), ref(3)])
    assert result == write.WriteResult(posted=True, rev=2)


@pytest.mark.parametrize("step", ["read", "post"])
def test_missing_token_propagates(world: World, step: str) -> None:
    token_error = github.MissingGitHubTokenError(1, ["gh", "api"], output="no token")
    if step == "read":
        world.log = token_error
    else:
        world.log = _log(_group("G1", 1, ref(1)))
        world.post_error = token_error
    with pytest.raises(github.MissingGitHubTokenError):
        _set([ref(1), ref(2)] if step == "post" else [ref(1)])
