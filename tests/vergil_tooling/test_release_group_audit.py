"""Tests for the group half of ``vrg-epic-audit --epic``: when a live group is runnable.

Epic vergil-project/.github#1672, phase 3 Task 16 (spec §5.3). The group log is stubbed at
``record.read_log``; the members' native and ``Blocked-by:`` blockers, and each blocker's
state, are stubbed at the ``epics`` readers.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib import epics, github, trust
from vergil_tooling.lib.epic_docs import EpicRecord
from vergil_tooling.lib.epics import BlockerState, IssueRef, TaskEpic
from vergil_tooling.lib.release_groups import audit, closers, record
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.release_groups.audit"
# The home the listings are read from. Its epics are the test repo's, so their refs match EPIC.
HOME = "org/repo"
EPIC = IssueRef("org", "repo", 40)
_AUTHORS = TrustedAuthors(human=("human",), agents=())


def ref(number: int) -> IssueRef:
    """A task of the test epic's repo."""
    return IssueRef("org", "repo", number)


def _pr(number: int) -> IssueRef:
    """A PR of the test epic's repo, as a closer of a task."""
    return IssueRef("org", "repo", number)


def _listed(number: int, state: str = "OPEN") -> EpicRecord:
    """One epic of the home's listing."""
    return EpicRecord(
        number=number, title="", state=state, stateReason=None, createdAt="", labels=[], body=None
    )


def _scan(*listed: EpicRecord) -> list[audit.GroupFinding]:
    """The findings of the home's listing *listed* (the test epic when none is given)."""
    listing = list(listed) or [_listed(40)]
    return audit.group_findings("org", home=HOME, listing=listing, authors=_AUTHORS)


@dataclass
class _World:
    """The stubbed reads: the epic's group log, and each member's blockers and their states.

    The other fields stub what a group check reads of an epic and a member: each epic's kind
    (its own when unset), each member's closing PRs, its placement and its labels.
    """

    groups: dict[str, record.Group] = field(default_factory=dict)
    broken: dict[str, record.GroupRecordError] = field(default_factory=dict)
    epic_error: record.GroupRecordError | None = None
    native: dict[IssueRef, tuple[BlockerState, ...]] = field(default_factory=dict)
    body: dict[IssueRef, list[IssueRef]] = field(default_factory=dict)
    states: dict[IssueRef, str] = field(default_factory=dict)
    logs: dict[IssueRef, record.GroupLog] = field(default_factory=dict)
    fail: dict[IssueRef, Exception] = field(default_factory=dict)
    reads: list[IssueRef] = field(default_factory=list)
    kinds: dict[IssueRef, tuple[IssueRef, str]] = field(default_factory=dict)
    placed: dict[IssueRef, TaskEpic] = field(default_factory=dict)
    operational: dict[IssueRef, str] = field(default_factory=dict)
    closers: dict[IssueRef, tuple[IssueRef, ...]] = field(default_factory=dict)
    closer_errors: dict[IssueRef, Exception] = field(default_factory=dict)

    def kind_of(self, epic: IssueRef) -> tuple[IssueRef, str]:
        """An epic's chain head and kind: a development epic of itself, unless stubbed."""
        return self.kinds.get(epic, (epic, epics.DEVELOPMENT_KIND))

    def placed_of(self, task: IssueRef) -> TaskEpic:
        """A task's epic: the test epic, unless stubbed."""
        return self.placed.get(task, TaskEpic(task, EPIC, epics.DEVELOPMENT_KIND, None))

    def merged(self, task: IssueRef, **_: object) -> tuple[IssueRef, ...]:
        """A task's merged closing PRs, or the failure stubbed for it."""
        if task in self.closer_errors:
            raise self.closer_errors[task]
        return self.closers.get(task, ())

    def add_group(self, group: str, rev: int, *members: int, dissolved: bool = False) -> None:
        """Add a group of the test epic, its members the given task numbers."""
        self.groups[group] = record.Group(
            epic=EPIC,
            group=group,
            rev=rev,
            members=tuple(ref(n) for n in members),
            dissolved=dissolved,
            comment_id=f"comment-{group}",
        )

    def log(self, epic: IssueRef, **_: object) -> record.GroupLog:
        """The group log, as :func:`record.read_log` returns it: the epic's own when stubbed."""
        self.reads.append(epic)
        if epic in self.fail:
            raise self.fail[epic]
        if epic in self.logs:
            return self.logs[epic]
        return record.GroupLog(
            epic=epic,
            groups=self.groups,
            broken=self.broken,
            epic_error=self.epic_error,
            ids_used=frozenset(self.groups) | frozenset(self.broken),
        )


