"""The release-group rules and the holder check: what a group must be, and what holds a task.

Epic vergil-project/.github#1672, phase 3 Task 10 (spec §5.1, §5.5). :func:`check_group_rules`
returns a :class:`RuleBreak` for each rule of §5.1 a group breaks: every member an open task
(not an epic or an archive), in one repo, under the group's epic, not operational and not a
documentation review; a size of 1 to ``MAX_BATCH``; the epic a development epic, or a proxy of
one with sound links; and no dependency path from one member to another through an open task
outside the group. :func:`dependency_paths` is that last walk. :func:`holders` names what holds
a task back from a change: an open PR, a handed-off batch or a batch marker. The design-question
rule stays a planning judgment and is not checked.

The reads go through :mod:`~vergil_tooling.lib.in_flight` for PRs, relays and local ready-states
(the same trusted sources the in-flight marks read), and through :mod:`~vergil_tooling.lib.epics`
and the trust layer for the issue graph. Every read that fails raises :class:`GroupRuleReadError`,
naming what could not be read; no failure ever reads as "no break" or "no holder".
"""

from __future__ import annotations

import re
import subprocess
from collections import deque
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from vergil_tooling.lib import epic_audit, epics, github, in_flight, proxies, trust
from vergil_tooling.lib.pr_workflow import batch_marker
from vergil_tooling.lib.pr_workflow.batch_rules import MAX_BATCH
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

# The errors a GitHub, git, trust or configuration read can end in. A read that ends in any of
# them refuses the rules, never passes them (see batch_rules._READ_ERRORS for the same set).
_READ_ERRORS: tuple[type[Exception], ...] = (
    subprocess.CalledProcessError,
    OSError,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    WorkflowError,
    trust.TrustReadError,
    trust.UntrustedContentError,
)

_DOC_REVIEW_TITLE = "Documentation review:"
_DOC_REVIEW_LINE = re.compile(r"Plan task: doc-review\b")
_KIND_WORDS = {epics.NOT_EPIC_KIND: "not an epic", epics.ARCHIVE_KIND: "an archive"}


class GroupRuleReadError(Exception):
    """A read the group rules need failed: the rules refuse, naming what could not be read."""


@dataclass(frozen=True)
class RuleBreak:
    """One rule of §5.1 a group breaks: the group, the rule's name and what breaks it."""

    group: str
    rule: Literal[
        "open code task",
        "one repo",
        "not operational",
        "not a documentation review",
        "dependency path",
        "size",
        "group's epic",
        "same epic",
    ]
    detail: str


@dataclass(frozen=True)
class Holder:
    """What holds *task* back from a change, and where: a PR URL, a branch or a marker's batch."""

    task: epics.IssueRef
    kind: Literal["open PR", "handed-off batch", "batch marker"]
    where: str


def _key(ref: epics.IssueRef) -> tuple[str, str, int]:
    """*ref* as a key that ignores the case of its owner and repo (GitHub names ignore it)."""
    return (ref.owner.casefold(), ref.repo.casefold(), ref.number)


def _repo_slug(ref: epics.IssueRef) -> str:
    return f"{ref.owner}/{ref.repo}"


def _read[T](what: str, read: Callable[[], T]) -> T:
    """Run one read; a failure raises :class:`GroupRuleReadError` naming *what*.

    A missing GitHub token propagates unchanged, so the CLI's auth-error boundary prints its
    configuration fix.
    """
    try:
        return read()
    except github.MissingGitHubTokenError:
        raise
    except _READ_ERRORS as exc:
        raise GroupRuleReadError(f"could not read {what}: {error_text(exc)}") from exc


def _in_org[T](ref: epics.IssueRef, read: Callable[[epics.IssueRef], T]) -> T:
    """*read* of *ref*, with the App token scoped to *ref*'s own org."""
    with github.target_org(ref.owner):
        return read(ref)


def _title(ref: epics.IssueRef) -> str:
    raw: object = github.read_json(
        "issue", "view", str(ref.number), "--repo", _repo_slug(ref), "--json", "title"
    )
    title = raw.get("title") if isinstance(raw, dict) else None
    if not isinstance(title, str):
        raise ValueError(f"no title in the read of {ref.slug}: {raw!r}")
    return title


def _trusted_body(ref: epics.IssueRef) -> str:
    """*ref*'s body, which the trust layer must trust: an untrusted one is a stop."""
    content = trust.read_issue(_repo_slug(ref), ref.number, require_body=True)
    return content.body or ""


def _is_doc_review(ref: epics.IssueRef) -> bool:
    """True when *ref* is a documentation review: its title or its trusted body says so."""
    title = _read(f"the title of {ref.slug}", partial(_in_org, ref, _title))
    if title.startswith(_DOC_REVIEW_TITLE):
        return True
    body = _read(f"the body of {ref.slug}", partial(_in_org, ref, _trusted_body))
    return _DOC_REVIEW_LINE.search(body) is not None


