"""The release-group audit: when a group runs, and what is wrong with the live groups.

Epic vergil-project/.github#1672, phase 3 Task 16 (spec §5.3) and Task 17 (spec §5.6).
:func:`group_status` says when a live group is runnable: every blocker of its members that
lies outside the group is closed; a blocker between two members never counts, since the
members ship together. Blockers are the native blocked-by relationships and the
``Blocked-by:`` lines of a trusted body alike.

:func:`group_findings` audits the live groups of every open development and proxy epic of an
org's home, and repairs nothing. It reports a ``split`` (members closed by different merged
PRs), a ``closed unmerged`` member (no merged PR closes it), a ``rule`` break (an open member
breaks a group rule, the dependency-path rule above all), an ``edited`` record or skipped
edit, and an ``unreadable`` log, group or epic (one whose kind or state cannot be
read). An open member whose merged PR also closed the others
is drift for the drift-close pass, never a finding. :func:`render_findings` prints them.

A group that cannot be read refuses the whole read, never "no groups": the failure is a
:class:`~vergil_tooling.lib.release_groups.record.GroupRecordError` naming the epic and the
group. Nothing here writes to GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from vergil_tooling.lib import epic_audit, epics, github
from vergil_tooling.lib.pending_release import PendingReleaseError
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_groups import closers, record, rules
from vergil_tooling.lib.trusted_authors import load_trusted_authors

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vergil_tooling.lib.epic_docs import EpicRecord
    from vergil_tooling.lib.trusted_authors import TrustedAuthors

# The errors a read of one epic's or one group's state can end in. Each one refuses that read
# alone, as an unreadable finding; a missing GitHub token is re-raised before this check.
_READ_ERRORS: tuple[type[Exception], ...] = (
    *rules.READ_ERRORS,
    rules.GroupRuleReadError,
    closers.ClosersReadError,
    PendingReleaseError,
    epics.AmbiguousEpicCategoryError,
)
# The epic kinds a release group may live under: a development epic, or a proxy of one.
_GROUP_EPIC_KINDS = (epics.DEVELOPMENT_KIND, epics.PROXY_LABEL)
# The order of the finding kinds, as the spec lists them.
_KINDS: tuple[str, ...] = ("split", "closed unmerged", "rule", "edited", "unreadable")


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


@dataclass(frozen=True)
class GroupFinding:
    """One thing wrong with a live group of an epic, or with the epic's group log.

    *group* is the group ID, or ``""`` for a finding about the epic as a whole. *detail* names
    the members and their PRs, the rule broken, or the record or log that cannot be read.
    """

    epic: epics.IssueRef
    group: str
    kind: Literal["split", "closed unmerged", "rule", "edited", "unreadable"]
    detail: str


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


def group_findings(
    org: str,
    *,
    home: str | None = None,
    listing: Sequence[EpicRecord] | None = None,
    authors: TrustedAuthors | None = None,
) -> list[GroupFinding]:
    """The findings of the live release groups of every open development and proxy epic of *org*.

    *home* defaults to the org's ``<org>/.github`` home and *listing* to its epic listing;
    *authors* to the configured trusted authors. A closed epic is never checked. A read that
    fails for one epic or one group becomes an ``unreadable`` finding naming it, and the rest
    are still checked; a missing GitHub token propagates. Nothing is written. The findings come
    in epic, group ID and kind order.
    """
    with github.target_org(org):
        home_slug = epics.require_org_home(org) if home is None else home
        epic_records = epic_audit.list_home_epics(home_slug) if listing is None else listing
        trusted = load_trusted_authors() if authors is None else authors
        findings: list[GroupFinding] = []
        heads, findings = _open_heads(home_slug, epic_records)
        for head in heads:
            findings += _epic_findings(head, trusted)
    return sorted(findings, key=_order)


def _open_heads(
    home: str, listing: Sequence[EpicRecord]
) -> tuple[list[epics.IssueRef], list[GroupFinding]]:
    """The open development or proxy chain heads of *home*'s listing, each once, by number.

    A continuation resolves to its chain's head, so the head is reached once. The head's own
    state is read fresh, since the listing may predate a close earlier in the same run. A
    listed epic whose kind or state cannot be read (chain drift, a head with no single kind,
    a failed read) becomes an ``unreadable`` finding naming it, and the rest are still read;
    a missing GitHub token propagates. The findings come back beside the heads.
    """
    owner, _, repo = home.partition("/")
    seen: set[epics.IssueRef] = set()
    heads: list[epics.IssueRef] = []
    unreadable: list[GroupFinding] = []
    for epic in sorted(listing, key=lambda e: e["number"]):
        if epic["state"] != "OPEN":
            continue
        listed = epics.IssueRef(owner, repo, epic["number"])
        try:
            head, kind = epics.epic_kind(listed)
            if head in seen:
                continue
            seen.add(head)
            if kind in _GROUP_EPIC_KINDS and epics.issue_state(head) == "OPEN":
                heads.append(head)
        except github.MissingGitHubTokenError:
            raise
        except _READ_ERRORS as exc:
            cause = error_text(exc)
            detail = f"cannot read the kind or state of {listed.slug}: {cause}"
            unreadable.append(GroupFinding(listed, "", "unreadable", detail))
    return heads, unreadable


def _epic_findings(head: epics.IssueRef, authors: TrustedAuthors) -> list[GroupFinding]:
    """The findings of one open epic's group log and its live groups."""
    try:
        log = record.read_log(head, authors=authors)
    except github.MissingGitHubTokenError:
        raise
    except _READ_ERRORS as exc:
        cause = error_text(exc)
        detail = f"cannot read the group log of {head.slug}: {cause}"
        return [GroupFinding(head, "", "unreadable", detail)]
    if log.epic_error is not None:
        return _refusal_findings(head, "", log.epic_error)
    findings: list[GroupFinding] = []
    for group_id, refusal in log.broken.items():
        findings += _refusal_findings(head, group_id, refusal)
    for group in record.in_id_order(g for g in log.groups.values() if not g.dissolved):
        findings += _group_findings(head, group, authors)
    return findings