@pytest.fixture
def world() -> Iterator[_World]:
    w = _World()
    with (
        patch("vergil_tooling.lib.release_groups.record.read_log", side_effect=w.log),
        patch(f"{_MOD}.github.target_org", side_effect=lambda org: contextlib.nullcontext()),
        patch(
            "vergil_tooling.lib.epics.native_blockers_of",
            side_effect=lambda task: w.native.get(task, ()),
        ),
        patch(
            "vergil_tooling.lib.epics.blockers_of",
            side_effect=lambda task: list(w.body.get(task, [])),
        ),
        # An issue nobody stubbed is open: the epic and its members are open by default.
        patch(
            "vergil_tooling.lib.epics.issue_state", side_effect=lambda r: w.states.get(r, "OPEN")
        ),
        patch("vergil_tooling.lib.epics.epic_kind", side_effect=w.kind_of),
        patch("vergil_tooling.lib.epics.task_epic", side_effect=w.placed_of),
        patch("vergil_tooling.lib.epics.non_task_kind", return_value=None),
        patch(
            "vergil_tooling.lib.epics.operational_kind",
            side_effect=lambda r: w.operational.get(r),
        ),
        patch("vergil_tooling.lib.release_groups.rules._is_doc_review", return_value=False),
        patch("vergil_tooling.lib.release_groups.closers.merged_closers", side_effect=w.merged),
    ):
        yield w


def _blocker(number: int, state: str) -> BlockerState:
    return BlockerState(ref(number), state)


def test_group_runnable_when_outside_blockers_closed(world: _World) -> None:
    # Member 1's native blocker is closed and its body names the same issue; member 2's body
    # blocker is closed too. Every outside blocker is closed, so the group runs.
    world.add_group("G1", 2, 1, 2)
    world.native[ref(1)] = (_blocker(9, "CLOSED"),)
    world.body[ref(1)] = [ref(9)]
    world.body[ref(2)] = [ref(10)]
    world.states[ref(10)] = "CLOSED"
    [status] = audit.group_status(EPIC)
    assert status == audit.GroupStatus("G1", 2, (ref(1), ref(2)), True, ())


def test_group_blocked_by_outside_task(world: _World) -> None:
    # Member 2 is natively blocked by an open task outside the group.
    world.add_group("G1", 1, 1, 2)
    world.native[ref(2)] = (_blocker(9, "OPEN"),)
    [status] = audit.group_status(EPIC)
    assert status.runnable is False
    assert status.blocked_by == (ref(9),)


def test_inner_blockers_ignored(world: _World) -> None:
    # Member 2 is blocked by member 1, which is open: the members ship together, so it
    # does not block the group.
    world.add_group("G1", 1, 1, 2)
    world.native[ref(2)] = (_blocker(1, "OPEN"),)
    world.body[ref(1)] = [ref(2)]
    world.states[ref(2)] = "OPEN"
    [status] = audit.group_status(EPIC)
    assert status == audit.GroupStatus("G1", 1, (ref(1), ref(2)), True, ())


def test_group_blocked_by_outside_task_via_blocked_by_line(world: _World) -> None:
    # A Blocked-by line of a member's trusted body names an open task outside the group.
    world.add_group("G1", 1, 1)
    world.body[ref(1)] = [ref(9)]
    world.states[ref(9)] = "OPEN"
    [status] = audit.group_status(EPIC)
    assert status.runnable is False
    assert status.blocked_by == (ref(9),)


