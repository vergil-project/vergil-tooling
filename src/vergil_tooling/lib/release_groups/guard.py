"""The refusal of a move, route or transfer of a grouped task (epic vergil-project/.github#1672).

A grouped task stays where its group is (spec §5.1). Each command that would move a task
out of its epic, re-home it through the intake routes, or transfer its issue asks
:func:`refusal` first, before any write, and refuses when it returns a reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.release_groups import record

if TYPE_CHECKING:
    from vergil_tooling.lib import epics


def refusal(task: epics.IssueRef, *, doing: str) -> str | None:
    """Why *doing* (``move``, ``route`` or ``transfer``) must not run on *task*, or ``None``.

    A task in a live release group is refused, naming the group and its epic. A group read
    that fails (:class:`record.GroupRecordError`) is refused too: a task whose group cannot
    be read is not known to be free to move, so it is not moved.
    """
    try:
        groups = record.groups_of(task)
    except record.GroupRecordError as exc:
        return (
            f"refusing to {doing} {task.slug}: its release group cannot be read ({exc}). "
            "A grouped task stays where its group is, so nothing moves until the group reads"
        )
    if not groups:
        return None
    group = groups[0]
    return (
        f"refusing to {doing} {task.slug}: it is in release group {group.group} of epic "
        f"{group.epic.slug}. A grouped task stays where its group is: a recorded change "
        "(vrg-release-group set or dissolve) must remove the task first"
    )