def _describe(kind: str) -> str:
    """*kind*, as a clause of a refusal: a standing kind's key names its epic."""
    return _KIND_WORDS.get(kind, f"a {kind} epic")


def _primary_of(proxy: epics.IssueRef) -> epics.IssueRef | str:
    """The Primary of proxy *proxy*, or the problem its link has (a broken link is a refusal)."""
    try:
        return _in_org(proxy, proxies.primary_of)
    except proxies.ProxyLinkError as exc:
        return f"has a broken Primary link: {exc}"


def _not_group_epic(head: epics.IssueRef, kind: str) -> str | None:
    """Why *head* is not a group's epic (a development epic, or a proxy of one), else None."""
    if kind == epics.DEVELOPMENT_KIND:
        return None
    if kind != epics.PROXY_LABEL:
        return f"is {_describe(kind)}, not a development epic or a proxy of one"
    problems = _read(
        f"the proxy links of {head.slug}", partial(epic_audit.proxy_link_problems, head)
    )
    if problems:
        return "has broken proxy links: " + "; ".join(problems)
    primary = _read(f"the Primary link of {head.slug}", partial(_primary_of, head))
    if isinstance(primary, str):
        return primary
    _, primary_kind = _read(
        f"the kind of {primary.slug}", partial(_in_org, primary, epics.epic_kind)
    )
    if primary_kind != epics.DEVELOPMENT_KIND:
        described = _describe(primary_kind)
        return f"has Primary {primary.slug}, which is {described}, not a development epic"
    return None


def _size_breaks(group: str, members: Sequence[epics.IssueRef]) -> list[RuleBreak]:
    if 1 <= len(members) <= MAX_BATCH:
        return []
    detail = f"a group has 1 to {MAX_BATCH} members, not {len(members)}"
    return [RuleBreak(group, "size", detail)]


def _group_epic_breaks(group: str, head: epics.IssueRef, kind: str) -> list[RuleBreak]:
    reason = _not_group_epic(head, kind)
    return [] if reason is None else [RuleBreak(group, "group's epic", f"{head.slug} {reason}")]


def _member_breaks(group: str, head: epics.IssueRef, member: epics.IssueRef) -> list[RuleBreak]:
    """The per-member rules: open, a task, not operational, not a doc review, under *head*."""
    slug = member.slug
    breaks: list[RuleBreak] = []
    state = _read(f"the state of {slug}", partial(_in_org, member, epics.issue_state))
    non_task = _read(f"the labels of {slug}", partial(_in_org, member, epics.non_task_kind))
    if state != "OPEN":
        breaks.append(RuleBreak(group, "open code task", f"{slug} is {state.lower()}, not open"))
    if non_task is not None:
        breaks.append(RuleBreak(group, "open code task", f"{slug} is an {non_task}, not a task"))
    operational = _read(f"the labels of {slug}", partial(_in_org, member, epics.operational_kind))
    if operational is not None:
        breaks.append(
            RuleBreak(
                group, "not operational", f"{slug} is labelled {operational}, not PR-workable"
            )
        )
    if _is_doc_review(member):
        breaks.append(RuleBreak(group, "not a documentation review", f"{slug} is a doc review"))
    placed = _read(f"the epic of {slug}", partial(_in_org, member, epics.task_epic))
    if placed.epic is None or _key(placed.epic) != _key(head):
        where = "no epic" if placed.epic is None else placed.epic.slug
        breaks.append(RuleBreak(group, "same epic", f"{slug} is under {where}, not {head.slug}"))
    return breaks


def _one_repo_breaks(group: str, members: Sequence[epics.IssueRef]) -> list[RuleBreak]:
    repos = {_repo_slug(m).casefold(): _repo_slug(m) for m in members}
    if len(repos) <= 1:
        return []
    listed = ", ".join(sorted(repos.values(), key=str.casefold))
    return [RuleBreak(group, "one repo", f"the members are in {listed}")]