def test_closed_outside_blocker_does_not_block(world: _World) -> None:
    world.add_group("G1", 1, 1)
    world.body[ref(1)] = [ref(9)]
    world.states[ref(9)] = "CLOSED"
    [status] = audit.group_status(EPIC)
    assert status == audit.GroupStatus("G1", 1, (ref(1),), True, ())


def test_dissolved_groups_are_omitted(world: _World) -> None:
    world.add_group("G1", 2, 1, dissolved=True)
    world.add_group("G2", 1, 2)
    [status] = audit.group_status(EPIC)
    assert status.group == "G2"


def test_groups_returned_in_id_order(world: _World) -> None:
    # G10 is read before G2, but the groups come back G2 first (numeric, not lexical, order).
    world.add_group("G10", 1, 3)
    world.add_group("G2", 1, 4)
    assert [s.group for s in audit.group_status(EPIC)] == ["G2", "G10"]


def test_blocked_by_is_sorted(world: _World) -> None:
    # Outside blockers come back sorted by (owner, repo, number), whatever order they were read
    # in: "org" sorts before "other", so the org/repo refs come first.
    world.add_group("G1", 1, 1)
    other = IssueRef("other", "lib", 2)
    world.native[ref(1)] = (_blocker(9, "OPEN"), BlockerState(other, "OPEN"))
    world.body[ref(1)] = [ref(3)]
    world.states[ref(3)] = "OPEN"
    [status] = audit.group_status(EPIC)
    assert status.blocked_by == (ref(3), ref(9), other)


def test_broken_group_raises(world: _World) -> None:
    error = record.GroupRecordError("org/repo#40: group G3 has a record that cannot be read")
    world.add_group("G1", 1, 1)
    world.broken["G3"] = error
    with pytest.raises(record.GroupRecordError) as raised:
        audit.group_status(EPIC)
    assert raised.value is error


def test_epic_error_raises(world: _World) -> None:
    error = record.GroupRecordError("org/repo#40: the epic refuses every read of its groups")
    world.add_group("G1", 1, 1)
    world.epic_error = error
    with pytest.raises(record.GroupRecordError) as raised:
        audit.group_status(EPIC)
    assert raised.value is error


def test_no_groups_is_an_empty_list(world: _World) -> None:
    assert audit.group_status(EPIC) == []


def test_member_blocker_read_failure_propagates(world: _World) -> None:
    # A native blocker list that cannot be read in full is never "runnable": its error propagates.
    world.add_group("G1", 1, 1)
    error = epics.NativeBlockersReadError(
        "org/repo#1 has 2 native blockers but only 1 could be read"
    )
    with (
        patch(f"{_MOD}.epics.native_blockers_of", side_effect=error),
        pytest.raises(epics.NativeBlockersReadError),
    ):
        audit.group_status(EPIC)


# --- group_findings: the closing PRs of a group's members ---------------------


def test_split_group_names_both_prs(world: _World) -> None:
    # Two closed members, closed by two different merged PRs: a split, naming both PRs.
    world.add_group("G1", 1, 1, 2)
    world.states.update({ref(1): "CLOSED", ref(2): "CLOSED"})
    world.closers.update({ref(1): (_pr(10),), ref(2): (_pr(11),)})
    [finding] = _scan()
    assert finding.kind == "split"
    assert finding.group == "G1"
    assert finding.epic == EPIC
    assert finding.detail == (
        "members closed by different merged PRs: org/repo#1 (closed) by org/repo#10;"
        " org/repo#2 (closed) by org/repo#11"
    )


def test_member_closed_unmerged_flagged(world: _World) -> None:
    # Member 1 is closed, and no merged PR closes it; member 2's PR is the only closer.
    world.add_group("G1", 1, 1, 2)
    world.states.update({ref(1): "CLOSED", ref(2): "CLOSED"})
    world.closers[ref(2)] = (_pr(10),)
    [finding] = _scan()
    assert finding == audit.GroupFinding(
        EPIC, "G1", "closed unmerged", "org/repo#1 is closed, but no merged PR closes it"
    )


