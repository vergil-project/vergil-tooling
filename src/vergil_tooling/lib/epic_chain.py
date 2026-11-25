"""An epic's chain of containers past GitHub's 100-sub-issue limit: the read side.

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
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics
from vergil_tooling.lib.epics import EpicRefusalError, IssueRef

if TYPE_CHECKING:
    from collections.abc import Collection

#: The most items one container holds; the 100th slot of a non-tail container
#: is its continuation (spec §4.1).
ITEM_CAP = 99

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


def _walk(head: IssueRef) -> list[_Step]:
    """Walk *head*'s chain, one raw container read each, checking every continuation."""
    steps: list[_Step] = []
    current = head
    while True:
        listing = epics._container_listing(current)
        continuations: list[IssueRef] = []
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
            continuations.append(child)
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
