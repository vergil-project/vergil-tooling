"""An epic's chain of containers past GitHub's 100-sub-issue limit.

GitHub allows at most 100 sub-issues under one parent, so an epic grows as a
**chain** (epic vergil-project/.github#1609, spec §4.1): its **head**, the
epic as created and the ref everything names, followed by zero or more
**continuations**. Each continuation is a native sub-issue of the container
before it, in that container's last slot, so a container holds at most
:data:`ITEM_CAP` items and, when it is not the tail, exactly one
continuation.

A continuation is labelled ``epic`` + ``continuation``
(:func:`~vergil_tooling.lib.epics.is_continuation`), and its trusted body
carries a ``Continues: <owner>/<repo>#<head>`` line, alone on its line. It is
followed only when it carries the label, its body passes the trust layer, its
``Continues:`` line names the head being walked, and it is a native child of
the container before it. Anything else is **chain drift**: a reader raises
:class:`ChainDriftError`, naming the container and the rule it broke, and
never guesses past a broken link. A continuation closed by hand is not drift:
it is walked like any other.

Every walk here uses only the raw per-container reads of
:mod:`~vergil_tooling.lib.epics` (``_container_listing``,
``_container_parent``), never its public readers, so making those readers
chain-aware changes nothing in this module.

**The write side** (spec §4.2, §4.3, §4.5, §4.7). :func:`link_into` and
:func:`move_into` put an item into the first container, in chain order, that
is open and holds fewer than :data:`ITEM_CAP` items (**first fit**), and roll
over only when every container is full: find-or-create the next
continuation, link it into the tail's last slot, then link the item. Before
any write they refuse a closed head (closed is closed) and a chain that
cannot grow past GitHub's nesting limit (:class:`ChainDepthError`), and
:func:`check_capacity` makes the same checks, reading only, for a command
that creates an issue before linking it. While :data:`ROLLOVER_ENABLED` is
off, no writer creates, adopts or links a continuation: the head is the only
container and takes up to GitHub's 100 items, and a full one is refused by
name (:class:`RolloverDisabledError`).

**Races** (spec §4.4). GitHub offers no lock, so a chain is kept whole by
reading back. A link GitHub refuses into a container whose fresh read holds
100 sub-issues is a racing writer's doing, whatever the error text says: the
writer re-reads, heals or rolls over, and tries again, at most
:data:`LINK_RETRIES` times in all (:class:`ChainLimitError`). After every
link the chain is read back: a tail left at 100 items with no continuation
is healed by moving one item into a new continuation and linking that into
the freed slot (:func:`heal_full_tail`), and two continuations under one
container are merged into the lower-numbered (:func:`heal_duplicate_continuations`).
Each heal prints one line. :func:`close_chain` closes a chain head first,
then every continuation (spec §4.6).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.epics import ClosedEpicError, EpicRefusalError, IssueRef
from vergil_tooling.lib.labels import ensure_labels
from vergil_tooling.lib.process_errors import error_text

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

#: The most items one container holds; the 100th slot of a non-tail container
#: is its continuation (spec §4.1).
ITEM_CAP = 99

#: GitHub's limit of sub-issues under one parent.
SUBISSUE_LIMIT = 100

#: GitHub's nesting limit in levels, the top issue included: the ``Max depth``
#: the live probe recorded (Task 4 of epic vergil-project/.github#1609,
#: vergil-project/.github#1632). The items of container ``k`` sit at the
#: head's level plus ``k``, so a chain grows only while that stays within it.
MAX_DEPTH = 8

#: Whether a writer may roll an epic over into a continuation. Off until every
#: reader of an epic's children reads through the chain (Task 11b of epic
#: vergil-project/.github#1609 removes it): while it is off, the head is the
#: only container and takes up to :data:`SUBISSUE_LIMIT` items, and a link into
#: a full head raises :class:`RolloverDisabledError` before any write.
ROLLOVER_ENABLED: bool = False

#: The most times a writer tries a link that GitHub refuses as full (spec §4.4):
#: each refusal is re-read, healed or rolled over, and tried again, and the
#: last raises :class:`ChainLimitError`.
LINK_RETRIES = 3

# GitHub's depth refusal, as the live probe recorded it: "... You can’t add
# more than 7 layers of sub-issues. ..."
_DEPTH_ERROR_RE = re.compile(r"more than \d+ layers of sub-issues")

# A continuation's Container: line, rewritten when an orphan is adopted.
_CONTAINER_LINE_RE = re.compile(r"^Container:[ \t]*[0-9]+[ \t]*$", re.MULTILINE)

#: The rules a continuation can break (spec §4.1), each a :class:`ChainDriftError` rule.
DRIFT_RULES = (
    "label",
    "untrusted-body",
    "continues-mismatch",
    "not-native-child",
    "two-continuations",
)

_CONTINUES_RE = re.compile(
    r"^Continues:[ \t]*([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#([0-9]+)[ \t]*$",
    re.MULTILINE,
)
# Any line that claims to be a Continues: line, well formed or not.
_CONTINUES_CLAIM_RE = re.compile(r"^[ \t]*Continues:", re.MULTILINE)


class ChainDriftError(EpicRefusalError, RuntimeError):
    """*container* claims to be a continuation and breaks *rule* (spec §4.1).

    *rule* is one of :data:`DRIFT_RULES`; *detail* says how it was broken.
    """

    def __init__(self, container: IssueRef, rule: str, detail: str) -> None:
        if rule not in DRIFT_RULES:
            raise ValueError(f"unknown chain drift rule {rule!r}")
        super().__init__(f"chain drift: {container.slug} breaks the {rule} rule: {detail}")
        self.container = container
        self.rule = rule


@dataclass(frozen=True)
class Container:
    """One container of a chain, as one read of it saw it.

    ``item_count`` is the container's sub-issue total less one when it holds
    a continuation: every item, open and closed, as GitHub counts them.
    ``continuation`` is the next container, or ``None`` for the tail.
    """

    ref: IssueRef
    state: str  # "OPEN" | "CLOSED"
    item_count: int
    continuation: IssueRef | None


def parse_continues(body: str) -> IssueRef | None:
    """The head *body*'s ``Continues:`` line names, or ``None``.

    The line must stand alone on its line. A body with no such line, or with
    lines naming different heads, gives ``None``.
    """
    named = {
        IssueRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))
        for m in _CONTINUES_RE.finditer(body)
    }
    return next(iter(named)) if len(named) == 1 else None


@dataclass(frozen=True)
class _Claim:
    """An issue that claims to be a continuation, and its body (``None`` if untrusted)."""

    body: str | None


def _claim(ref: IssueRef, labels: Collection[str]) -> _Claim | None:
    """Whether *ref* claims to be a continuation, read from its *labels* and body.

    The ``continuation`` label is a claim. So is an ``epic``-labelled issue
    whose trusted body carries a ``Continues:`` line: a continuation that
    lost its label, which :func:`_accept` refuses. Any other issue is an item
    (or a head), and its body is read only when it carries ``epic``.
    """
    if epics.CONTINUATION_LABEL in labels:
        return _Claim(body=epics._trusted_body(ref))
    if "epic" in labels:
        body = epics._trusted_body(ref)
        if body is not None and _CONTINUES_CLAIM_RE.search(body):
            return _Claim(body=body)
    return None


def _accept(ref: IssueRef, labels: Collection[str], claim: _Claim) -> IssueRef:
    """The head claimed continuation *ref* names; a broken label or body rule raises."""
    if not epics.is_continuation(labels):
        missing = "epic" if epics.CONTINUATION_LABEL in labels else epics.CONTINUATION_LABEL
        raise ChainDriftError(ref, "label", f"it claims to be a continuation but lacks {missing!r}")
    if claim.body is None:
        raise ChainDriftError(ref, "untrusted-body", "the trust layer does not trust its body")
    named = parse_continues(claim.body)
    if named is None:
        raise ChainDriftError(ref, "continues-mismatch", "its body has no single Continues: line")
    return named


@dataclass(frozen=True)
class _Step:
    """One container walked: what it is, and its items (its continuation excluded)."""

    container: Container
    items: tuple[epics.ChildState, ...]


def _continuations_under(
    head: IssueRef, current: IssueRef, listing: epics._ContainerListing
) -> list[IssueRef]:
    """The continuations of *head*'s chain that *listing*, the read of *current*, holds.

    Each is checked against the rules of spec §4.1, and a broken rule raises
    :class:`ChainDriftError`. More than one is returned as found: the readers
    raise on it, and :func:`heal_duplicate_continuations` heals it.
    """
    found: list[IssueRef] = []
    for entry in listing.children:
        child = entry.child.ref
        claim = _claim(child, entry.labels)
        if claim is None:
            continue
        named = _accept(child, entry.labels, claim)
        if named != head:
            raise ChainDriftError(
                child,
                "continues-mismatch",
                f"its Continues: line names {named.slug}, not {head.slug}",
            )
        parent = epics._container_parent(child)
        if parent != current:
            where = parent.slug if parent is not None else "no parent"
            raise ChainDriftError(
                child,
                "not-native-child",
                f"it is listed under {current.slug} but its native parent is {where}",
            )
        found.append(child)
    return found


def _walk(head: IssueRef) -> list[_Step]:
    """Walk *head*'s chain, one raw container read each, checking every continuation."""
    steps: list[_Step] = []
    current = head
    while True:
        listing = epics._container_listing(current)
        continuations = _continuations_under(head, current, listing)
        if len(continuations) > 1:
            raise ChainDriftError(
                current,
                "two-continuations",
                f"it holds {len(continuations)} continuations: "
                + ", ".join(c.slug for c in continuations),
            )
        successor = continuations[0] if continuations else None
        steps.append(
            _Step(
                container=Container(
                    ref=current,
                    state=listing.state,
                    item_count=listing.total - (1 if successor is not None else 0),
                    continuation=successor,
                ),
                items=tuple(e.child for e in listing.children if e.child.ref != successor),
            )
        )
        if successor is None:
            return steps
        current = successor