def test_open_member_with_merged_pr_is_drift_not_break(world: _World) -> None:
    # Member 2 is open, but its merged PR also closed member 1: the drift-close pass closes
    # it. That is no finding, not a split and not a broken group.
    world.add_group("G1", 1, 1, 2)
    world.states.update({ref(1): "CLOSED", ref(2): "OPEN"})
    world.closers.update({ref(1): (_pr(10),), ref(2): (_pr(10),)})
    assert _scan() == []


def test_closed_member_shipped_is_not_reported_as_closed_not_open(world: _World) -> None:
    # Member 1 shipped through PR 10 and is closed; member 2 is still open. The rule check
    # breaks "open code task" for the closed member, which is not a finding: only an open
    # member's break is. The naive audit would report "org/repo#1 is closed, not open".
    world.add_group("G1", 1, 1, 2)
    world.states.update({ref(1): "CLOSED", ref(2): "OPEN"})
    world.closers[ref(1)] = (_pr(10),)
    assert _scan() == []


def test_open_member_breaking_a_rule_is_reported(world: _World) -> None:
    # Member 2 is open and labelled operational: a rule break by an open member is a finding.
    world.add_group("G1", 1, 1, 2)
    world.operational[ref(2)] = "validation"
    [finding] = _scan()
    assert finding == audit.GroupFinding(
        EPIC,
        "G1",
        "rule",
        "not operational: org/repo#2 is labelled validation, not PR-workable",
    )


def test_healthy_group_has_no_findings(world: _World) -> None:
    world.add_group("G1", 1, 1, 2)
    assert _scan() == []


def test_dissolved_group_not_checked(world: _World) -> None:
    # A dissolved group's members are never checked: a split among them is not a finding.
    world.add_group("G1", 2, 1, 2, dissolved=True)
    world.states.update({ref(1): "CLOSED", ref(2): "CLOSED"})
    world.closers.update({ref(1): (_pr(10),), ref(2): (_pr(11),)})
    assert _scan() == []


# --- group_findings: the dependency path and the member moved out -------------


def test_dependency_path_from_blocked_by_edit_fails_audit(world: _World) -> None:
    # No group write: member 1's trusted body now says it waits on task 9, which waits on
    # member 2. The group's own record is unchanged, and the audit still reports the path.
    world.add_group("G1", 1, 1, 2)
    world.body[ref(1)] = [ref(9)]
    world.body[ref(9)] = [ref(2)]
    [finding] = _scan()
    assert finding == audit.GroupFinding(
        EPIC,
        "G1",
        "rule",
        "dependency path: org/repo#1 waits on org/repo#9, an open task outside the group,"
        " which waits on org/repo#2",
    )


def test_audit_flags_member_moved_out_of_epic(world: _World) -> None:
    # Member 2 was moved by hand to epic 41: the group breaks the same-epic rule, and the
    # finding names the member, so nothing silently drops it.
    world.add_group("G1", 1, 1, 2)
    world.placed[ref(2)] = TaskEpic(
        ref(2), IssueRef("org", "repo", 41), epics.DEVELOPMENT_KIND, None
    )
    [finding] = _scan()
    assert finding == audit.GroupFinding(
        EPIC, "G1", "rule", "same epic: org/repo#2 is under org/repo#41, not org/repo#40"
    )


# --- group_findings: the group log's refusals ---------------------------------


def test_edited_record_flagged(world: _World) -> None:
    world.broken["G1"] = record.GroupRecordError(
        "org/repo#40 group G1 cannot be read: comment 7 was edited after it was posted",
        edits=("comment 7 was edited after it was posted",),
        unreadable=False,
    )
    assert _scan() == [
        audit.GroupFinding(EPIC, "G1", "edited", "comment 7 was edited after it was posted")
    ]


def test_skipped_record_flagged(world: _World) -> None:
    # An outsider's edit of a trusted comment on the epic is an epic-wide edit finding.
    edit = "comment 8 was edited by an identity outside the human and agents roles"
    world.epic_error = record.GroupRecordError(
        f"org/repo#40 cannot be read: {edit}", edits=(edit,), unreadable=False
    )
    assert _scan() == [audit.GroupFinding(EPIC, "", "edited", edit)]


