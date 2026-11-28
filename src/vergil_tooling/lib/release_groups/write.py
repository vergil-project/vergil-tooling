"""Write a release group's record: ``vrg-release-group set`` and ``dissolve``.

Epic vergil-project/.github#1672, phase 3 Task 12 (spec §5.1, §5.5). :func:`set_group` posts a
group's membership as its next revision on the epic's chain head, and :func:`dissolve_group`
posts the group dissolved. Each re-reads the log first and posts nothing when the latest record
already holds the request (a safe retry). Otherwise every problem is collected and the change is
refused with :class:`WriteRefusedError`, naming each problem and the human's recovery, before
anything is posted. A change must pass:

- the group log reads clean, with no conflict, edited or skipped record, and the epic is open;
- a new ID is unused, and a dissolved ID is refused;
- a reason is given, written as the record's ``Reason:`` line;
- the command runs from the main clone of the members' repo, since batch markers are local;
- the group rules hold for the requested membership. The rules that need an open member
  (open, and the dependency path) are not applied to a closed member of the latest record;
- every incoming task is open, holds nothing (bar, with ``--at-batch-stop``, the marker of the
  group's own batch), and is in no other live group;
- the group is not frozen (no open member has an open PR or a handed-off batch), and is not
  started (no member has a batch marker) unless ``--at-batch-stop`` is given.

The holder reads (open PRs, handed-off batches, markers) are this clone's. Nothing else is written.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, git, github, issue_body
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_groups import record, rules

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from vergil_tooling.lib.release_groups.rules import Holder

# The errors a read of the log, the state, the rules or the holders can end in: each refuses
# the change, never passes it.
_READ_ERRORS: tuple[type[Exception], ...] = (
    *rules.READ_ERRORS,
    rules.GroupRuleReadError,
    epics.AmbiguousEpicCategoryError,
)
# The rules that need an open member. A closed member of the latest record is exempt from them.
_OPEN_ONLY_RULES = frozenset({"open code task", "dependency path"})
# The holders that freeze a group while an open member has one.
_FROZEN_KINDS = frozenset({"open PR", "handed-off batch"})


class WriteRefusedError(Exception):
    """A refusal of a change, or a failed post. *messages* holds each problem, one line each."""

    def __init__(self, messages: Sequence[str]) -> None:
        super().__init__("\n".join(messages))
        self.messages = tuple(messages)


@dataclass(frozen=True)
class WriteResult:
    """The outcome of a change: whether a record was posted, and the group's revision it holds."""

    posted: bool
    rev: int


def set_group(
    epic: epics.IssueRef,
    group: str,
    members: Sequence[epics.IssueRef],
    *,
    reason: str | None,
    at_batch_stop: bool,
    main_root: Path,
) -> WriteResult:
    """Record *group* of *epic* with *members*, or post nothing when its latest record holds them.

    A new group is posted at revision 1; an existing one at its latest revision plus one.
    Raises :class:`WriteRefusedError` for any refusal or failed post (see the module
    docstring). *main_root* is the main clone of the members' repo.
    """
    if not members:
        raise WriteRefusedError([f"{group} names no members: a group names at least one member"])
    return _change(
        epic,
        group,
        tuple(members),
        dissolved=False,
        reason=reason,
        at_batch_stop=at_batch_stop,
        main_root=main_root,
    )


def dissolve_group(
    epic: epics.IssueRef,
    group: str,
    *,
    reason: str | None,
    at_batch_stop: bool,
    main_root: Path,
) -> WriteResult:
    """Record *group* of *epic* as dissolved, or post nothing when its latest record already is.

    Raises :class:`WriteRefusedError` for any refusal or failed post. *main_root* is the
    main clone of the group's members' repo.
    """
    return _change(
        epic,
        group,
        (),
        dissolved=True,
        reason=reason,
        at_batch_stop=at_batch_stop,
        main_root=main_root,
    )