def containers(head: IssueRef) -> list[Container]:
    """Every container of *head*'s chain, head first; chain drift raises."""
    return [step.container for step in _walk(head)]


def chain_of(head: IssueRef) -> list[IssueRef]:
    """*head*, then each of its continuations in order; chain drift raises."""
    return [step.container.ref for step in _walk(head)]


def chain_child_states(head: IssueRef) -> list[epics.ChildState]:
    """Every item across *head*'s chain, in container order, continuations excluded."""
    return [item for step in _walk(head) for item in step.items]


def chain_contains(head: IssueRef, task: IssueRef) -> bool:
    """Whether *task* is linked as an item anywhere in *head*'s chain."""
    return any(item.ref == task for item in chain_child_states(head))


def head_of(container: IssueRef) -> IssueRef:
    """The head of *container*'s chain: *container* itself when it is not a continuation.

    Walks native parents while the container is a continuation, and checks
    each one's ``Continues:`` line names the head the walk reaches. A
    continuation with no native parent (an **orphaned continuation**, spec
    §4.3) resolves through its trusted ``Continues:`` line instead, and is
    reported on stderr as drift; adopting it is the writers' job, never a
    reader's. Any broken rule raises :class:`ChainDriftError`.
    """
    current = container
    labels = epics._labels(current)
    claim = _claim(current, labels)
    if claim is None:
        return container
    head = _accept(current, labels, claim)
    while True:
        parent = epics._container_parent(current)
        if parent is None:
            print(
                f"chain drift: {current.slug} is an orphaned continuation of {head.slug}",
                file=sys.stderr,
            )
            return head
        parent_labels = epics._labels(parent)
        parent_claim = _claim(parent, parent_labels)
        if parent_claim is None:
            if parent != head:
                raise ChainDriftError(
                    current,
                    "not-native-child",
                    f"its Continues: line names {head.slug}, but its native parents reach "
                    f"{parent.slug}",
                )
            return head
        named = _accept(parent, parent_labels, parent_claim)
        if named != head:
            raise ChainDriftError(
                parent,
                "continues-mismatch",
                f"its Continues: line names {named.slug}, not {head.slug}",
            )
        current = parent


