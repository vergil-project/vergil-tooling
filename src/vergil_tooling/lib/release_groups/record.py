"""The release-group record: its grammar, and the reads that trust it.

Epic vergil-project/.github#1672, phase 1 Task 2 (spec §5.1, §5.2, §5.9). A release group
is a set of tasks of one epic that ship as one batch. Its membership is recorded in
comments on the epic (the chain's head): ``release-group <id> rev <n>: <members>`` or
``release-group <id> rev <n>: dissolved``, with an optional ``Reason:`` line. The latest
record of a group (the highest revision) is in force.

Only the ``human`` and ``agents`` roles are trusted to write a record. A failure is scoped
to its group where its comment names one, and refuses every read of the epic where it names
none; a refusal always names the epic and the comments involved. ``read_log`` is the reader
and ``groups_of`` answers which live groups hold a task. Nothing here writes to GitHub.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import epic_audit, epics, github, proxies, trust

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from vergil_tooling.lib.trusted_authors import TrustedAuthors

GROUP_ID_RE = re.compile(r"G[1-9][0-9]*")
RECORD_PREFIX = "release-group "
_REASON_PREFIX = "Reason:"
_ROLES = frozenset({trust.Role.HUMAN, trust.Role.AGENTS})
_MEMBER = r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9][0-9]*"
_MEMBER_RE = re.compile(
    r"(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+)#(?P<number>[1-9][0-9]*)"
)
_FIRST_LINE_RE = re.compile(
    rf"release-group (?P<group>{GROUP_ID_RE.pattern}) rev (?P<rev>[1-9][0-9]*): "
    rf"(?P<rest>dissolved|{_MEMBER}(?: {_MEMBER})*)"
)


class GroupRecordError(Exception):
    """A refusal of a group read: it names the epic, the group when one is named, and why.

    *edits* holds each problem that is an outsider's edit of a trusted comment, each naming
    its comment; it is empty when the refusal holds none. *unreadable* is True when the
    refusal holds a problem that is not an edit (a malformed or conflicting record, or a
    comment the epic cannot be read for). An edit alone is not an unreadable log.
    """

    def __init__(
        self, message: str, *, edits: tuple[str, ...] = (), unreadable: bool = True
    ) -> None:
        super().__init__(message)
        self.edits = edits
        self.unreadable = unreadable


@dataclass(frozen=True)
class ParsedRecord:
    """One record's first line, and its ``Reason:`` line when it has one."""

    group: str
    rev: int
    members: tuple[epics.IssueRef, ...]
    dissolved: bool
    reason: str | None


@dataclass(frozen=True)
class Group:
    """A group's latest record, read from *comment_id* on *epic*."""

    epic: epics.IssueRef
    group: str
    rev: int
    members: tuple[epics.IssueRef, ...]
    dissolved: bool
    comment_id: str


@dataclass(frozen=True)
class GroupLog:
    """Every group an epic records, and what refuses each.

    *groups* holds each readable group, dissolved ones included. *broken* holds the groups
    whose reads refuse, by ID. *epic_error* is a failure that names no group, and refuses
    every read of the epic. *ids_used* holds every group ID the epic has used, broken
    groups' included.
    """

    epic: epics.IssueRef
    groups: Mapping[str, Group]
    broken: Mapping[str, GroupRecordError]
    epic_error: GroupRecordError | None
    ids_used: frozenset[str]


@dataclass(frozen=True)
class _Entry:
    """One trusted record comment: its parse, or the problem that stops it parsing."""

    comment_id: str
    parsed: ParsedRecord | None
    edited: bool
    group: str | None
    problem: str | None


@dataclass(frozen=True)
class _Read:
    """A read's log, and the readable records of each group (any revision, broken or not)."""

    log: GroupLog
    holds: Mapping[str, tuple[ParsedRecord, ...]]


def parse_record(body: str) -> ParsedRecord | None:
    """The record *body* holds, or ``None`` when its first line does not start ``release-group``.

    Raises :class:`GroupRecordError` when the first line starts so but is malformed, or
    when the record has more than one ``Reason:`` line. Other lines are not parsed.
    """
    lines = body.splitlines()
    first = lines[0] if lines else ""
    if not first.startswith(RECORD_PREFIX):
        return None
    match = _FIRST_LINE_RE.fullmatch(first)
    if match is None:
        raise GroupRecordError("its first line does not match the release-group grammar")
    dissolved = match.group("rest") == "dissolved"
    members = () if dissolved else _members(match.group("rest"))
    reasons = [
        line[len(_REASON_PREFIX) :].strip() for line in lines[1:] if line.startswith(_REASON_PREFIX)
    ]
    if len(reasons) > 1:
        raise GroupRecordError("it has more than one Reason line")
    return ParsedRecord(
        group=match.group("group"),
        rev=int(match.group("rev")),
        members=members,
        dissolved=dissolved,
        reason=(reasons[0] or None) if reasons else None,
    )


