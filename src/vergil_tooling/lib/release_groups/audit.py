"""When a release group is runnable: the group half of ``vrg-epic-audit --epic``.

Epic vergil-project/.github#1672, phase 3 Task 16 (spec §5.3). A live group is runnable
when every blocker of its members that lies outside the group is closed; a blocker between
two members never counts, since the members ship together. Blockers are the native
blocked-by relationships and the ``Blocked-by:`` lines of a trusted body alike.

A group that cannot be read refuses the whole read, never "no groups": the failure is a
:class:`~vergil_tooling.lib.release_groups.record.GroupRecordError` naming the epic and
the group. Nothing here writes to GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.release_groups import record

if TYPE_CHECKING:
    from vergil_tooling.lib.trusted_authors import TrustedAuthors


@dataclass(frozen=True)
class GroupStatus:
    """A live group of an epic, and whether it can run now.

    *blocked_by* holds the open blockers outside the group (sorted), empty exactly when
    *runnable*.
    """

    group: str
    rev: int
    members: tuple[epics.IssueRef, ...]
    runnable: bool
    blocked_by: tuple[epics.IssueRef, ...]


def _key(ref: epics.IssueRef) -> tuple[str, str, int]:
    return (ref.owner, ref.repo, ref.number)


def _open_blockers(member: epics.IssueRef) -> set[epics.IssueRef]:
    """Every issue not closed that *member* is blocked by, native and body alike."""
    with github.target_org(member.owner):
        states = {b.ref: b.state for b in epics.native_blockers_of(member)}
        for ref in epics.blockers_of(member):
            if ref not in states:
                states[ref] = epics.issue_state(ref)
    return {ref for ref, state in states.items() if state != "CLOSED"}


def group_status(
    epic: epics.IssueRef, *, authors: TrustedAuthors | None = None
) -> list[GroupStatus]:
    """The live groups of *epic*, in ID order, each with its open outside blockers.

    Raises :class:`~vergil_tooling.lib.release_groups.record.GroupRecordError` when the
    epic refuses every read or any of its groups cannot be read, and the read's own error
    when a member's blockers cannot be read in full.
    """
    log = record.read_log(epic, authors=authors)
    if log.epic_error is not None:
        raise log.epic_error
    if log.broken:
        first = min(log.broken, key=lambda g: int(g[1:]))
        raise log.broken[first]
    statuses: list[GroupStatus] = []
    for group in record.in_id_order(g for g in log.groups.values() if not g.dissolved):
        blockers: set[epics.IssueRef] = set()
        for member in group.members:
            blockers |= _open_blockers(member)
        outside = tuple(sorted(blockers - set(group.members), key=_key))
        statuses.append(GroupStatus(group.group, group.rev, group.members, not outside, outside))
    return statuses