# -- the write side -----------------------------------------------------------------


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


class RolloverDisabledError(EpicRefusalError, RuntimeError):
    """*head* holds GitHub's 100 sub-issues, and rollover is not enabled yet."""

    def __init__(self, head: IssueRef) -> None:
        super().__init__(
            f"epic {head.slug} is at GitHub's limit of {SUBISSUE_LIMIT} sub-issues, and "
            "rollover into a continuation is not enabled yet"
        )
        self.head = head


class ChainDepthError(EpicRefusalError, RuntimeError):
    """*head*'s chain cannot grow past GitHub's nesting limit (spec §4.5).

    Raised before any write when the next item would sit deeper than
    :data:`MAX_DEPTH`, and in place of GitHub's own depth error if it comes
    first. *task*, when given, is the item whose link GitHub refused: the
    depth check does not count the item's own sub-issues, so the message
    names them as a possible cause.
    """

    def __init__(
        self, head: IssueRef, *, items: int, containers: int, task: IssueRef | None = None
    ) -> None:
        message = (
            f"epic {head.slug} cannot grow: {_count(items, 'item')} in "
            f"{_count(containers, 'container')}, at GitHub's nesting limit of {MAX_DEPTH}; "
            "split the epic"
        )
        if task is not None:
            message += f", or {task.slug}'s own sub-issues are too deep to fit under it"
        super().__init__(message)
        self.head = head
        self.task = task


class ChainLimitError(EpicRefusalError, RuntimeError):
    """A link into *container* of *head*'s chain was refused as full every time it was tried.

    GitHub refuses a link into a parent that already holds
    :data:`SUBISSUE_LIMIT` sub-issues. A racing writer can fill the container
    a writer chose between its read and its link (spec §4.4), so a link
    refused as full is re-read and retried, at most :data:`LINK_RETRIES`
    times in all; this is raised once every try was refused. *item* is what
    was being linked (``None`` when the full-tail heal had no item of its
    own); *note*, when given, says where an item the heal moved is left.
    """

    def __init__(
        self, head: IssueRef, container: IssueRef, item: IssueRef | None, *, note: str = ""
    ) -> None:
        what = item.slug if item is not None else "a continuation"
        message = (
            f"could not link {what} into {container.slug} of epic {head.slug}: GitHub refused it "
            f"at its limit of {SUBISSUE_LIMIT} sub-issues in {LINK_RETRIES} "
            + ("try" if LINK_RETRIES == 1 else "tries")
        )
        super().__init__(f"{message}; {note}" if note else message)
        self.head = head
        self.container = container
        self.item = item