def _members(text: str) -> tuple[epics.IssueRef, ...]:
    """The members of a grammar-checked member list, each once."""
    refs = tuple(
        epics.IssueRef(owner=m.group("owner"), repo=m.group("repo"), number=int(m.group("number")))
        for m in _MEMBER_RE.finditer(text)
    )
    if len(set(refs)) != len(refs):
        raise GroupRecordError("a member is listed more than once")
    return refs


def render_record(
    group: str,
    rev: int,
    members: Sequence[epics.IssueRef],
    *,
    dissolved: bool = False,
    reason: str | None = None,
) -> str:
    """The text of a record that :func:`parse_record` reads back as given.

    Raises ``ValueError`` for an input the grammar cannot hold.
    """
    if GROUP_ID_RE.fullmatch(group) is None:
        raise ValueError(f"not a group ID: {group!r}")
    if rev < 1:
        raise ValueError(f"a revision is a positive integer, not {rev}")
    if dissolved and members:
        raise ValueError(f"{group} is dissolved and names no members")
    if not dissolved and not members:
        raise ValueError(f"{group} names no members")
    if len(set(members)) != len(members):
        raise ValueError(f"{group} lists a member more than once")
    lines = [
        f"{RECORD_PREFIX}{group} rev {rev}: "
        + ("dissolved" if dissolved else " ".join(m.slug for m in members))
    ]
    if reason is not None:
        stripped = reason.strip()
        if len(stripped.splitlines()) != 1:
            raise ValueError("a reason is one non-empty line")
        lines.append(f"{_REASON_PREFIX} {stripped}")
    return "\n".join(lines)


def in_id_order(groups: Iterable[Group]) -> list[Group]:
    """*groups* sorted by group ID number (G2 before G10)."""
    return sorted(groups, key=lambda g: int(g.group[1:]))


def read_log(epic: epics.IssueRef, *, authors: TrustedAuthors | None = None) -> GroupLog:
    """The group log of *epic*, read from its chain's head.

    Raises only on a read failure; a malformed, edited or conflicting record goes into the
    log as a refusal. Skipped comments are reported on stderr as metadata only.
    """
    return _read(epic, authors).log


def groups_of(task: epics.IssueRef, *, authors: TrustedAuthors | None = None) -> list[Group]:
    """The live groups of *task*'s epic that hold *task*, in ID order.

    Raises :class:`GroupRecordError` when the epic refuses every read, when a broken group
    may hold *task* (it names *task* in a readable record of the group, or it has none), or
    when *task* is in more than one live group.
    """
    with github.target_org(task.owner):
        found = epics.task_epic(task)
    if found.epic is None:
        return []
    read = _read(found.epic, authors)
    log = read.log
    if log.epic_error is not None:
        raise log.epic_error
    for group_id in sorted(log.broken, key=lambda g: int(g[1:])):
        if _may_hold(task, read.holds.get(group_id, ())):
            raise log.broken[group_id]
    live = [g for g in in_id_order(log.groups.values()) if not g.dissolved and task in g.members]
    if len(live) > 1:
        names = ", ".join(g.group for g in live)
        comments = ", ".join(f"comment {g.comment_id}" for g in live)
        raise GroupRecordError(
            f"{log.epic.slug}: {task.slug} is a member of more than one live group ({names}),"
            f" from {comments}"
        )
    return live


def _may_hold(task: epics.IssueRef, records: Sequence[ParsedRecord]) -> bool:
    """True when a broken group may hold *task*: no readable record, or one names it."""
    return not records or any(task in record.members for record in records)