def _change(
    epic: epics.IssueRef,
    group: str,
    members: tuple[epics.IssueRef, ...],
    *,
    dissolved: bool,
    reason: str | None,
    at_batch_stop: bool,
    main_root: Path,
) -> WriteResult:
    """The one write path of :func:`set_group` and :func:`dissolve_group`."""
    if record.GROUP_ID_RE.fullmatch(group) is None:
        raise WriteRefusedError([f"not a group ID (G1, G2, ...): {group!r}"])
    if len({_key(m) for m in members}) != len(members):
        raise WriteRefusedError([f"{group} lists a member more than once"])
    log = _read("the group log of " + epic.slug, partial(record.read_log, epic))
    head = log.epic
    _refuse_closed(head)
    _refuse_unreadable(log)
    latest = log.groups.get(group)
    if latest is None and dissolved:
        raise WriteRefusedError(
            [f"{group} is not a group of {head.slug}: there is nothing to dissolve"]
        )
    if latest is not None and latest.dissolved and not dissolved:
        raise WriteRefusedError(
            [
                f"{group} was dissolved (comment {latest.comment_id} on {head.slug}), and a"
                " dissolved ID is never reused: use a new group ID"
            ]
        )
    if latest is not None and _recorded(latest, members, dissolved):
        return WriteResult(posted=False, rev=latest.rev)
    if reason is None or not reason.strip():
        raise WriteRefusedError(
            [f"a change to {group} needs a reason (--reason): say why the group changes"]
        )
    rev = 1 if latest is None else latest.rev + 1
    try:
        body = record.render_record(group, rev, members, dissolved=dissolved, reason=reason)
    except ValueError as exc:
        raise WriteRefusedError([f"the record cannot be written: {exc}"]) from exc

    existing = () if latest is None or latest.dissolved else latest.members
    _check_clone(main_root, (*members, *existing))
    incoming = () if dissolved else _incoming(members, existing)
    states = {ref: _state(ref) for ref in (*existing, *incoming)}
    refusals: list[str] = []
    if not dissolved:
        closed = frozenset(_key(m) for m in existing if states[m] != "OPEN")
        refusals += _rule_refusals(head, group, members, closed)
        refusals += [msg for m in incoming if (msg := _other_live_group(log, group, m)) is not None]
    holds = {ref: _holders(ref, main_root) for ref in (*existing, *incoming)}
    refusals += _holder_refusals(group, existing, incoming, states, holds, at_batch_stop)
    if refusals:
        raise WriteRefusedError(refusals)
    _post(head, group, rev, body)
    return WriteResult(posted=True, rev=rev)


def _key(ref: epics.IssueRef) -> tuple[str, str, int]:
    """*ref* as a key that ignores the case of its owner and repo (GitHub names ignore it)."""
    return (ref.owner.casefold(), ref.repo.casefold(), ref.number)


def _recorded(latest: record.Group, members: Sequence[epics.IssueRef], dissolved: bool) -> bool:
    """True when *latest* already holds the request: dissolved, or the same membership."""
    if dissolved:
        return latest.dissolved
    if latest.dissolved:
        return False
    return {_key(m) for m in latest.members} == {_key(m) for m in members}


def _incoming(
    members: Sequence[epics.IssueRef], existing: Sequence[epics.IssueRef]
) -> tuple[epics.IssueRef, ...]:
    """The requested members that the latest record does not already hold."""
    have = {_key(m) for m in existing}
    return tuple(m for m in members if _key(m) not in have)


def _read[T](what: str, read: Callable[[], T]) -> T:
    """Run one read; a failure refuses the change, naming *what* and that a human must look."""
    try:
        return read()
    except github.MissingGitHubTokenError:
        raise
    except _READ_ERRORS as exc:
        raise WriteRefusedError(
            [f"could not read {what}: {error_text(exc)}; a human must look at it before a change"]
        ) from exc


def _state(ref: epics.IssueRef) -> str:
    """*ref*'s state, ``OPEN`` or ``CLOSED``, read with the App token of its own org."""
    with github.target_org(ref.owner):
        return _read(f"the state of {ref.slug}", partial(epics.issue_state, ref))


def _holders(ref: epics.IssueRef, main_root: Path) -> list[Holder]:
    """What holds *ref* back from a change, read from this clone."""
    with github.target_org(ref.owner):
        return _read(f"what holds {ref.slug}", partial(rules.holders, ref, main_root=main_root))


def _refuse_closed(head: epics.IssueRef) -> None:
    """A group on a closed epic is never changed."""
    if _state(head) != "OPEN":
        raise WriteRefusedError(
            [
                f"{head.slug} is closed: a group on a closed epic is never changed, and closed"
                " epics are not reopened by the tooling"
            ]
        )


def _refuse_unreadable(log: record.GroupLog) -> None:
    """Refuse on a log that refuses any read: an epic-wide refusal, or any broken group."""
    if log.epic_error is not None:
        raise WriteRefusedError([_recovery(str(log.epic_error), log.epic)])
    if log.broken:
        ordered = sorted(log.broken.items(), key=lambda item: int(item[0][1:]))
        raise WriteRefusedError([_recovery(str(error), log.epic) for _, error in ordered])


def _recovery(message: str, epic: epics.IssueRef) -> str:
    """A log refusal with the human's recovery: the comments it names, read and fixed by hand."""
    return (
        f"{message}. To recover, read the comments named above on {epic.slug} in the web UI"
        " and delete the bad one, then rerun the command"
    )