def _is_full(container: IssueRef) -> bool:
    """Whether *container*, read afresh, holds GitHub's :data:`SUBISSUE_LIMIT` sub-issues."""
    return epics._container_listing(container).total >= SUBISSUE_LIMIT


def _level(ref: IssueRef) -> int:
    """*ref*'s level in GitHub's sub-issue tree: 1 for a top issue, one more per ancestor."""
    level = 1
    parent = epics._container_parent(ref)
    while parent is not None:
        level += 1
        if level > MAX_DEPTH + 1:
            raise RuntimeError(
                f"the native parents of {ref.slug} run deeper than GitHub's nesting limit "
                f"of {MAX_DEPTH}"
            )
        parent = epics._container_parent(parent)
    return level


@dataclass(frozen=True)
class _Fit:
    """Where the next item of a chain goes, as one read of the chain saw it.

    *target* is the first open container with a free item slot, or ``None``
    when every container is full and the chain must roll over; *chain* is
    every container, head first.
    """

    target: IssueRef | None
    chain: tuple[Container, ...]

    @property
    def tail(self) -> Container:
        return self.chain[-1]

    @property
    def items(self) -> int:
        return sum(container.item_count for container in self.chain)


def _fit(head: IssueRef) -> _Fit:
    """Find the next item's container in *head*'s chain, refusing before any write.

    Raises :class:`ClosedEpicError` for a closed head,
    :class:`RolloverDisabledError` for a full head while rollover is off, and
    :class:`ChainDepthError` when the item, or the continuation a rollover
    would add, would sit past :data:`MAX_DEPTH`. Reads only.
    """
    if not ROLLOVER_ENABLED:
        listing = epics._container_listing(head)
        if listing.state == "CLOSED":
            raise ClosedEpicError(head)
        if listing.total >= SUBISSUE_LIMIT:
            raise RolloverDisabledError(head)
        chain: tuple[Container, ...] = (
            Container(ref=head, state=listing.state, item_count=listing.total, continuation=None),
        )
        target: IssueRef | None = head
        position = 1
    else:
        chain = tuple(containers(head))
        if chain[0].state == "CLOSED":
            raise ClosedEpicError(head)
        free = [
            k
            for k, container in enumerate(chain, start=1)
            if container.state == "OPEN" and container.item_count < ITEM_CAP
        ]
        position = free[0] if free else len(chain) + 1
        target = chain[position - 1].ref if free else None
    fit = _Fit(target=target, chain=chain)
    # The items of container k sit at the head's level plus k.
    if _level(head) + position > MAX_DEPTH:
        raise ChainDepthError(head, items=fit.items, containers=len(chain))
    return fit


def check_capacity(head: IssueRef) -> None:
    """Refuse, reading only, when a link into *head*'s chain would fail.

    For a command that creates an issue and then links it: it runs this
    before creating, so a refusal leaves nothing orphaned (spec §4.5).
    Raises :class:`ClosedEpicError`, :class:`RolloverDisabledError` or
    :class:`ChainDepthError`, as :func:`link_into` would.
    """
    _fit(head)


def _link_rewording_depth(
    write: Callable[[IssueRef, IssueRef], None],
    parent: IssueRef,
    child: IssueRef,
    *,
    head: IssueRef,
    fit: _Fit,
    task: IssueRef | None,
) -> None:
    """``write(parent, child)``, with GitHub's depth error reworded as :class:`ChainDepthError`."""
    try:
        write(parent, child)
    except github.GitHubAPIError as exc:
        if _DEPTH_ERROR_RE.search(error_text(exc)):
            raise ChainDepthError(
                head, items=fit.items, containers=len(fit.chain), task=task
            ) from exc
        raise


