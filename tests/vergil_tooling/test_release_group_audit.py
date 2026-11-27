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

from vergil_tooling.lib import epics
from vergil_tooling.lib.epics import BlockerState, IssueRef
from vergil_tooling.lib.release_groups import audit, record

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.release_groups.audit"
EPIC = IssueRef("org", "repo", 40)


def ref(number: int) -> IssueRef:
    """A task of the test epic's repo."""
    return IssueRef("org", "repo", number)


@dataclass
class _World:
    """The stubbed reads: the epic's group log, and each member's blockers and their states."""

    groups: dict[str, record.Group] = field(default_factory=dict)
    broken: dict[str, record.GroupRecordError] = field(default_factory=dict)
    epic_error: record.GroupRecordError | None = None
    native: dict[IssueRef, tuple[BlockerState, ...]] = field(default_factory=dict)
    body: dict[IssueRef, list[IssueRef]] = field(default_factory=dict)
    states: dict[IssueRef, str] = field(default_factory=dict)

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
        """The group log, as :func:`record.read_log` returns it."""
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
        patch("vergil_tooling.lib.epics.issue_state", side_effect=lambda r: w.states[r]),
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
    # Outside blockers come back sorted by (owner, repo, number), whatever order they were read in.
    world.add_group("G1", 1, 1)
    other = IssueRef("other", "lib", 2)
    world.native[ref(1)] = (_blocker(9, "OPEN"), BlockerState(other, "OPEN"))
    world.body[ref(1)] = [ref(3)]
    world.states[ref(3)] = "OPEN"
    [status] = audit.group_status(EPIC)
    assert status.blocked_by == (other, ref(3), ref(9))


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