def _check_clone(main_root: Path, refs: Sequence[epics.IssueRef]) -> None:
    """Refuse unless this clone's origin is the repo of every member in *refs*."""
    try:
        url = git.remote_url("origin", repo=main_root)
    except (subprocess.CalledProcessError, OSError) as exc:
        raise WriteRefusedError(
            [
                "cannot tell which repo this clone is: its origin remote cannot be read"
                f" ({error_text(exc)})"
            ]
        ) from exc
    clone = github.github_remote_repo(url)
    if clone is None:
        raise WriteRefusedError(
            ["cannot tell which repo this clone is: its origin remote is not a github.com repo"]
        )
    if all(f"{r.owner}/{r.repo}".casefold() == clone.casefold() for r in refs):
        return
    repos = sorted({f"{r.owner}/{r.repo}" for r in refs}, key=str.casefold)
    raise WriteRefusedError(
        [
            f"run this from the main clone of {', '.join(repos)}, the members' repo;"
            f" this clone is {clone}"
        ]
    )


def _rule_refusals(
    head: epics.IssueRef,
    group: str,
    members: Sequence[epics.IssueRef],
    closed: frozenset[tuple[str, str, int]],
) -> list[str]:
    """The group rules the requested membership breaks, less a closed member's open-only breaks."""
    breaks = _read(
        f"the group rules of {group}",
        partial(rules.check_group_rules, head, group, members),
    )
    refusals: list[str] = []
    for rule_break in breaks:
        exempt = (
            rule_break.member is not None
            and _key(rule_break.member) in closed
            and rule_break.rule in _OPEN_ONLY_RULES
        )
        if exempt:
            continue
        refusals.append(
            f"{group} would break the group rule {rule_break.rule}: {rule_break.detail}"
        )
    return refusals


def _other_live_group(log: record.GroupLog, group: str, task: epics.IssueRef) -> str | None:
    """A refusal when *task* is a member of a live group other than *group*, else ``None``."""
    for other in record.in_id_order(log.groups.values()):
        if other.group == group or other.dissolved:
            continue
        if any(_key(m) == _key(task) for m in other.members):
            return (
                f"{task.slug} is a member of live group {other.group} (comment"
                f" {other.comment_id}): take it out of {other.group} first, by setting or"
                " dissolving that group, so no task is in two live groups"
            )
    return None


def _holder_refusals(
    group: str,
    existing: Sequence[epics.IssueRef],
    incoming: Sequence[epics.IssueRef],
    states: dict[epics.IssueRef, str],
    holds: dict[epics.IssueRef, list[Holder]],
    at_batch_stop: bool,
) -> list[str]:
    """The frozen, incoming and started refusals of a change, from each task's holders."""
    started = sorted({h.where for m in existing for h in holds[m] if h.kind == "batch marker"})
    refusals: list[str] = []
    for member in existing:
        if states[member] != "OPEN":
            continue
        for holder in holds[member]:
            if holder.kind in _FROZEN_KINDS:
                refusals.append(
                    f"{group} is frozen while {member.slug} is in {holder.kind} {holder.where}:"
                    " take it back first with vrg-pr-workflow unfreeze, or close the PR"
                )
    for member in incoming:
        found = holds[member]
        if _own_batch_marker(found, started, at_batch_stop):
            continue
        for holder in found:
            refusals.append(_incoming_refusal(group, member, holder))
    if started and not at_batch_stop:
        for where in started:
            refusals.append(
                f"{group} is started: its batch {where} has a batch marker, so a change stops"
                " for the human; rerun with --at-batch-stop to make it"
            )
    return refusals


def _own_batch_marker(found: list[Holder], started: list[str], at_batch_stop: bool) -> bool:
    """True when the only holder of an incoming task is a marker of the group's own batch."""
    if not at_batch_stop or len(found) != 1:
        return False
    only = found[0]
    return only.kind == "batch marker" and only.where in started


def _incoming_refusal(group: str, member: epics.IssueRef, holder: Holder) -> str:
    """Why an incoming task cannot join *group*, for one holder it has."""
    if holder.kind == "batch marker":
        return (
            f"{member.slug} is held by the batch marker of {holder.where}, which is not the"
            f" batch of {group}'s members, so it cannot join {group}"
        )
    return (
        f"{member.slug} cannot join {group}: it is in {holder.kind} {holder.where}; take it"
        " back first (vrg-pr-workflow unfreeze, or close the PR)"
    )


def _post(head: epics.IssueRef, group: str, rev: int, body: str) -> None:
    """Post *body* on the chain head *head*, once. A failed post names what may have landed."""
    try:
        issue_body.post_comment(head.owner, head.repo, head.number, body)
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError as exc:
        raise WriteRefusedError(
            [
                f"could not post {group} rev {rev} on {head.slug}: {error_text(exc)}. The post"
                f" may have landed: read the comments on {head.slug} before rerunning, since a"
                " rerun of the same change posts nothing once the record is there"
            ]
        ) from exc