def _place(head: IssueRef, task: IssueRef, write: Callable[[IssueRef, IssueRef], None]) -> IssueRef:
    """Put *task* into the first container of *head*'s chain with a free slot, rolling over.

    A link GitHub refuses, into a container whose fresh read holds
    :data:`SUBISSUE_LIMIT` sub-issues, is a racing writer's doing whatever
    the error says (``LIMIT_ERROR`` is a hint only): the chain is re-read and
    the link tried again, at most :data:`LINK_RETRIES` times in all
    (spec §4.4). Any other failure is raised with GitHub's error. A full tail
    with no continuation is healed by the rollover
    (:func:`find_or_create_continuation`), and once *task* is linked the
    chain is read back and healed (:func:`_settle`).
    """
    refusals = 0
    rollovers = 0
    while True:
        fit = _fit(head)
        container = fit.target if fit.target is not None else fit.tail.ref
        if fit.target is None and rollovers > MAX_DEPTH:
            raise RuntimeError(
                f"no container of epic {head.slug} had a free slot for {task.slug} after "
                f"{rollovers} rollovers"
            )
        try:
            if fit.target is None:
                rollovers += 1
                find_or_create_continuation(head, fit.tail.ref)
                continue
            _link_rewording_depth(write, fit.target, task, head=head, fit=fit, task=task)
        except github.GitHubAPIError as exc:
            if not _is_full(container):
                raise
            refusals += 1
            if refusals >= LINK_RETRIES:
                raise ChainLimitError(head, container, task) from exc
            print(
                f"{container.slug} of epic {head.slug} was full when {task.slug} was linked "
                f"(a racing writer); re-reading the chain to try again ({refusals} of "
                f"{LINK_RETRIES})"
            )
            continue
        return _settle(head, task, fit.target)


def _settle(head: IssueRef, task: IssueRef, linked: IssueRef) -> IssueRef:
    """Read *head*'s chain back after *task*'s link into *linked*, heal it, and say where *task* is.

    Racing writers can leave two continuations under one container, or a
    tail at :data:`SUBISSUE_LIMIT` with no continuation (spec §4.4); either
    heal can move *task* itself, so after one the container is read afresh.
    """
    if not ROLLOVER_ENABLED:
        return linked
    healed = heal_duplicate_continuations(head)
    healed = heal_full_tail(head, own_item=task) or healed
    if not healed:
        return linked
    where = epics._container_parent(task)
    if where is None:
        raise RuntimeError(f"{task.slug} has no parent after the chain of epic {head.slug} healed")
    return where


def link_into(head: IssueRef, task: IssueRef) -> IssueRef:
    """Link *task* into *head*'s chain by first fit, rolling over when every container is full.

    Returns the container *task* is in once the chain is read back and
    healed (a heal can move it). A closed head raises
    :class:`ClosedEpicError` and is never reopened; the depth limit raises
    :class:`ChainDepthError` before any write; a full head while rollover is
    off raises :class:`RolloverDisabledError`; a link refused as full
    :data:`LINK_RETRIES` times raises :class:`ChainLimitError`. A closed
    continuation is skipped, never linked into.
    """
    return _place(head, task, epics.add_child)


def move_into(head: IssueRef, task: IssueRef) -> IssueRef:
    """Move *task* from wherever it is into *head*'s chain, in one atomic re-parent.

    Returns the container *task* is in. The container is chosen as
    :func:`link_into` chooses it, with the same refusals and rollover, and
    the move is one ``replaceParent``, so a refused move leaves *task* where
    it was. A *task* already linked anywhere in the chain stays where it is.
    """
    epics.require_open_epic(head)
    current = epics._container_parent(task)
    if current is not None and current in chain_of(head):
        return current
    return _place(head, task, epics.reparent_child)


def unlink_from(head: IssueRef, task: IssueRef) -> None:
    """Unlink *task* from whichever container of *head*'s chain it is linked in.

    Raises :class:`ValueError`, naming both, when *task* is not an item of
    the chain.
    """
    for step in _walk(head):
        if any(item.ref == task for item in step.items):
            epics.remove_child(step.container.ref, task)
            return
    raise ValueError(f"{task.slug} is not linked in the chain of epic {head.slug}")


def _continuation_title(head: IssueRef, k: int) -> str:
    return f"{epics._issue_title(head)} (continuation {k})"


def _continuation_body(head: IssueRef, k: int) -> str:
    return (
        f"This issue holds the overflow of epic {head.slug} past GitHub's limit of "
        f"{SUBISSUE_LIMIT} sub-issues. It is managed by the tooling.\n\n"
        f"Continues: {head.slug}\n"
        f"Container: {k}\n"
    )