class _Reader:
    """The issue-graph reads of one walk: each issue's state and open blockers, read once."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str, int], str] = {}
        self._blockers: dict[tuple[str, str, int], list[epics.IssueRef]] = {}

    def state(self, issue: epics.IssueRef) -> str:
        key = _key(issue)
        if key not in self._states:
            read = partial(_in_org, issue, epics.issue_state)
            self._states[key] = _read(f"the state of {issue.slug}", read)
        return self._states[key]

    def open_blockers(self, task: epics.IssueRef) -> list[epics.IssueRef]:
        """The open issues *task* is blocked by: its Blocked-by lines and its native links."""
        key = _key(task)
        if key not in self._blockers:
            found: dict[tuple[str, str, int], epics.IssueRef] = {}
            body = partial(_in_org, task, epics.blockers_of)
            for blocker in _read(f"the Blocked-by lines of {task.slug}", body):
                if self.state(blocker) == "OPEN":
                    found.setdefault(_key(blocker), blocker)
            native = partial(_in_org, task, epics.native_blockers_of)
            for link in _read(f"the native blocked-by links of {task.slug}", native):
                if link.state == "OPEN":
                    found.setdefault(_key(link.ref), link.ref)
            self._blockers[key] = list(found.values())
        return self._blockers[key]


def _walk_from(
    start: epics.IssueRef, member_keys: set[tuple[str, str, int]], reader: _Reader
) -> list[tuple[epics.IssueRef, epics.IssueRef, epics.IssueRef]]:
    """The paths from *start* through open outside tasks to each other member it reaches.

    *start* waits on the first outside task on a path, which waits (transitively) on the
    member the path ends at. A walk does not pass through a member, which is a start of its
    own walk, and each member is reached once from *start*.
    """
    start_key = _key(start)
    seen = {start_key}
    reached: set[tuple[str, str, int]] = set()
    found: list[tuple[epics.IssueRef, epics.IssueRef, epics.IssueRef]] = []
    queue: deque[tuple[epics.IssueRef, epics.IssueRef | None]] = deque([(start, None)])
    while queue:
        task, through = queue.popleft()
        for blocker in reader.open_blockers(task):
            key = _key(blocker)
            if key in member_keys:
                if through is not None and key != start_key and key not in reached:
                    reached.add(key)
                    found.append((start, through, blocker))
                continue
            if key not in seen:
                seen.add(key)
                queue.append((blocker, blocker if through is None else through))
    return found


def dependency_paths(
    members: Sequence[epics.IssueRef],
) -> list[tuple[epics.IssueRef, epics.IssueRef, epics.IssueRef]]:
    """Each dependency path from one member to another through an open task outside the group.

    A path is ``(from member, through outside task, to member)``: *from* is blocked by *through*,
    which is blocked, transitively, by *to*. Blocked-by lines and native links count, across
    epics and repos; a closed task cannot wait, so a path through one does not count. A direct
    link between two members is not a path (commit order satisfies it). Raises
    :class:`GroupRuleReadError` when a read the walk needs fails.
    """
    reader = _Reader()
    member_keys = {_key(m) for m in members}
    found: list[tuple[epics.IssueRef, epics.IssueRef, epics.IssueRef]] = []
    for start in members:
        found += _walk_from(start, member_keys, reader)
    return found


def check_group_rules(
    epic: epics.IssueRef, group: str, members: Sequence[epics.IssueRef]
) -> list[RuleBreak]:
    """The rules of §5.1 that *members*, as the group *group* of *epic*, break; ``[]`` when none.

    *epic* is resolved to its chain's head, which must be a group's epic. Raises
    :class:`GroupRuleReadError` when a read the rules need fails.
    """
    head, kind = _read(f"the kind of {epic.slug}", partial(_in_org, epic, epics.epic_kind))
    breaks = _size_breaks(group, members)
    breaks += _group_epic_breaks(group, head, kind)
    for member in members:
        breaks += _member_breaks(group, head, member)
    breaks += _one_repo_breaks(group, members)
    for start, through, to in dependency_paths(members):
        detail = (
            f"{start.slug} waits on {through.slug}, an open task outside the group,"
            f" which waits on {to.slug}"
        )
        breaks.append(RuleBreak(group, "dependency path", detail))
    return breaks


def _open_pr_holders(task: epics.IssueRef, numbers: frozenset[int]) -> list[Holder]:
    repo = _repo_slug(task)
    return [
        Holder(task, "open PR", f"https://github.com/{repo}/pull/{number}")
        for number in sorted(numbers)
    ]


def holders(task: epics.IssueRef, *, main_root: Path) -> list[Holder]:
    """What holds *task* back: an open PR, a handed-off batch (relay or local), or a batch marker.

    *main_root* is the main clone of *task*'s repo, where the local ready-states and the batch
    markers are read. Raises :class:`GroupRuleReadError` when a read fails; never "no holder".
    """
    repo = _repo_slug(task)
    with github.target_org(task.owner):
        marks = _read(f"the open PRs of {repo}", partial(in_flight.open_pr_marks, repo))
        relays = _read(
            f"the relay refs of {repo}", partial(in_flight.ready_relays, repo, main_root)
        )
        local = _read(
            f"the local ready-states of {repo}",
            partial(in_flight.ready_local_branches, repo, main_root),
        )
        marker = _read(
            f"the batch marker of {task.slug}",
            partial(batch_marker.read_marker, main_root, f"#{task.number}"),
        )
    found = _open_pr_holders(task, marks.get(task.number, frozenset()))
    for ready in (relays, local):
        found += [
            Holder(task, "handed-off batch", branch)
            for branch, issues in sorted(ready.items())
            if task.number in issues
        ]
    if marker is not None:
        found.append(Holder(task, "batch marker", ", ".join(marker.issues)))
    return found