def test_conflict_flagged_unreadable(world: _World) -> None:
    world.broken["G1"] = record.GroupRecordError(
        "org/repo#40 group G1 cannot be read: comment 1 and comment 2 both set revision 3"
    )
    [finding] = _scan()
    assert finding.kind == "unreadable"
    assert finding.group == "G1"
    assert "both set revision 3" in finding.detail


def test_edit_and_conflict_flagged_as_both(world: _World) -> None:
    world.broken["G1"] = record.GroupRecordError(
        "org/repo#40 group G1 cannot be read: comment 2 was edited; comment 1 and comment 2"
        " both set revision 3",
        edits=("comment 2 was edited after it was posted",),
    )
    assert [f.kind for f in _scan()] == ["edited", "unreadable"]


def test_unreadable_epic_log_flagged(world: _World) -> None:
    world.epic_error = record.GroupRecordError("org/repo#40 cannot be read: it holds a conflict")
    assert _scan() == [
        audit.GroupFinding(
            EPIC, "", "unreadable", "org/repo#40 cannot be read: it holds a conflict"
        )
    ]


# --- group_findings: which epics and groups are checked ----------------------


def test_closed_epic_not_checked(world: _World) -> None:
    world.add_group("G1", 1, 1, 2)
    world.states.update({ref(1): "CLOSED", ref(2): "CLOSED"})
    world.closers.update({ref(1): (_pr(10),), ref(2): (_pr(11),)})
    assert _scan(_listed(40, "CLOSED")) == []
    assert world.reads == []


def test_closed_head_of_an_open_continuation_not_checked(world: _World) -> None:
    # Continuation 41 is open, but its head 40 is closed: the chain is closed, so nothing is read.
    world.add_group("G1", 1, 1, 2)
    world.states.update({EPIC: "CLOSED", ref(1): "CLOSED", ref(2): "CLOSED"})
    world.closers.update({ref(1): (_pr(10),), ref(2): (_pr(11),)})
    world.kinds[IssueRef("org", "repo", 41)] = (EPIC, epics.DEVELOPMENT_KIND)
    assert _scan(_listed(41)) == []
    assert world.reads == []


def test_continuation_resolves_to_its_head_once(world: _World) -> None:
    # Epic 40 and its continuation 41 both list; the head is checked once.
    world.add_group("G1", 1, 1, 2)
    world.states.update({ref(1): "CLOSED", ref(2): "CLOSED"})
    world.closers.update({ref(1): (_pr(10),), ref(2): (_pr(11),)})
    world.kinds[IssueRef("org", "repo", 41)] = (EPIC, epics.DEVELOPMENT_KIND)
    [finding] = _scan(_listed(40), _listed(41))
    assert finding.epic == EPIC
    assert world.reads == [EPIC]


def test_non_group_epic_kinds_not_checked(world: _World) -> None:
    # An archive is neither a development epic nor a proxy: its groups are not checked.
    world.kinds[IssueRef("org", "repo", 41)] = (IssueRef("org", "repo", 41), epics.ARCHIVE_KIND)
    world.logs[IssueRef("org", "repo", 41)] = record.GroupLog(
        IssueRef("org", "repo", 41), {}, {}, None, frozenset()
    )
    assert _scan(_listed(41)) == []
    assert world.reads == []


def test_proxy_epic_groups_checked(world: _World) -> None:
    # A proxy epic's groups are checked like a development epic's, under the proxy's own ref.
    proxy = IssueRef("org", "repo", 41)
    world.kinds[proxy] = (proxy, epics.PROXY_LABEL)
    group = record.Group(proxy, "G1", 1, (ref(3), ref(4)), False, "comment-1")
    world.logs[proxy] = record.GroupLog(proxy, {"G1": group}, {}, None, frozenset({"G1"}))
    world.states.update({ref(3): "CLOSED", ref(4): "CLOSED"})
    world.closers.update({ref(3): (_pr(10),), ref(4): (_pr(11),)})
    [finding] = _scan(_listed(41))
    assert finding.epic == proxy
    assert finding.kind == "split"