def find_orphans(head: IssueRef) -> list[IssueRef]:
    """The open continuations of *head* that its chain does not reach, lowest number first.

    Lists the open ``continuation``-labelled issues in *head*'s repo. One
    whose trusted ``Continues:`` line names *head*, with no native parent and
    not reached by :func:`chain_of`, is an orphan (spec §4.3). One that
    names *head* but breaks another rule (the ``epic`` label missing, or
    linked outside the chain) raises :class:`ChainDriftError`. So does an
    unparented one whose body is untrusted or has no single ``Continues:``
    line: it could be this head's orphan, and is never guessed past. One
    that is linked elsewhere, or names another head, is not this head's.
    """
    repo = f"{head.owner}/{head.repo}"
    rows: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        repo,
        "--label",
        epics.CONTINUATION_LABEL,
        "--state",
        "open",
        "--limit",
        str(epics.LISTING_LIMIT),
        "--json",
        "number,labels",
    )
    epics.refuse_truncated(rows, epics.LISTING_LIMIT, f"the open continuations of {repo}")
    if not isinstance(rows, list):
        raise RuntimeError(f"listing the open continuations of {repo} returned no list")
    reached = set(chain_of(head))
    orphans: list[IssueRef] = []
    for row in sorted(rows, key=lambda r: int(r["number"])):
        ref = IssueRef(owner=head.owner, repo=head.repo, number=int(row["number"]))
        if ref in reached:
            continue
        labels = {str(label.get("name", "")) for label in row.get("labels") or []}
        parent = epics._container_parent(ref)
        body = epics._trusted_body(ref)
        named = parse_continues(body) if body is not None else None
        if named is None:
            if parent is not None:
                continue  # linked in another container, whose chain's readers judge it
            if body is None:
                raise ChainDriftError(
                    ref, "untrusted-body", "an unlinked continuation whose body is not trusted"
                )
            raise ChainDriftError(
                ref, "continues-mismatch", "an unlinked continuation with no single Continues: line"
            )
        if named != head:
            continue
        if not epics.is_continuation(labels):
            raise ChainDriftError(ref, "label", "it names this head but lacks 'epic'")
        if parent is not None:
            raise ChainDriftError(
                ref,
                "not-native-child",
                f"its Continues: line names {head.slug}, but it is linked under {parent.slug}, "
                "outside that chain",
            )
        orphans.append(ref)
    return orphans


def _announce(head: IssueRef, continuation: IssueRef) -> None:
    """Report a rollover of *head* into *continuation*, as its kind calls for (spec §4.7).

    A queue's is the review nudge; any other standing epic's is a plain
    line; a development or proxy epic's is loud, advises a split and is
    commented on the head too.
    """
    kind = epics.standing_kind_of(epics._labels(head))
    if kind is not None and kind in epics.QUEUE_KINDS:
        print(
            f"queue {head.slug} rolled over into continuation {continuation.slug}; "
            f"run epic-review {kind.key}"
        )
        return
    if kind is not None:
        print(f"epic {head.slug} rolled over into continuation {continuation.slug}")
        return
    line = (
        f"epic {head.slug} rolled over into continuation {continuation.slug} at "
        f"{ITEM_CAP} children; consider splitting it"
    )
    print(line)
    github.run(
        "issue", "comment", str(head.number), "--repo", epics._repo_slug(head), "--body", line
    )


def _renumber(head: IssueRef, continuation: IssueRef, k: int) -> None:
    """Rewrite an adopted *continuation*'s title and ``Container:`` line to its place *k*."""
    body = epics._trusted_body(continuation)
    if body is None:
        raise ChainDriftError(
            continuation, "untrusted-body", "the trust layer does not trust its body"
        )
    line = f"Container: {k}"
    if _CONTAINER_LINE_RE.search(body):
        new_body = _CONTAINER_LINE_RE.sub(line, body)
    else:
        new_body = f"{body.rstrip()}\n{line}\n"
    title = _continuation_title(head, k)
    if new_body == body and title == epics._issue_title(continuation):
        return
    github.run(
        "issue",
        "edit",
        str(continuation.number),
        "--repo",
        epics._repo_slug(continuation),
        "--title",
        title,
        "--body",
        new_body,
    )