def _refusal_findings(
    head: epics.IssueRef, group: str, refusal: record.GroupRecordError
) -> list[GroupFinding]:
    """A refusal as findings: its edits as ``edited``, and its other problems as ``unreadable``."""
    found: list[GroupFinding] = []
    if refusal.edits:
        found.append(GroupFinding(head, group, "edited", "; ".join(refusal.edits)))
    if refusal.unreadable:
        found.append(GroupFinding(head, group, "unreadable", str(refusal)))
    return found


def _group_findings(
    head: epics.IssueRef, group: record.Group, authors: TrustedAuthors
) -> list[GroupFinding]:
    """The findings of one live group; a read that fails becomes that group's unreadable finding."""
    try:
        return _check_group(head, group, authors)
    except github.MissingGitHubTokenError:
        raise
    except _READ_ERRORS as exc:
        cause = error_text(exc)
        detail = f"cannot check group {group.group} of {head.slug}: {cause}"
        return [GroupFinding(head, group.group, "unreadable", detail)]


def _check_group(
    head: epics.IssueRef, group: record.Group, authors: TrustedAuthors
) -> list[GroupFinding]:
    """The closer findings of *group*, then its rule breaks when it has an open member."""
    members = group.members
    states = {m: _in_org(m, epics.issue_state) for m in members}
    closed_by = {m: _in_org(m, partial(closers.merged_closers, authors=authors)) for m in members}
    found = _closer_findings(head, group.group, members, states, closed_by)
    open_members = frozenset(m for m in members if states[m] == "OPEN")
    if open_members:
        for rule_break in rules.check_group_rules(head, group.group, members):
            # A closed member is already shipped: only an open member's break is a finding.
            if rule_break.member is None or rule_break.member in open_members:
                detail = f"{rule_break.rule}: {rule_break.detail}"
                found.append(GroupFinding(head, group.group, "rule", detail))
    return found


def _closer_findings(
    head: epics.IssueRef,
    group: str,
    members: Sequence[epics.IssueRef],
    states: dict[epics.IssueRef, str],
    closed_by: dict[epics.IssueRef, tuple[epics.IssueRef, ...]],
) -> list[GroupFinding]:
    """``closed unmerged`` for each closed member no merged PR closes, and ``split`` when the
    members' merged closing PRs are more than one.

    An open member's merged PR counts toward the split too: when it is the PR that closed the
    others, there is one PR and no split (the drift-close pass closes the open member).
    """
    found: list[GroupFinding] = []
    for member in members:
        if states[member] != "OPEN" and not closed_by[member]:
            detail = f"{member.slug} is closed, but no merged PR closes it"
            found.append(GroupFinding(head, group, "closed unmerged", detail))
    if len({pr for prs in closed_by.values() for pr in prs}) > 1:
        parts = []
        for member in members:
            if closed_by[member]:
                state = "open" if states[member] == "OPEN" else "closed"
                prs = ", ".join(pr.slug for pr in closed_by[member])
                parts.append(f"{member.slug} ({state}) by {prs}")
        detail = "members closed by different merged PRs: " + "; ".join(parts)
        found.append(GroupFinding(head, group, "split", detail))
    return found


def _in_org[T](ref: epics.IssueRef, read: Callable[[epics.IssueRef], T]) -> T:
    """*read* of *ref*, with the App token scoped to *ref*'s own org."""
    with github.target_org(ref.owner):
        return read(ref)


def _order(finding: GroupFinding) -> tuple[int, int, int, str]:
    """Epic number, group ID number (epic-wide findings first), kind order, then detail."""
    group_number = int(finding.group[1:]) if finding.group else 0
    return (finding.epic.number, group_number, _KINDS.index(finding.kind), finding.detail)


def render_findings(findings: Sequence[GroupFinding]) -> str:
    """The ``Release groups`` section: a heading and one line per finding, in *findings*' order.

    It begins with a blank line, as the other sections do, and is printed only when there are
    findings.
    """
    lines = ["", "## Release groups", ""]
    for finding in findings:
        where = finding.epic.slug if not finding.group else f"{finding.epic.slug} {finding.group}"
        lines.append(f"- {where} ({finding.kind}): {finding.detail}")
    return "\n".join(lines)