def test_read_failure_names_its_epic_and_the_rest_are_checked(world: _World) -> None:
    proxy = IssueRef("org", "repo", 41)
    world.kinds[proxy] = (proxy, epics.PROXY_LABEL)
    world.fail[proxy] = trust.TrustReadError("comments", "the author could not be read")
    world.add_group("G1", 1, 1, 2)
    world.states.update({ref(1): "CLOSED", ref(2): "CLOSED"})
    world.closers.update({ref(1): (_pr(10),), ref(2): (_pr(11),)})
    split, unreadable = _scan(_listed(40), _listed(41))
    assert split.kind == "split"
    assert unreadable.epic == proxy
    assert unreadable.kind == "unreadable"
    assert unreadable.group == ""
    assert "cannot read the group log of org/repo#41" in unreadable.detail
    assert "the author could not be read" in unreadable.detail


def test_group_read_failure_is_that_groups_unreadable(world: _World) -> None:
    # G1's closing PRs cannot be read in full; G2 is still checked and splits.
    world.add_group("G1", 1, 1, 2)
    world.add_group("G2", 1, 3, 4)
    world.states.update({ref(3): "CLOSED", ref(4): "CLOSED"})
    world.closers.update({ref(3): (_pr(10),), ref(4): (_pr(11),)})
    world.closer_errors[ref(1)] = closers.ClosersReadError("the closing PRs cannot be read")
    unreadable, split = _scan()
    assert (unreadable.group, unreadable.kind) == ("G1", "unreadable")
    assert "cannot check group G1 of org/repo#40" in unreadable.detail
    assert (split.group, split.kind) == ("G2", "split")


@pytest.mark.parametrize("where", ["log", "closers"])
def test_missing_token_propagates(world: _World, where: str) -> None:
    # A missing token is the CLI's configuration fix, never a finding.
    token = github.MissingGitHubTokenError(1, "gh", "", "no token")
    world.add_group("G1", 1, 1)
    if where == "log":
        world.fail[EPIC] = token
    else:
        world.closer_errors[ref(1)] = token
    with pytest.raises(github.MissingGitHubTokenError):
        _scan()


def test_findings_in_epic_group_and_kind_order(world: _World) -> None:
    # G1 (edited), G2 (closed unmerged) and G10 (split): the group ID order is numeric.
    world.broken["G1"] = record.GroupRecordError(
        "org/repo#40 group G1 cannot be read: comment 3 was edited after it was posted",
        edits=("comment 3 was edited after it was posted",),
        unreadable=False,
    )
    world.add_group("G2", 1, 1, 2)
    world.add_group("G10", 1, 5, 6)
    world.states.update({ref(n): "CLOSED" for n in (1, 2, 5, 6)})
    world.closers.update({ref(2): (_pr(10),), ref(5): (_pr(10),), ref(6): (_pr(11),)})
    assert [(f.group, f.kind) for f in _scan()] == [
        ("G1", "edited"),
        ("G2", "closed unmerged"),
        ("G10", "split"),
    ]


def test_group_findings_defaults_to_the_org_home(world: _World) -> None:
    # With no home, listing or authors given, the org's home, its listing and its trusted
    # authors are read from the org.
    with (
        patch("vergil_tooling.lib.epics.require_org_home", return_value="org/.github") as home,
        patch(
            "vergil_tooling.lib.epic_audit.list_home_epics", return_value=[_listed(40)]
        ) as listing,
        patch(f"{_MOD}.load_trusted_authors", return_value=_AUTHORS) as authors,
    ):
        assert audit.group_findings("org") == []
    home.assert_called_once_with("org")
    listing.assert_called_once_with("org/.github")
    authors.assert_called_once_with()


def test_render_findings_names_each_finding() -> None:
    findings = [
        audit.GroupFinding(EPIC, "G1", "split", "members closed by different merged PRs: x"),
        audit.GroupFinding(EPIC, "", "unreadable", "cannot read the group log"),
    ]
    assert audit.render_findings(findings) == (
        "\n## Release groups\n\n"
        "- org/repo#40 G1 (split): members closed by different merged PRs: x\n"
        "- org/repo#40 (unreadable): cannot read the group log"
    )