def _read(epic: epics.IssueRef, authors: TrustedAuthors | None) -> _Read:
    """Read *epic*'s group log and the readable records of each group."""
    with github.target_org(epic.owner):
        head, kind = epics.epic_kind(epic)
        stream = trust.read_comment_stream(
            f"{head.owner}/{head.repo}", head.number, roles=_ROLES, authors=authors
        )
    trust.report_skipped(stream.skipped, stream=sys.stderr)
    entries = [e for e in (_entry(c) for c in stream.comments) if e is not None]

    edit_problems = [_edit_problem(s) for s in stream.skipped if s.kind == "edit"]
    problems: list[str] = []
    if entries:
        reason = _not_group_epic(head, kind)
        if reason is not None:
            ids = ", ".join(f"comment {e.comment_id}" for e in entries)
            problems.append(f"it holds release-group records ({ids}) but {reason}")
    problems.extend(
        f"comment {e.comment_id} is malformed with no readable group ID ({e.problem})"
        for e in entries
        if e.group is None
    )
    if edit_problems or problems:
        error = GroupRecordError(
            f"{head.slug} cannot be read: " + "; ".join(edit_problems + problems),
            edits=tuple(edit_problems),
            unreadable=bool(problems),
        )
        return _Read(GroupLog(head, {}, {}, error, frozenset()), {})

    # Every entry is scoped here: an unscoped one is a problem of the epic, refused above.
    by_group: dict[str, list[_Entry]] = {}
    for group_id, entry in [(e.group, e) for e in entries if e.group is not None]:
        by_group.setdefault(group_id, []).append(entry)
    groups: dict[str, Group] = {}
    broken: dict[str, GroupRecordError] = {}
    holds: dict[str, tuple[ParsedRecord, ...]] = {}
    for group_id, group_entries in by_group.items():
        holds[group_id] = tuple(e.parsed for e in group_entries if e.parsed is not None)
        group_problems = _group_problems(group_entries)
        if group_problems:
            detail = "; ".join(text for text, _ in group_problems)
            ids = ", ".join(f"comment {e.comment_id}" for e in group_entries)
            broken[group_id] = GroupRecordError(
                f"{head.slug} group {group_id} cannot be read: {detail} (records: {ids})",
                edits=tuple(text for text, is_edit in group_problems if is_edit),
                unreadable=any(not is_edit for _, is_edit in group_problems),
            )
            continue
        parsed = [(e.comment_id, e.parsed) for e in group_entries if e.parsed is not None]
        comment_id, latest = parsed[-1]
        groups[group_id] = Group(
            epic=head,
            group=group_id,
            rev=latest.rev,
            members=latest.members,
            dissolved=latest.dissolved,
            comment_id=comment_id,
        )
    log = GroupLog(head, groups, broken, None, frozenset(by_group))
    return _Read(log, holds)


def _entry(comment: trust.StreamComment) -> _Entry | None:
    """The record a trusted comment holds, or ``None`` when its first line is not one."""
    first = comment.body.splitlines()
    first_line = first[0] if first else ""
    try:
        parsed = parse_record(comment.body)
    except GroupRecordError as exc:
        return _Entry(comment.id, None, comment.edited, _readable_group(first_line), str(exc))
    if parsed is None:
        return None
    return _Entry(comment.id, parsed, comment.edited, parsed.group, None)


def _readable_group(first_line: str) -> str | None:
    """The group ID a malformed first line names, when it is a readable one."""
    parts = first_line.split(" ")
    if len(parts) > 1 and parts[0] == "release-group" and GROUP_ID_RE.fullmatch(parts[1]):
        return parts[1]
    return None


def _group_problems(entries: Sequence[_Entry]) -> list[tuple[str, bool]]:
    """Why a group's records refuse its reads: each problem's text, and whether it is an edit.

    Each problem names its comments. An edited record is an edit; a malformed or conflicting
    one is not.
    """
    problems: list[tuple[str, bool]] = []
    for entry in entries:
        if entry.parsed is None:
            problems.append((f"comment {entry.comment_id} is malformed ({entry.problem})", False))
        if entry.edited:
            problems.append((f"comment {entry.comment_id} was edited after it was posted", True))
    parsed = [(e.comment_id, e.parsed) for e in entries if e.parsed is not None]
    top_id, top_rev = "", 0
    for comment_id, record in parsed:
        if top_rev and record.rev == top_rev:
            problems.append(
                (f"comment {top_id} and comment {comment_id} both set revision {record.rev}", False)
            )
        elif record.rev < top_rev:
            problems.append(
                (
                    f"comment {comment_id} sets revision {record.rev} after comment {top_id}"
                    f" set revision {top_rev}",
                    False,
                )
            )
        else:
            top_id, top_rev = comment_id, record.rev
    return problems


def _edit_problem(skip: trust.Skipped) -> str:
    """An outsider's edit of a trusted comment: it refuses every read of the epic."""
    return (
        f"{skip.field} was edited by an identity outside the human and agents roles"
        f" ({skip.describe()}, {skip.cause})"
    )


def _not_group_epic(head: epics.IssueRef, kind: str) -> str | None:
    """Why *head* is not a group's epic (a development epic or a proxy of one), else None."""
    if kind == epics.DEVELOPMENT_KIND:
        return None
    if kind != epics.PROXY_LABEL:
        return f"{head.slug} is {kind}, not a development epic or a proxy of one"
    problems = epic_audit.proxy_link_problems(head)
    if problems:
        return "its Primary link has problems: " + "; ".join(problems)
    try:
        primary = proxies.primary_of(head)
    except proxies.ProxyLinkError as exc:
        return f"its Primary link is broken: {exc}"
    with github.target_org(primary.owner):
        primary_kind = epics.epic_kind(primary)[1]
    if primary_kind != epics.DEVELOPMENT_KIND:
        return f"its Primary {primary.slug} is a {primary_kind} epic, not a development epic"
    return None