def find_or_create_continuation(
    head: IssueRef, tail: IssueRef, *, own_item: IssueRef | None = None
) -> IssueRef:
    """The next continuation of *head*'s chain after *tail*, linked into *tail*'s last slot.

    An existing continuation under *tail* (a concurrent writer's) is
    returned as it is. Otherwise an orphan of *head* (:func:`find_orphans`)
    is adopted: linked under *tail*, then its title and ``Container:`` line
    renumbered. Only when there is neither is a continuation created, then
    linked. A closed *head* raises :class:`ClosedEpicError` before any read
    of orphans and any write: an orphan of a closed head is never adopted. A
    closed *tail* under an open head takes the link, the one link a closed
    container takes (spec §4.2), and stays closed. Every growth of the chain
    is reported (:func:`_announce`). While rollover is off this raises
    :class:`RolloverDisabledError` and writes nothing.

    A *tail* holding :data:`SUBISSUE_LIMIT` items has no slot for its
    continuation (racing writers filled it, spec §4.4), so one is freed
    first: the continuation is found or created unlinked, one item is moved
    from *tail* into it with an atomic re-parent (*own_item* when it is in
    *tail*, else *tail*'s newest item), and the continuation is linked into
    the freed slot, so no item is ever left outside a container. If that
    link fails, the moved item is put back. When a racing writer took the
    slot, it goes into the first container with room (:func:`_rehome`) and
    GitHub's error is raised for the caller to re-read and retry. Otherwise
    it goes back into *tail*, and the failure is raised as a
    :class:`ChainDepthError` or a :class:`RuntimeError` carrying GitHub's
    text, never as a GitHub error a caller would take for a race.
    """
    if not ROLLOVER_ENABLED:
        raise RolloverDisabledError(head)
    if epics._issue_state(head) == "CLOSED":
        raise ClosedEpicError(head)
    steps = _walk(head)
    chain = [step.container for step in steps]
    refs = [container.ref for container in chain]
    if tail not in refs:
        raise ValueError(f"{tail.slug} is not a container of the chain of epic {head.slug}")
    position = refs.index(tail)
    if position + 1 < len(refs):
        return refs[position + 1]
    fit = _Fit(target=None, chain=tuple(chain))
    k = len(refs) + 1
    orphans = find_orphans(head)
    if orphans:
        continuation = orphans[0]
    else:
        repo = epics._repo_slug(head)
        labels = ["epic", epics.CONTINUATION_LABEL]
        ensure_labels(repo, labels)
        url = github.create_issue(
            repo=repo,
            title=_continuation_title(head, k),
            body=_continuation_body(head, k),
            labels=labels,
        )
        continuation = IssueRef(
            owner=head.owner, repo=head.repo, number=int(url.rstrip("/").rsplit("/", 1)[-1])
        )
    moved: IssueRef | None = None
    if chain[-1].item_count >= SUBISSUE_LIMIT:
        items = [item.ref for item in steps[-1].items]
        moved = own_item if own_item in items else items[-1]
        try:
            epics.reparent_child(continuation, moved)
        except github.GitHubAPIError as exc:
            # A move into an unlinked continuation never meets the sub-issue
            # limit, so this is never a race: nothing moved, nothing to put back.
            raise RuntimeError(
                f"could not move {moved.slug} from the full tail {tail.slug} of epic "
                f"{head.slug} into continuation {continuation.slug}: {error_text(exc)}"
            ) from exc
    write = epics._add_child_unchecked if chain[-1].state == "CLOSED" else epics.add_child
    try:
        _link_rewording_depth(write, tail, continuation, head=head, fit=fit, task=None)
    except (github.GitHubAPIError, ChainDepthError) as exc:
        if moved is None:
            raise
        if isinstance(exc, github.GitHubAPIError) and _is_full(tail):
            _rehome(head, tail, moved, continuation, exc)
            raise
        epics.reparent_child(tail, moved)
        print(
            f"epic {head.slug}: moved {moved.slug} back into {tail.slug}, since continuation "
            f"{continuation.slug} could not be linked into the slot it freed"
        )
        if isinstance(exc, ChainDepthError):
            raise
        # Not a race: raised so that no caller takes the full tail for one and retries.
        raise RuntimeError(
            f"could not link continuation {continuation.slug} into {tail.slug} of epic "
            f"{head.slug}: {error_text(exc)}"
        ) from exc
    if orphans:
        _renumber(head, continuation, k)
    if moved is not None:
        print(
            f"healed epic {head.slug}: its tail {tail.slug} held {SUBISSUE_LIMIT} items and no "
            f"continuation; moved {moved.slug} into continuation {continuation.slug} and linked "
            "it into the freed slot"
        )
    _announce(head, continuation)
    return continuation


def _rehome(
    head: IssueRef, tail: IssueRef, moved: IssueRef, continuation: IssueRef, exc: Exception
) -> None:
    """Put *moved* back in *head*'s chain after a racing writer took the slot it freed in *tail*.

    *moved* sits in the unlinked *continuation*, outside the chain, so it
    goes into the first container with room, and *continuation* is left
    unlinked, an orphan the next rollover adopts. With no room anywhere,
    :class:`ChainLimitError` says where *moved* waits.
    """
    fit = _fit(head)
    if fit.target is None:
        raise ChainLimitError(
            head,
            tail,
            continuation,
            note=(
                f"{moved.slug} waits in the unlinked continuation {continuation.slug}, which the "
                "next rollover adopts"
            ),
        ) from exc
    epics.reparent_child(fit.target, moved)
    print(
        f"epic {head.slug}: a racing writer took the slot freed in {tail.slug}; moved "
        f"{moved.slug} into {fit.target.slug} and left continuation {continuation.slug} "
        "unlinked, for the next rollover to adopt"
    )


# -- race heals and closing (spec §4.4, §4.6) ----------------------------------------


def heal_full_tail(head: IssueRef, *, own_item: IssueRef | None = None) -> bool:
    """Heal *head*'s tail when it holds :data:`SUBISSUE_LIMIT` items and no continuation.

    Two writers that each saw room in the tail can both link, taking the
    slot its continuation needs (spec §4.4). The heal moves one item into a
    continuation and links that into the freed slot
    (:func:`find_or_create_continuation`): *own_item*, the item the writer
    just linked, when it is in the tail, else the tail's newest item. A heal
    that loses a race to another writer is re-read and tried again, at most
    :data:`LINK_RETRIES` times in all.

    Returns whether the tail was found full; it is healed on return. It
    returns ``False`` with no write while rollover is off, for a tail with
    room, and for a chain at GitHub's nesting limit, which cannot take a
    continuation and so needs no slot for one.
    """
    if not ROLLOVER_ENABLED:
        return False
    refused: list[tuple[IssueRef, github.GitHubAPIError]] = []
    while len(refused) < LINK_RETRIES:
        chain = containers(head)
        tail = chain[-1]
        if tail.item_count < SUBISSUE_LIMIT:
            return bool(refused)  # a racing writer's heal won, after one of ours was refused
        if _level(head) + len(chain) + 1 > MAX_DEPTH:
            return False
        try:
            find_or_create_continuation(head, tail.ref, own_item=own_item)
        except github.GitHubAPIError as exc:
            if not _is_full(tail.ref):
                raise
            refused.append((tail.ref, exc))
            continue
        return True
    container, error = refused[-1]
    raise ChainLimitError(head, container, None) from error


def heal_duplicate_continuations(head: IssueRef) -> bool:
    """Heal every container of *head*'s chain that holds two continuations or more.

    Two writers that both rolled over can each link a continuation under one
    container (spec §4.4). The lowest-numbered is kept; each other one's
    children are moved into it with atomic re-parents, then it is unlinked,
    then closed as not planned with a comment naming the kept one, so no
    closed duplicate stays in the chain or holds a slot. Each heal prints one
    line. A merge that would put more than :data:`SUBISSUE_LIMIT` sub-issues
    in the kept one is refused by name, before any write to it.

    Returns whether any container was healed; while rollover is off it reads
    nothing and returns ``False``.
    """
    if not ROLLOVER_ENABLED:
        return False
    healed = False
    current = head
    while True:
        listing = epics._container_listing(current)
        found = sorted(_continuations_under(head, current, listing), key=lambda r: r.number)
        if not found:
            return healed
        keep = found[0]
        for duplicate in found[1:]:
            _merge_duplicate(head, current, keep, duplicate)
            healed = True
        current = keep


def _merge_duplicate(head: IssueRef, parent: IssueRef, keep: IssueRef, duplicate: IssueRef) -> None:
    """Move *duplicate*'s children into *keep*, unlink *duplicate* from *parent*, then close it."""
    moving = [entry.child.ref for entry in epics._container_listing(duplicate).children]
    held = epics._container_listing(keep).total
    if held + len(moving) > SUBISSUE_LIMIT:
        raise RuntimeError(
            f"cannot heal the duplicate continuations of epic {head.slug} under {parent.slug}: "
            f"{keep.slug} holds {held} sub-issues and {duplicate.slug} {len(moving)}, past "
            f"GitHub's limit of {SUBISSUE_LIMIT}; a person must move some by hand"
        )
    for item in moving:
        epics.reparent_child(keep, item)
    epics.remove_child(parent, duplicate)
    github.run(
        "issue",
        "close",
        str(duplicate.number),
        "--repo",
        epics._repo_slug(duplicate),
        "--reason",
        "not planned",
        "--comment",
        f"Closed as a duplicate continuation of epic {head.slug}, left by two racing writers: "
        f"{keep.slug} is kept, and this issue's sub-issues were moved into it.",
    )
    print(
        f"healed epic {head.slug}: {parent.slug} held two continuations; kept {keep.slug}, "
        f"moved {_count(len(moving), 'sub-issue')} from {duplicate.slug} into it, then unlinked "
        f"{duplicate.slug} and closed it as not planned"
    )


def close_chain(head: IssueRef) -> list[IssueRef]:
    """Close *head*, then every continuation of its chain; return those closed, head first.

    The head is closed first (spec §4.6), so a close interrupted partway
    leaves a closed head with an open continuation, which the audit reports
    and the epic sweep heals by calling this again. A container already
    closed is skipped: closing is never a reopen, so closed is closed holds.
    Prints one line naming the continuations it closed, when there are any.
    """
    chain = containers(head)
    closed: list[IssueRef] = []
    for container in chain:
        if container.state == "CLOSED":
            continue
        github.run(
            "issue", "close", str(container.ref.number), "--repo", epics._repo_slug(container.ref)
        )
        closed.append(container.ref)
    continuations = [ref for ref in closed if ref != head]
    if continuations:
        print(
            f"closed {_count(len(continuations), 'continuation')} of epic {head.slug}: "
            + ", ".join(ref.slug for ref in continuations)
        )
    return closed
