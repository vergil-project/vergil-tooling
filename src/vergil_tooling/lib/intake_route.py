"""Deterministic routing between the container epics (epics vergil-project/.github#176, #508).

Every queue review decides, per item, one route; this module owns the mechanics
so no skill hand-rolls them. An item is routed out of the **container epic** it
sits in (intake, planning, ad hoc or deferred), and :data:`ROUTES` says which
routes each container allows:

- **drop** — comment the reason, naming the queue it was dropped from, and
  close (not planned).
- **defer** — move the item into its queue home's deferred epic and record why
  it waits and when to look again, plus the container it came ``from``; on an
  item already deferred it is a **refresh**: a new record and no move,
  carrying forward the ``Deferred from:`` line of the item's latest trusted
  deferral record that has one, and saying on stderr when none has
  (vergil-tooling#5224). The link lands before the record, and a record that
  fails after the move raises with the full intended comment.
- **intake** — reparent a deferred item back onto its home's intake epic.
- **one-off** — move the item into its target repo and link it under that
  repo's ad-hoc epic (or a named finite epic). A move *transfers* the issue,
  keeping its history; when GitHub refuses the transfer it falls back to
  recreating the item in the target repo with a link back and closing the
  original with a pointer — and says so on stderr. Before a transfer the item
  is unlinked from its queue epic, so no link is left for the transfer to
  carry over minutes after the route reported success (#4291); a refused
  transfer links it back before the recreate. A transfer can still carry the
  item's old parent across (sometimes only after a delay), so the moved
  issue is *reparented* with ``replaceParent`` and its final parent verified
  after a settle delay, replaced again if the old parent came back late
  (#3556). A matching read is not enough: it must hold on a second read after
  another settle delay, and the old parent's sub-issue list (what the queue
  views read) must no longer hold the item (#4004). GitHub can also list the
  moved item under the target epic while its parent still reads the old one,
  and then refuses every link as a duplicate sub-issue: the stale old-parent
  link is removed and the parent read back the same way (#3839). A recreated
  issue starts unparented and is simply linked. A one-off whose work lands in the item's
  own repo is just reparented. On every path the parent is read back, and one
  other than the target epic fails loudly with :class:`RouteHalfAppliedError`
  naming the item, where it is and how to recover it, as does a link or
  read-back that fails outright. A planning item's ``in-progress`` label is
  removed from its final issue once the move has settled (a recreate never
  carries it). An item routed one-off is no longer an epic candidate, so its
  estimated size ends there (epic vergil-project/.github#1191), from intake,
  planning or deferred alike: a recreate never carries an ``epic-size:*``
  label, and a transfer or a same-repo one-off removes every one from the
  final issue once the move has settled. When the item carried any size (an
  ``epic-size:*`` label, or a ``Sized …`` comment by anyone, so this needs no
  trusted authors configured), the final issue of a transfer or a same-repo
  one-off then gets the cleared marker, ending its sizing history; a
  recreated issue carries no comments to end. A failed step here raises
  :class:`RouteHalfAppliedError` naming the moved item and the by-hand fix.
  The parent field alone never proves the link: every read-back
  also needs the target epic's sub-issue list to hold the item, and a round
  that finds it missing links it again (#4291). A re-run on an item already
  in the target repo whose parent reads the target epic, but which that epic
  does not list or a queue epic of its own or its org's queue home still
  lists, links it under the target (if missing), only then removes the stale
  queue listing, and reads it back twice, instead of being refused (#4291).
  A one-off never goes under the hoshin epic, which holds only the hoshin
  review's logbook tasks: the ``hoshin`` sentinel is refused by name before
  anything resolves, so a refused route creates nothing, and an explicit ref
  to a hoshin epic is refused once it resolves. A one-off may go under the
  operations epic (``--epic operations``), so an operational problem captured
  by hand reaches the operations queue through intake review.
- **planning** — reparent onto the planning epic of the same queue home,
  from intake, deferred or ad hoc (an ad-hoc task found too big for one PR,
  #4598, refused while an open PR would close it), or an unparented issue.
  It also takes a **follow-on brainstorm** out of a development epic
  (#4296): an item whose title starts ``Follow-on brainstorm:`` and whose
  native parent is a development epic. A brainstorm never gates its epic's work, so moving it
  suspends nothing. The parent is replaced in one call, then the hand-off is
  recorded on the epic (for the retrospective's §5) and on the item; a record
  that fails after the move raises with every comment still to post. With a
  sizing (epic vergil-project/.github#1191) it then sizes the item as ``size``
  does; the sizing is validated before the move, and one that fails after it
  raises naming the item, now in planning, and the ``size`` command that
  finishes it. Without one the item keeps any size it already carries.
- **size** — record the item's estimated epic size, from planning or deferred
  only: post the dated sizing comment (the record), then, in one edit, add the
  derived ``epic-size:*`` label and remove every other one, after ensuring the
  label exists. It neither moves nor closes the item. A label step that fails
  after the comment raises :class:`RouteHalfAppliedError` with the command
  that finishes it (re-running the same ``size`` is safe).
- **split** — the replacement tasks already exist (made with
  ``vrg-issue-create``); comment the links and the reason, then close.

A **security** item (the ``security`` label and at least one ``Security
advisory:`` line, epics vergil-project/.github#517 and #573) keeps its advisory
links: a split or a recreate retargets the advisory (a recreate: every
advisory, in body order) to the new issue(s) *before* the original closes, and
stops with the original open if that fails; a transfer retargets every
advisory after the move and, if one fails, reports the moved-but-not-retargeted
state with the exact recovery command for each advisory still pending. Every
retarget moves the advisory ``--from`` the routed item and keeps every other
issue it tracks, so a route never stops tracking another issue; a one-off
reads those issues before it moves anything. A split
of an item naming several advisories is refused before any change, since which
advisory goes to which task cannot be inferred. The planning route leaves the
advisories alone. The advisory lines a split or a move reads come only from a
body the trust layer trusts: an untrusted body is a stop before any write
(:class:`~vergil_tooling.lib.trust.UntrustedContentError`), and a recreate
copies that same trusted body, read before the move.

An item under the hoshin epic, an ops issue under the operations epic, a
task in a development epic, an epic, and an unparented issue are refused
(``planning`` alone accepts an unparented issue, the ``epic-create`` free-text
path, and a follow-on brainstorm under a development epic), the refusal naming
the parent. A planning item refuses drop, defer and
one-off while any of its development epics is open (one an ``Epic:`` marker in a
trusted comment names, as the trust layer judges it, or
one whose body carries a ``Planning:`` line for it; #4001, #4170). An
ad-hoc item refuses defer and planning while an open PR would close it by a
reference the trust layer counts (:mod:`~vergil_tooling.lib.closing_refs`); an
untrusted body or hand link never holds the route. The whole route runs under
the item owner's App token (:func:`github.target_org`).

Every epic a route reads or writes may be a **chain** of containers past
GitHub's 100-sub-issue limit (epic vergil-project/.github#1609): every link,
move and unlink goes through :mod:`~vergil_tooling.lib.epic_chain`, so an
item in a continuation is found there and a link into a full queue rolls
over. Settling after a transfer is the chain layer's too: the parent field
is observed against the listings of every container of the epic's chain
(:func:`~vergil_tooling.lib.epic_chain.observe_parent`), a stale listing is
cleared from the container that holds it
(:func:`~vergil_tooling.lib.epic_chain.clear_stale_listing`), and a forced
re-link goes into the container the parent field names
(:func:`~vergil_tooling.lib.epic_chain.relink`). The route makes no raw
sub-issue read or write of its own.

An item opened by an outsider (its author holds no role, as the trust layer
judges it) is never routed or sized: every route on it is a stop before
anything resolves or is written
(:class:`~vergil_tooling.lib.trust.UntrustedContentError`, metadata only;
spec §5.3 of epic vergil-project/.github#1467). The human decides next.
"""

from __future__ import annotations

import contextlib
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, cast

from vergil_tooling.lib import (
    closing_refs,
    epic_chain,
    epics,
    github,
    security_records,
    trust,
    trusted_comments,
)
from vergil_tooling.lib.epic_size import (
    LABEL_PREFIX,
    Sizing,
    has_size,
    label_for,
    render_cleared_marker,
    render_sizing_comment,
)
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.labels import (
    RETIRED_LABELS,
    UnknownLabelError,
    ensure_labels,
    load_labels,
)
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.queue_view import (
    PLANNING_MARKER,
    parse_epic_markers,
)
from vergil_tooling.lib.shell_ref import quoted_slug
from vergil_tooling.lib.trusted_authors import load_trusted_authors

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence

    from vergil_tooling.lib.trusted_authors import TrustedAuthors

DROP_COMMENT = "Dropped in {queue} review: {reason}"
DEFER_COMMENT = "Deferred {day}: {reason}\nRevisit when: {revisit_when}"
DEFERRED_FROM_LINE = "Deferred from: {epic}"
# A deferral record's first line, as DEFER_COMMENT writes it.
_DEFER_RECORD_RE = re.compile(r"\ADeferred \d{4}-\d{2}-\d{2}: ")
_DEFERRED_FROM_RE = re.compile(r"^Deferred from: \S+$", re.MULTILINE)
SPLIT_COMMENT = "Split into: {refs}. {reason}"
MOVED_TO_COMMENT = "Moved to {url}"
MOVED_FROM_LINE = "Moved from {url}"
# The title a follow-on brainstorm carries (epic-create's forward bookend, #4296).
BRAINSTORM_TITLE_PREFIX = "Follow-on brainstorm:"
BRAINSTORM_EPIC_COMMENT = "Follow-on brainstorm handed to planning: {item} (now under {planning})"
BRAINSTORM_ITEM_COMMENT = "Handed to planning from: {epic}"
# The kind _check_eligible returns for a follow-on brainstorm under a development epic.
_DEVELOPMENT = "development"
# The refusal of a one-off into the hoshin epic (epic vergil-project/.github#1417).
HOSHIN_REFUSAL = (
    "{epic} is the hoshin epic, which holds only the logbook tasks /vergil:hoshin "
    "files and works; a one-off goes under an ad-hoc or finite epic"
)
# The refusal of every route for an item under the hoshin epic: /vergil:hoshin
# alone places and works its tasks, so none is ever routed.
HOSHIN_ITEM_REFUSAL = (
    "{item} is under the hoshin epic {epic}, which holds only the logbook tasks "
    "/vergil:hoshin files and works; its tasks are never routed"
)
# The refusal of every route for an ops issue (epic vergil-project/.github#1494):
# it stays under the operations epic until it is closed by hand.
OPERATIONS_ITEM_REFUSAL = (
    "{item} is an ops issue under {epic}; it is closed by hand once dealt with, never routed"
)

# A transfer carries the old parent over eventually, not atomically: a link
# made right after it can be overwritten seconds later (#3556). Each settle
# round replaces the parent, waits, then re-reads it.
_SETTLE_ROUNDS = 4
_SETTLE_SECONDS = 3.0

_ISSUE_URL_RE = re.compile(r"https?://[^/\s]+/([^/\s]+)/([^/\s]+)/issues/([0-9]+)")


class RouteRefusedError(ValueError):
    """A route that must not run: wrong container, cross-org, or a refused target."""


class RouteHalfAppliedError(RuntimeError):
    """A route that changed the item but could not finish.

    A one-off that moved or linked the item but could not settle it under its
    epic, or a sizing whose comment posted but whose label did not.
    """


@dataclass(frozen=True)
class Drop:
    reason: str


@dataclass(frozen=True)
class Defer:
    reason: str
    revisit_when: str
    day: date | None = None  # overrides today (e.g. to keep an original deferral date)


@dataclass(frozen=True)
class OneOff:
    repo: str  # target "owner/repo"
    epic: str = "adhoc"  # 'adhoc', '#N' (target repo's epic home), or 'owner/repo#N'


@dataclass(frozen=True)
class ToPlanning:
    sizing: Sizing | None = None  # sized once in planning; None leaves any size as it is


@dataclass(frozen=True)
class SizeItem:
    """Record an estimated epic size (named so as not to clash with ``epic_size.Size``)."""

    sizing: Sizing


@dataclass(frozen=True)
class ToIntake:
    pass


@dataclass(frozen=True)
class Split:
    into: tuple[IssueRef, ...]
    reason: str


RouteAction = Drop | Defer | OneOff | ToPlanning | ToIntake | Split | SizeItem

# The routes each container allows, keyed by :func:`epics.container_kind` of the
# item's parent; ``None`` is an unparented item. A parent of any other kind (a
# development epic, an archive) allows nothing.
ROUTES: dict[str | None, frozenset[type]] = {
    "intake": frozenset({Drop, Defer, OneOff, ToPlanning, Split}),
    "planning": frozenset({Defer, Drop, OneOff, SizeItem}),
    "ad-hoc": frozenset({Defer, ToPlanning}),
    "deferred": frozenset({Defer, ToIntake, ToPlanning, OneOff, Drop, Split, SizeItem}),
    None: frozenset({ToPlanning}),
}

# The CLI name of each route, for refusals.
_ROUTE_NAMES: dict[type, str] = {
    Drop: "drop",
    Defer: "defer",
    OneOff: "one-off",
    ToPlanning: "planning",
    ToIntake: "intake",
    Split: "split",
    SizeItem: "size",
}


@dataclass(frozen=True)
class RouteResult:
    """Where the item ended up and how it got there."""

    final: IssueRef
    moved_by: Literal["none", "transfer", "recreate"]


def route(ref: IssueRef, action: RouteAction, *, today: date | None = None) -> RouteResult:
    """Apply *action* to the container item *ref*; raise :class:`RouteRefusedError` to refuse."""
    with github.target_org(ref.owner):
        return _route(ref, action, today or date.today())


def _repo_of(ref: IssueRef) -> str:
    return f"{ref.owner}/{ref.repo}"


def _route(ref: IssueRef, action: RouteAction, today: date) -> RouteResult:
    if isinstance(action, OneOff) and action.epic == epics.HOSHIN.sentinel:
        # Refused by name before anything resolves: resolving the sentinel
        # would create the hoshin epic.
        raise RouteRefusedError(HOSHIN_REFUSAL.format(epic=f"--epic {action.epic}"))
    _refuse_outsider_opened(ref)
    parent = epics.parent_of(ref)
    try:
        kind = _check_eligible(ref, action, parent)
    except RouteRefusedError:
        # A re-run of a one-off that already moved the item, while a queue epic
        # still lists it, clears that listing instead of being refused (#4291).
        if (
            isinstance(action, OneOff)
            and parent is not None
            and _clear_stale_queue_listing(ref, action, parent)
        ):
            return RouteResult(final=ref, moved_by="none")
        raise
    if isinstance(action, Drop):
        if kind == epics.PLANNING.key:
            _guard_open_planning_epics(ref, "dropping")
        _close(ref, DROP_COMMENT.format(queue=kind, reason=action.reason), reason="not planned")
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, Defer):
        # _check_eligible refuses an unparented defer, so *parent* is set here.
        _defer(ref, action, kind, cast("IssueRef", parent), action.day or today)
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, Split):
        if not action.into:
            raise RouteRefusedError("split needs at least one --into task")
        refs = ", ".join(task.slug for task in action.into)
        ghsas = _item_advisories(ref)
        if len(ghsas) > 1:
            # Which advisory goes to which task cannot be inferred: refuse before any change.
            raise RouteRefusedError(
                f"{ref.slug} names several advisories ({', '.join(ghsas)}); retarget each "
                f"by hand to the tasks that fix it, keeping every other issue it tracks "
                f"({_retarget_commands(ghsas, ref, None)}), then split"
            )
        if ghsas:
            [ghsa] = ghsas
            try:
                tracked = _read_tracked(ref, ghsas)
                _retarget(tracked, ref, ghsa, tracked.targets(ghsa, action.into))
            except _RETARGET_ERRORS as exc:
                raise RouteRefusedError(
                    f"{ref.slug}: advisory {ghsa} was not retargeted to {refs} ({exc}); "
                    f"{ref.slug} is left open"
                ) from exc
        _close(ref, SPLIT_COMMENT.format(refs=refs, reason=action.reason), reason="completed")
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, SizeItem):
        _apply_sizing(ref, action.sizing, today)
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, ToPlanning):
        if kind == epics.ADHOC.key:
            _guard_open_closing_pr(ref, "routing to planning")
        planning = epics.ensure_planning_epic(_repo_of(ref))
        _link(planning, ref)
        if kind == _DEVELOPMENT:
            # _check_eligible only returns it for a parented item.
            try:
                _record_brainstorm_handoff(ref, cast("IssueRef", parent), planning)
            except RuntimeError as exc:
                if action.sizing is None:
                    raise
                raise RuntimeError(
                    f"{exc}; then size it with: {_size_command(ref, action.sizing)}"
                ) from exc
        if action.sizing is not None:
            _size_in_planning(ref, action.sizing, planning, today)
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, ToIntake):
        intake = epics.ensure_intake_epic(_repo_of(ref))
        _link(intake, ref)
        return RouteResult(final=ref, moved_by="none")
    return _one_off(ref, action, parent, kind, today)


def _refuse_outsider_opened(ref: IssueRef) -> None:
    """Stop when an outsider opened *ref*: a tool pointed at it acts on nothing.

    Only the author decides it: a trusted item with untrusted comments, or
    with a body an outsider edited, is routed (its body is judged where a
    route reads it, :func:`_trusted_body`).
    """
    opened = trust.outsider_opened(trust.read_issue(_repo_of(ref), ref.number))
    if opened is not None:
        raise trust.UntrustedContentError(opened, "the item was opened by an untrusted author")


def _check_eligible(ref: IssueRef, action: RouteAction, parent: IssueRef | None) -> str | None:
    """Refuse *action* unless :data:`ROUTES` allows it from *ref*'s container.

    Returns the container kind of *parent* (None for an unparented item).
    """
    name = _ROUTE_NAMES[type(action)]
    if parent is None:
        if type(action) in ROUTES[None]:
            return None
        raise RouteRefusedError(
            f"{ref.slug} has no parent; only an item in a container epic (intake, planning, "
            "ad hoc or deferred) can be routed (the planning route also accepts an "
            "unparented issue)"
        )
    kind = epics.container_kind(parent)
    if kind is None and isinstance(action, ToPlanning) and _is_movable_brainstorm(ref, parent):
        return _DEVELOPMENT
    if kind == epics.HOSHIN.key:
        raise RouteRefusedError(HOSHIN_ITEM_REFUSAL.format(item=ref.slug, epic=parent.slug))
    if kind == epics.OPERATIONS.key:
        raise RouteRefusedError(OPERATIONS_ITEM_REFUSAL.format(item=ref.slug, epic=parent.slug))
    if kind is None or kind not in ROUTES:
        raise RouteRefusedError(
            f"{ref.slug} is under {parent.slug}, not a container epic (intake, planning, "
            "ad hoc or deferred); a task in a development epic cannot be routed or "
            "deferred, since deferring one task in effect suspends its whole epic"
        )
    allowed = ROUTES[kind]
    if type(action) not in allowed:
        names = ", ".join(sorted(_ROUTE_NAMES[route_type] for route_type in allowed))
        raise RouteRefusedError(
            f"{ref.slug} is in the {kind} epic {parent.slug}; the {name} route is not "
            f"allowed from {kind} (allowed: {names})"
        )
    return kind


def _is_movable_brainstorm(ref: IssueRef, parent: IssueRef) -> bool:
    """Whether *ref* is a follow-on brainstorm the planning route may take from *parent*.

    It must be a task (not an epic or archive) titled ``Follow-on brainstorm:``,
    and *parent* must be a development epic (an ``epic`` that is no standing
    epic, which the caller has already ruled out) and *ref*'s native parent: a
    body ``Parent:`` line alone would survive the reparent and still name the
    epic. False leaves the development-epic refusal standing.
    """
    view = _view(ref, "title,labels")
    if not str(view.get("title") or "").startswith(BRAINSTORM_TITLE_PREFIX):
        return False
    if epics.non_task_kind_of_labels(_label_names(view)) is not None:
        return False
    if not epics.is_epic(parent):
        return False
    return epics.native_parent_of(ref) == parent


def _record_brainstorm_handoff(ref: IssueRef, epic: IssueRef, planning: IssueRef) -> None:
    """Record a brainstorm's move to *planning* on its development *epic* and on *ref*.

    The move has already happened, so a failed comment is not a refusal: it
    raises naming every comment still to post by hand (re-running the route
    would be refused, the item now being in planning).
    """
    pending = [
        (epic, BRAINSTORM_EPIC_COMMENT.format(item=ref.slug, planning=planning.slug)),
        (ref, BRAINSTORM_ITEM_COMMENT.format(epic=epic.slug)),
    ]
    for index, (target, body) in enumerate(pending):
        try:
            _comment(target, body)
        except subprocess.CalledProcessError as exc:
            by_hand = "; ".join(f"on {where.slug}: {text}" for where, text in pending[index:])
            raise RuntimeError(
                f"{ref.slug} moved from {epic.slug} to the planning epic {planning.slug}, but "
                f"recording the hand-off failed ({' '.join(error_text(exc).split())}). Post "
                f"by hand {by_hand}"
            ) from exc


def _size_command(ref: IssueRef, sizing: Sizing) -> str:
    """The paste-ready ``vrg-intake-route … size`` command that records *sizing* on *ref*."""
    return (
        f"vrg-intake-route {quoted_slug(ref)} size --tasks {sizing.tasks} "
        f"--confidence {sizing.confidence} --reason {shlex.quote(sizing.reason)}"
    )


def _apply_sizing(ref: IssueRef, sizing: Sizing, day: date) -> None:
    """Post *sizing*'s comment on *ref*, then set its derived ``epic-size:*`` label.

    The comment is the record, so it goes first, through the same path as the
    other routes' comments (under the item owner's token). The label is then
    set in one edit that adds it and removes every other ``epic-size:*`` label
    present, after ensuring it exists. A failed comment changes nothing and
    propagates as it came; a label step that fails after it raises
    :class:`RouteHalfAppliedError` with the command that finishes it (a second
    comment with the same values only adds history).
    """
    _comment(ref, render_sizing_comment(sizing, day))
    label = label_for(sizing.size)
    try:
        stale = sorted(
            name
            for name in _label_names(_view(ref, "labels"))
            if name.startswith(LABEL_PREFIX) and name != label
        )
        ensure_labels(_repo_of(ref), [label])
        edit = ["issue", "edit", str(ref.number), "--repo", _repo_of(ref), "--add-label", label]
        for name in stale:
            edit += ["--remove-label", name]
        github.run(*edit)
    except github.MissingGitHubTokenError:
        raise
    except (subprocess.CalledProcessError, RuntimeError, UnknownLabelError) as exc:
        raise RouteHalfAppliedError(
            f"{ref.slug}: sizing comment posted, label not set: setting '{label}' failed "
            f"({' '.join(error_text(exc).split())}). Finish it with: {_size_command(ref, sizing)}"
        ) from exc


def _size_in_planning(ref: IssueRef, sizing: Sizing, planning: IssueRef, day: date) -> None:
    """Size *ref*, just routed into *planning*; a failure says it is in planning now.

    Re-running the planning route would be refused (the item is in planning),
    so the failure names the ``size`` command that finishes it.
    """
    moved = f"{ref.slug} is now in the planning epic {planning.slug}"
    try:
        _apply_sizing(ref, sizing, day)
    except RouteHalfAppliedError as exc:
        raise RouteHalfAppliedError(f"{moved}, but {exc}") from exc
    except github.MissingGitHubTokenError:
        raise
    except subprocess.CalledProcessError as exc:
        raise RouteHalfAppliedError(
            f"{moved}, unsized: posting its sizing comment failed "
            f"({' '.join(error_text(exc).split())}). Size it with: {_size_command(ref, sizing)}"
        ) from exc


def _view(ref: IssueRef, fields: str) -> dict[str, Any]:
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", _repo_of(ref), "--json", fields
    )
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected 'gh issue view' response for {ref.slug}: {raw!r}")
    return raw


def _label_names(view: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in view.get("labels") or []}


def _trusted_planning_body(epic: IssueRef) -> str | None:
    """*epic*'s body when the trust layer trusts it; otherwise ``None``, reported.

    The untrusted body is reported on stderr, metadata only; the comments'
    skips are not this reader's concern. A read the layer cannot complete
    raises :class:`~vergil_tooling.lib.trust.TrustReadError`.
    """
    content = trust.read_issue(_repo_of(epic), epic.number)
    trust.report_skipped(
        [item for item in content.skipped if item.field == "body"], stream=sys.stderr
    )
    return content.body


def _planning_epic_states(ref: IssueRef) -> dict[IssueRef, str]:
    """Each development epic of the planning item *ref* and its state, OPEN or CLOSED.

    The epics are those an ``Epic:`` marker in *ref*'s trusted comments names
    (read through the trust layer; each skipped comment is reported on stderr),
    plus every ``epic`` issue in *ref*'s org whose body carries a
    ``Planning: <ref>`` line (``epic-create`` writes that line into each epic it
    creates, while the marker names only one). The search is full text, so each
    hit's body is checked for the exact line, and the line counts only in a body
    the trust layer trusts: each hit that carries it is read again through the
    layer (:func:`_trusted_planning_body`). A search that reaches its limit, a
    state that is neither OPEN nor CLOSED, or a failed read raises: nothing is
    assumed closed.
    """
    states: dict[IssueRef, str] = {}
    for epic in parse_epic_markers(trusted_comments.bodies(ref)):
        states[epic] = github.read_output(
            "api", f"repos/{_repo_of(epic)}/issues/{epic.number}", "--jq", ".state"
        ).upper()
    results: Any = github.read_json(
        "search",
        "issues",
        f"{PLANNING_MARKER} {ref.slug}",
        "--owner",
        ref.owner,
        "--label",
        "epic",
        "--limit",
        str(epics.LISTING_LIMIT),
        "--json",
        "number,state,repository,body",
    )
    if not isinstance(results, list):
        raise RuntimeError(f"unexpected 'gh search issues' response for {ref.slug}: {results!r}")
    epics.refuse_truncated(results, epics.LISTING_LIMIT, f"the {PLANNING_MARKER} {ref.slug} search")
    line = re.compile(rf"^{re.escape(PLANNING_MARKER)}\s*{re.escape(ref.slug)}\s*$", re.MULTILINE)
    for item in results:
        if not line.search(str(item.get("body") or "")):
            continue
        name_with_owner = str((item.get("repository") or {}).get("nameWithOwner") or "")
        if name_with_owner.count("/") != 1:
            raise RuntimeError(
                f"search hit {item.get('number')!r} for {ref.slug} has no repository"
            )
        owner, repo = name_with_owner.split("/")
        epic = IssueRef(owner, repo, int(item["number"]))
        if epic in states or not line.search(_trusted_planning_body(epic) or ""):
            continue
        states[epic] = str(item["state"]).upper()
    for epic, state in states.items():
        if state not in ("OPEN", "CLOSED"):
            raise RuntimeError(f"unexpected state {state!r} for {epic.slug}")
    return states


def _guard_open_planning_epics(ref: IssueRef, doing: str) -> None:
    """Refuse *doing* a planning item while an epic of it is open.

    *doing* is the route: dropping, deferring or routing one-off. Once every
    development epic is closed nothing is left to orphan (#4170).
    """
    open_epics = [
        epic.slug for epic, state in _planning_epic_states(ref).items() if state == "OPEN"
    ]
    if open_epics:
        raise RouteRefusedError(
            f"{ref.slug} has an open development epic ({', '.join(open_epics)}); {doing} "
            "it would orphan that epic. Close every development epic first"
        )


def _guard_open_closing_pr(ref: IssueRef, doing: str) -> None:
    """Refuse *doing* an ad-hoc item that an open PR would close (it would strand the PR).

    *doing* is the route: deferring or routing to planning. Either takes the
    item out of the ad-hoc epic, and the PR tooling refuses a queued item.
    The PRs GitHub lists as closing the item are only candidates: one
    refuses the route only while it is open and the trust layer counts its
    reference to the item (:func:`_open_pr_closes`), so an untrusted PR body
    or hand link never blocks routing. Each one passed over is reported on
    stderr, metadata only.
    """
    view = _view(ref, "closedByPullRequestsReferences")
    authors = load_trusted_authors()
    for pr in view.get("closedByPullRequestsReferences") or []:
        url = str(pr.get("url") or "")
        if not url:
            raise RuntimeError(f"closing PR of {ref.slug} has no URL: {pr!r}")
        if _open_pr_closes(ref, url, authors):
            raise RouteRefusedError(
                f"{ref.slug} has an open PR that would close it ({url}); {doing} it would "
                "strand that work — finish or close the PR first"
            )


_PR_URL_RE = re.compile(r"https://github\.com/(?P<repo>[^/\s]+/[^/\s]+)/pull/(?P<number>[1-9]\d*)")


def _open_pr_closes(ref: IssueRef, url: str, authors: TrustedAuthors) -> bool:
    """True when the PR at *url* is open and the trust layer counts its reference to *ref*.

    The PR is read by number through :func:`closing_refs.read_closing_refs`;
    a read the layer cannot complete raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`. An open PR whose
    reference is not counted is reported on stderr and does not count.
    """
    match = _PR_URL_RE.fullmatch(url)
    if match is None:
        raise RuntimeError(f"closing PR of {ref.slug} has no PR URL: {url!r}")
    refs = closing_refs.read_closing_refs(match["repo"], int(match["number"]), authors=authors)
    if refs.state != "OPEN":
        return False
    trust.report_skipped(refs.skipped, stream=sys.stderr)
    item = _repo_of(ref).lower()
    if any(issue.repo.lower() == item and issue.number == ref.number for issue in refs.counted):
        return True
    print(
        f"{ref.slug}: open PR {refs.ref} would close it by a reference the trust layer "
        "does not count; it does not hold the route",
        file=sys.stderr,
    )
    return False


def _defer(ref: IssueRef, action: Defer, kind: str | None, parent: IssueRef, day: date) -> None:
    """Move *ref* into its home's deferred epic and record why; a refresh only records."""
    record = DEFER_COMMENT.format(
        day=day.isoformat(), reason=action.reason, revisit_when=action.revisit_when
    )
    if kind == epics.DEFERRED.key:
        origin = _deferred_from(ref)
        if origin is None:
            print(
                f"{ref.slug}: no earlier deferral record of it carries a 'Deferred from:' "
                "line, so the refreshed record has none",
                file=sys.stderr,
            )
            _comment(ref, record)
        else:
            _comment(ref, f"{record}\n{origin}")
        return
    view = _view(ref, "labels")
    if kind == epics.PLANNING.key:
        _guard_open_planning_epics(ref, "deferring")
    if kind == epics.ADHOC.key:
        _guard_open_closing_pr(ref, "deferring")
    deferred = epics.ensure_deferred_epic(_repo_of(ref))
    body = f"{record}\n{DEFERRED_FROM_LINE.format(epic=parent.slug)}"
    _link(deferred, ref)
    stranded = f"{ref.slug} is under the deferred epic {deferred.slug} without a deferral record"
    by_hand = (
        f"Post this comment on {ref.slug} by hand (re-running defer would be a refresh "
        f"and lose the 'Deferred from:' line):\n\n{body}"
    )
    if epics.PLANNING_IN_PROGRESS_LABEL in _label_names(view):
        try:
            _remove_in_progress(ref, doing="deferring")
        except RuntimeError as exc:
            raise RuntimeError(f"{stranded}, and {exc}. {by_hand}") from exc
    try:
        _comment(ref, body)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"{stranded}: posting it failed ({' '.join(error_text(exc).split())}). {by_hand}"
        ) from exc


def _deferred_from(ref: IssueRef) -> str | None:
    """The ``Deferred from:`` line of *ref*'s latest deferral record that has one.

    Only *ref*'s trusted comments are read (each skipped one is reported on
    stderr), and only a deferral record counts: a comment whose first line is
    ``Deferred <date>: ``. ``None`` when no such record has the line, as a
    refresh written before vergil-tooling#5224 does not.
    """
    for body in reversed(trusted_comments.bodies(ref)):
        if _DEFER_RECORD_RE.match(body) is None:
            continue
        found = _DEFERRED_FROM_RE.search(body)
        if found is not None:
            return found.group(0)
    return None


def _remove_in_progress(target: IssueRef, *, doing: str) -> None:
    """Remove the planning ``in-progress`` label from *target* (see :func:`_remove_labels`)."""
    _remove_labels(target, [epics.PLANNING_IN_PROGRESS_LABEL], doing=doing)


def _remove_labels(target: IssueRef, labels: Sequence[str], *, doing: str) -> None:
    """Remove every one of *labels* from *target* in one edit.

    Raise :class:`RuntimeError` naming *doing* (the route that removes them)
    and the command that removes them by hand when the edit fails. A missing
    token is a configuration error and is left as it came.
    """
    flags = [arg for label in labels for arg in ("--remove-label", label)]
    try:
        github.run("issue", "edit", str(target.number), "--repo", _repo_of(target), *flags)
    except github.MissingGitHubTokenError:
        raise
    except subprocess.CalledProcessError as exc:
        named = ", ".join(f"'{label}'" for label in labels)
        noun, pronoun = ("label", "it") if len(labels) == 1 else ("labels", "them")
        raise RuntimeError(
            f"{doing} left the {named} {noun} on {target.slug}: removing {pronoun} failed "
            f"({' '.join(error_text(exc).split())}). Fix it with: gh issue edit "
            f"{target.number} --repo {_repo_of(target)} {' '.join(flags)}"
        ) from exc


def _clear_size(final: IssueRef, day: date) -> None:
    """End the estimated size of *final*, an item just routed one-off.

    Read its labels, remove every ``epic-size:*`` one in one edit when any is
    present, then post the cleared marker, so any sizing comment it carried
    no longer counts. Raise :class:`RuntimeError` with the by-hand fix of
    every step not done; a missing token is left as it came.
    """
    marker = render_cleared_marker(day)
    post = f"gh issue comment {final.number} --repo {_repo_of(final)} --body {shlex.quote(marker)}"
    try:
        view = _view(final, "labels")
    except github.MissingGitHubTokenError:
        raise
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        raise RuntimeError(
            f"routing one-off could not read the labels of {final.slug} to clear its size "
            f"({' '.join(error_text(exc).split())}). Remove every '{LABEL_PREFIX}*' label "
            f"from it, then post the cleared marker with: {post}"
        ) from exc
    sizes = sorted(name for name in _label_names(view) if name.startswith(LABEL_PREFIX))
    if sizes:
        try:
            _remove_labels(final, sizes, doing="routing one-off")
        except RuntimeError as exc:
            raise RuntimeError(f"{exc}, then post the cleared marker with: {post}") from exc
    try:
        _comment(final, marker)
    except github.MissingGitHubTokenError:
        raise
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"routing one-off did not clear the size of {final.slug}: posting the cleared "
            f"marker failed ({' '.join(error_text(exc).split())}). Post it with: {post}"
        ) from exc


def _one_off(
    ref: IssueRef, action: OneOff, old_parent: IssueRef | None, kind: str | None, today: date
) -> RouteResult:
    """Route *ref* one-off; it loses ``in-progress`` and any size once the move has settled.

    A planning item is guarded first, before anything that can create the
    target's ad-hoc epic. Whether the item carries a size is read before the
    move, from any container (:func:`has_size`, over the comments the trust
    layer trusts). Both labels come off the item's final issue: a recreate never
    carries them, and a transfer keeps one only when the target repo has it,
    so each is read rather than assumed. Every step is tried, and those that
    fail are reported together in one :class:`RouteHalfAppliedError`.
    """
    view = _view(ref, "labels")
    if kind == epics.PLANNING.key:
        _guard_open_planning_epics(ref, "routing one-off")
    labels = _label_names(view)
    labelled = kind == epics.PLANNING.key and epics.PLANNING_IN_PROGRESS_LABEL in labels
    sized = has_size(labels, trusted_comments.bodies(ref))
    result = _move_one_off(ref, action, old_parent)
    failures: list[RuntimeError] = []
    if labelled:
        try:
            if _still_in_progress(result):
                _remove_in_progress(result.final, doing="routing one-off")
        except RuntimeError as exc:
            failures.append(exc)
    if sized and result.moved_by != "recreate":
        try:
            _clear_size(result.final, today)
        except RuntimeError as exc:
            failures.append(exc)
    if failures:
        # The move has settled: say so, so nobody re-runs a route now refused.
        detail = "; and ".join(str(exc) for exc in failures)
        raise RouteHalfAppliedError(f"{_moved(ref, result.final)}, but {detail}") from failures[0]
    return result


def _still_in_progress(result: RouteResult) -> bool:
    """Whether the routed item's final issue still carries ``in-progress``.

    Raise :class:`RuntimeError` with the by-hand fix when the labels cannot be read.
    """
    if result.moved_by == "recreate":
        return False
    if result.moved_by == "none":
        return True
    final = result.final
    label = epics.PLANNING_IN_PROGRESS_LABEL
    try:
        view = _view(final, "labels")
    except github.MissingGitHubTokenError:
        raise
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        raise RuntimeError(
            f"routing one-off could not read the labels of {final.slug} "
            f"({' '.join(error_text(exc).split())}). If it still carries '{label}', fix it "
            f"with: gh issue edit {final.number} --repo {_repo_of(final)} --remove-label {label}"
        ) from exc
    return label in _label_names(view)


def _move_one_off(ref: IssueRef, action: OneOff, old_parent: IssueRef | None) -> RouteResult:
    source = _repo_of(ref)
    dest = action.repo
    if dest.count("/") != 1 or not all(dest.split("/")):
        raise RouteRefusedError(f"one-off --repo must be owner/repo, got {dest!r}")
    if dest.split("/", 1)[0] != ref.owner:
        raise RouteRefusedError(f"cross-org operation is out of scope: {ref.slug} -> {dest}")
    if action.epic != "adhoc" and "/" in action.epic:
        epic_owner = epics.parse_issue_ref(action.epic, default_repo=dest).owner
        if epic_owner != ref.owner:
            raise RouteRefusedError(
                f"cross-org operation is out of scope: {ref.slug} -> epic {action.epic}"
            )
    epic = epics.resolve_epic_ref(action.epic, repo=dest)
    if epics.queue_kind(epic) is not None:
        raise RouteRefusedError(
            f"{epic.slug} is a queue epic; a one-off goes under an ad-hoc or finite epic "
            "(use the planning route for planning)"
        )
    if epics.container_kind(epic) == epics.HOSHIN.key:
        raise RouteRefusedError(HOSHIN_REFUSAL.format(epic=epic.slug))
    if dest == source:
        # The link refuses a closed epic before it writes (closed is closed).
        _link_and_check(ref, epic, ref, old_parent)
        return RouteResult(final=ref, moved_by="none")
    # Read before the move: a transfer drops labels the target repo lacks, and
    # a recreate copies this trusted body.
    body, ghsas = _item_security(ref)
    tracked = _tracked_before_move(ref, ghsas)
    # Closed is closed (.github#1609): a closed target is refused before the
    # first write, so the item is never moved out from under its queue.
    epics.require_open_epic(epic)
    unlinked = _unlink_before_transfer(ref, old_parent)
    try:
        output = github.read_output("issue", "transfer", str(ref.number), dest, "--repo", source)
    except github.MissingGitHubTokenError:
        raise
    except subprocess.CalledProcessError as exc:
        if unlinked is not None:
            _relink_after_failed_transfer(ref, unlinked, dest, exc)
        new = _recreate(ref, dest, exc, tracked, body)
        # A recreated issue has no parent to carry over: link it, then check.
        _link_and_check(ref, epic, new, None)
        return RouteResult(final=new, moved_by="recreate")
    final = _transferred_ref(ref, dest, epic, output)
    if tracked is None:
        with _half_applied_on_error(ref, epic, final):
            _settle_parent(ref, epic, final, old_parent)
        return RouteResult(final=final, moved_by="transfer")
    targets = {ghsa: tracked.targets(ghsa, (final,)) for ghsa in ghsas}
    try:
        with _half_applied_on_error(ref, epic, final):
            _settle_parent(ref, epic, final, old_parent)
    except RouteHalfAppliedError as exc:
        recovery = _transfer_recovery(ghsas, ref, targets)
        raise RouteHalfAppliedError(f"{exc}; also, {recovery}") from exc
    for index, ghsa in enumerate(ghsas):
        try:
            # The old number is the same issue: rewrite its tracking line, never unmark it.
            # The source is the old number, which the tracking line still names.
            _retarget(tracked, ref, ghsa, targets[ghsa], renamed=(ref,))
        except _RETARGET_ERRORS as exc:
            # The move already happened, so this is not a refusal: report what is left.
            pending = _transfer_recovery(ghsas[index:], ref, targets)
            raise RuntimeError(f"{ref.slug} moved to {final.slug}, but {pending}") from exc
    return RouteResult(final=final, moved_by="transfer")


def _unlink_before_transfer(ref: IssueRef, old_parent: IssueRef | None) -> IssueRef | None:
    """Unlink *ref* from its queue epic before a transfer; return that epic, or None.

    A transfer carries the item's sub-issue link across on its own schedule,
    sometimes minutes after the route has checked and reported the move done
    (#4291), so the queue epic listed it again. With no link left at transfer
    time there is nothing to carry. An item whose parent is only a body
    ``Parent:`` line has no native link to remove. Nothing has moved yet, so
    a failure here says so.
    """
    if old_parent is None or epics.native_parent_of(ref) != old_parent:
        return None
    try:
        epic_chain.unlink_from(old_parent, ref)
        current = epics.native_parent_of(ref)
    except (github.GitHubAPIError, RuntimeError, ValueError) as exc:
        raise RouteHalfAppliedError(
            f"unlinking {ref.slug} from {old_parent.slug} before its transfer failed "
            f"({error_text(exc)}); nothing was moved. Check where it is with vrg-queue; if it "
            f"is unparented, fix it with: vrg-epic-move --task {quoted_slug(ref)} "
            f"--epic {quoted_slug(old_parent)}, then route it again"
        ) from exc
    if current is not None:
        raise RouteHalfAppliedError(
            f"unlinked {ref.slug} from {old_parent.slug} before its transfer, but it is "
            f"{_placement(current)}; nothing was moved. Check where it is with vrg-queue, "
            "then route it again"
        )
    return old_parent


def _relink_after_failed_transfer(
    ref: IssueRef, old_parent: IssueRef, dest: str, transfer_error: subprocess.CalledProcessError
) -> None:
    """Put *ref* back under *old_parent* when the transfer is refused, before the recreate."""
    try:
        epic_chain.link_into(old_parent, ref)
    except (github.GitHubAPIError, RuntimeError, ValueError) as exc:
        raise RouteHalfAppliedError(
            f"the transfer of {ref.slug} to {dest} failed "
            f"({' '.join(error_text(transfer_error).split())}), and linking it back under "
            f"{old_parent.slug} failed ({error_text(exc)}); it is unparented and nothing was "
            f"moved. Fix it with: vrg-epic-move --task {quoted_slug(ref)} "
            f"--epic {quoted_slug(old_parent)}, then route it again"
        ) from exc


def _not_retargeted(ghsas: tuple[str, ...]) -> str:
    """``advisory A was`` / ``advisories A, B were``: the advisories left behind."""
    if len(ghsas) == 1:
        return f"advisory {ghsas[0]} was"
    return f"advisories {', '.join(ghsas)} were"


def _retarget_commands(
    ghsas: Sequence[str],
    source: IssueRef,
    targets: Mapping[str, Sequence[IssueRef]] | None,
) -> str:
    """One ``vrg-security-record retarget`` command per advisory, in order.

    Each moves its advisory ``--from`` *source*, an issue it tracks, to the
    target set the route uses for it (``targets[ghsa]``). With *targets*
    None, the tasks are not known, and each command ends in a ``<task>``
    placeholder for the human to fill in.
    """
    commands = []
    for ghsa in ghsas:
        if targets is None:
            commands.append(
                f"{security_records.retarget_command(ghsa, source, ())} --to <task> [--to …]"
            )
        else:
            commands.append(security_records.retarget_command(ghsa, source, targets[ghsa]))
    return ", then ".join(commands)


def _transfer_recovery(
    ghsas: Sequence[str], source: IssueRef, targets: Mapping[str, Sequence[IssueRef]]
) -> str:
    """What is left after a transfer: every advisory *ghsas* names, with its command.

    *source* is the item's pre-transfer ref, which the tracking lines still name.
    """
    commands = _retarget_commands(ghsas, source, targets)
    return f"{_not_retargeted(tuple(ghsas))} not retargeted; run: {commands}"


# The failures that leave an advisory not retargeted: the retarget itself, or
# reading the org's advisory home (an unreadable ``.github``).
_RETARGET_ERRORS = (
    security_records.RetargetError,
    github.GitHubAPIError,
)


def _security_ghsas(ref: IssueRef, labels: set[str], body: str) -> tuple[str, ...]:
    """Every advisory *ref* tracks, in body order, given its *labels* and *body*.

    An item is a security item only with both the ``security`` label and at
    least one advisory line; one without the other is a broken pair, warned
    about here (the scheduled sweep reports it too) and left for a human to
    repair. Empty when *ref* is not a security item.
    """
    ghsas = security_records.parse_advisory_refs(body)
    labelled = security_records.SECURITY_LABEL in labels
    if labelled and ghsas:
        return ghsas
    if labelled or ghsas:
        has, lacks = (
            ("the security label", "a Security advisory: line")
            if labelled
            else ("a Security advisory: line", "the security label")
        )
        print(
            f"intake-route: {ref.slug} has {has} but not {lacks}; "
            "no advisory retargeted (a broken security pair)",
            file=sys.stderr,
        )
    return ()


def _trusted_body(ref: IssueRef) -> str:
    """*ref*'s body, read through the trust layer; an untrusted body is a stop.

    The routed item's body is load-bearing: its ``Security advisory:`` lines
    pick the advisories a route retargets, and a recreate copies it. So a body
    whose author or an editor holds no role raises
    :class:`~vergil_tooling.lib.trust.UntrustedContentError` (metadata only),
    before any write, and nothing falls back to an untrusted copy.
    """
    content = trust.read_issue(_repo_of(ref), ref.number, require_body=True)
    return cast("str", content.body)


def _item_security(ref: IssueRef) -> tuple[str, tuple[str, ...]]:
    """*ref*'s trusted body and every advisory it tracks, in body order."""
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", _repo_of(ref), "--json", "labels"
    )
    names = {str(label.get("name", "")) for label in raw.get("labels") or []}
    body = _trusted_body(ref)
    return body, _security_ghsas(ref, names, body)


def _item_advisories(ref: IssueRef) -> tuple[str, ...]:
    """Every advisory *ref* tracks, in body order, from its labels and trusted body."""
    return _item_security(ref)[1]


@dataclass(frozen=True)
class _Tracked:
    """A security item's advisories, read before the route changes anything.

    ``others`` maps each GHSA ID, in body order, to the issues its
    ``Tracking issue:`` lines name besides the item, so the route keeps
    tracking them (a route never drops another tracked issue).
    """

    home: str
    others: dict[str, tuple[IssueRef, ...]]

    @property
    def ghsas(self) -> tuple[str, ...]:
        return tuple(self.others)

    def targets(self, ghsa: str, new: Sequence[IssueRef]) -> tuple[IssueRef, ...]:
        """The route's target set for *ghsa*: the other tracked issues, then *new*."""
        return tuple(dict.fromkeys((*self.others[ghsa], *new)))


def _read_tracked(ref: IssueRef, ghsas: Sequence[str]) -> _Tracked:
    """The advisory home of *ref*'s org and, per advisory, the issues it tracks besides *ref*.

    Raises one of :data:`_RETARGET_ERRORS` when the home or an advisory cannot be read.
    """
    home = security_records.advisory_home(ref.owner)
    others = {
        ghsa: tuple(issue for issue in security_records.tracked_issues(home, ghsa) if issue != ref)
        for ghsa in ghsas
    }
    return _Tracked(home=home, others=others)


def _tracked_before_move(ref: IssueRef, ghsas: tuple[str, ...]) -> _Tracked | None:
    """:func:`_read_tracked` before a one-off moves *ref*; None for a non-security item.

    A failed read refuses the route, since nothing has moved yet.
    """
    if not ghsas:
        return None
    try:
        return _read_tracked(ref, ghsas)
    except _RETARGET_ERRORS as exc:
        raise RouteRefusedError(
            f"{ref.slug}: the issues its advisories track could not be read ({exc}); "
            "nothing was moved"
        ) from exc


def _retarget(
    tracked: _Tracked,
    ref: IssueRef,
    ghsa: str,
    targets: tuple[IssueRef, ...],
    *,
    renamed: tuple[IssueRef, ...] = (),
) -> None:
    """Move advisory *ghsa* from the routed item *ref* to track *targets*.

    *targets* come from :meth:`_Tracked.targets`, so they keep every other
    issue the advisory tracks and the move never meets the drop check.
    """
    security_records.retarget(tracked.home, ghsa, targets, source=ref, renamed=renamed)


def _one_off_target(item: IssueRef, action: OneOff) -> IssueRef | None:
    """The epic *action* names for *item*, found without creating one; None if absent."""
    dest = action.repo
    if action.epic == epics.ADHOC.sentinel:
        owner, bare = dest.split("/", 1)
        return epics.find_standing_epic(epics.ADHOC, epics.resolve_epic_home(owner, bare), bare)
    if action.epic == epics.OPERATIONS.sentinel:
        return epics.find_standing_epic(epics.OPERATIONS, *epics.operations_home(dest))
    # No sentinel reaches here but ad-hoc and operations, so none is created; a
    # continuation's ref resolves to its chain's head, as the route itself does.
    target = epics.resolve_epic_ref(action.epic, repo=dest, sentinels=False)
    return target if target.owner == item.owner else None


def _queue_epics(item: IssueRef) -> list[IssueRef]:
    """The live queue epics a one-off of *item* can have come from, never created.

    Those of *item*'s own queue home and of its org's ``.github`` (a one-off
    transfers within the org), one of each kind that allows the route.
    """
    homes = dict.fromkeys(
        (epics.queue_home(_repo_of(item)), epics.queue_home(f"{item.owner}/.github"))
    )
    found = (
        epics.find_standing_epic(kind, home, name)
        for home, name in homes
        for kind in epics.QUEUE_KINDS
        if OneOff in ROUTES[kind.key]
    )
    return [epic for epic in dict.fromkeys(found) if epic is not None]


def _rerun_searches(task: IssueRef, old_parent: IssueRef) -> bool:
    """Whether a one-off re-run on *task* would find *old_parent*'s stale listing.

    It searches only :func:`_queue_epics` (#4291): a source repo that keeps
    its own queues is outside them. Only picks a failure's recovery text, so a
    lookup that fails picks the by-hand recovery, which always applies.
    """
    try:
        return old_parent in _queue_epics(task)
    except github.MissingGitHubTokenError:
        raise
    except github.GitHubAPIError, RuntimeError, ValueError, epics.EpicHomeError:
        return False


def _clear_stale_queue_listing(item: IssueRef, action: OneOff, parent: IssueRef) -> bool:
    """Finish a one-off whose item reads as moved but is not settled (#4291).

    Applies when *item* is already in the target repo and its parent field
    reads the target epic, but the target epic's sub-issue list does not hold
    it, or a queue epic's list (what the queue views read) still does. The
    parent field alone is not proof of the link: GitHub has shown it naming
    the target while the only real link was the intake epic's. So a target
    that does not list the item is linked again first, and a queue listing is
    removed only once the target lists the item, never leaving it orphaned.
    The item must then hold, on two reads a settle delay apart. Return False,
    changing nothing, when the item is settled or not in that state, so the
    caller's refusal stands.
    """
    if action.repo != _repo_of(item) or _one_off_target(item, action) != parent:
        return False
    queues = [queue for queue in _queue_epics(item) if queue != parent]
    listed = item in epics.native_children(parent)
    stale = [queue for queue in queues if item in epics.native_children(queue)]
    if listed and not stale:
        return False
    fix = (
        f"Re-check with vrg-queue in a minute; if it is still wrong, link {item.slug} under "
        f"{parent.slug} in the GitHub UI, and only then remove it from the sub-issues of "
        "any queue epic that still lists it"
    )
    try:
        if not listed:
            print(
                f"intake-route: {item.slug}'s parent reads {parent.slug}, but {parent.slug} "
                "does not list it as a sub-issue; linking it again",
                file=sys.stderr,
            )
            _link(parent, item, force=True)
            if item not in epics.native_children(parent):
                raise RouteHalfAppliedError(
                    f"linked {item.slug} under {parent.slug} again, but {parent.slug} still "
                    f"does not list it; no queue listing was removed. {fix}"
                )
            stale = [queue for queue in queues if item in epics.native_children(queue)]
        for queue in stale:
            print(
                f"intake-route: {item.slug} is under {parent.slug}, but {queue.slug} still "
                "lists it as a sub-issue; removing the stale listing",
                file=sys.stderr,
            )
            epic_chain.clear_stale_listing(queue, item, keep=parent)
        placement = _settled_placement(item, parent, queues)
    except RouteHalfAppliedError, github.MissingGitHubTokenError, epics.ClosedEpicError:
        raise
    except (github.GitHubAPIError, RuntimeError, ValueError) as exc:
        raise RouteHalfAppliedError(
            f"{item.slug} reads {parent.slug} as its parent but was not settled there, and "
            f"settling it or reading it back failed ({error_text(exc)}). {fix}"
        ) from exc
    if placement:
        raise RouteHalfAppliedError(
            f"settled {item.slug} under {parent.slug}, but it is {placement} on reading it "
            f"back. {fix}"
        )
    return True


def _settled_placement(item: IssueRef, parent: IssueRef, queues: list[IssueRef]) -> str:
    """Read *item* back twice, a settle delay apart; empty when it holds under *parent*.

    Otherwise the placement text of the first read that does not: another
    parent, *parent* not listing it, or a queue epic in *queues* that does.
    """
    for _ in range(2):
        time.sleep(_SETTLE_SECONDS)
        _, placement = _observe(parent, item, None)
        if placement:
            return placement
        listing = next((queue for queue in queues if item in epics.native_children(queue)), None)
        if listing is not None:
            return f"still listed as a sub-issue of {listing.slug}"
    return ""


def _settle_parent(
    item: IssueRef, epic: IssueRef, task: IssueRef, old_parent: IssueRef | None
) -> None:
    """Make *epic* the transferred *task*'s parent, robust to a late carry-over.

    The transfer keeps the intake parent, but it may surface only after a first
    read, so every round replaces (never plainly adds) the parent, waits, and
    verifies; a parent that reverts is replaced again. One matching read does
    not settle it: the carry-over can land just after it (#4004), so the round
    waits again and re-reads, and the old parent's sub-issue list must no
    longer hold *task* on either read. Nor is the parent field proof on its
    own: *epic*'s sub-issue list must hold *task* too, and a round that finds
    it missing links it again even though the parent field reads *epic*
    (#4291). Raise if it never holds.
    """
    placement = ""
    duplicate = False
    for attempt in range(1, _SETTLE_ROUNDS + 1):
        relink = placement.startswith(_NOT_LISTED)
        duplicate = _link(epic, task, replace=True, item=item, force=relink)
        time.sleep(_SETTLE_SECONDS)
        current, placement = _observe(epic, task, old_parent)
        if current == epic and not placement:
            time.sleep(_SETTLE_SECONDS)
            current, placement = _observe(epic, task, old_parent)
            if current == epic and not placement:
                return
        if placement.startswith(_NOT_LISTED):
            next_step = "linking it again"
        elif current == epic:
            next_step = "checking again"
        else:
            next_step = "replacing the parent"
        print(
            f"intake-route: {task.slug} is {placement} after linking it "
            f"under {epic.slug} (round {attempt}/{_SETTLE_ROUNDS}); {next_step}",
            file=sys.stderr,
        )
    raise _parent_mismatch(item, epic, task, placement, duplicate=duplicate, old_parent=old_parent)


# The placement of a task whose parent field names the epic its sub-issue list lacks.
_NOT_LISTED = "not listed as a sub-issue of"


def _observe(
    epic: IssueRef, task: IssueRef, old_parent: IssueRef | None
) -> tuple[IssueRef | None, str]:
    """Read *task*'s parent, *epic*'s sub-issue list and, if set, *old_parent*'s.

    Returns the parent (the head of the chain its parent field names) and the
    placement text, empty when *task* is settled under *epic*: its parent
    field names a container of *epic*'s chain, that container lists it, and
    *old_parent*'s chain no longer lists it.
    """
    field, listings = epic_chain.observe_parent(epic, task)
    current = field if field is None or field == epic else epic_chain.head_of(field)
    if current != epic:
        return current, _placement(current)
    if field not in listings:
        return current, f"{_NOT_LISTED} {epic.slug} (its parent reads {epic.slug})"
    if old_parent is not None and old_parent != epic and task in epics.native_children(old_parent):
        return current, (
            f"still listed as a sub-issue of {old_parent.slug} (its parent reads {epic.slug})"
        )
    return current, ""


def _link_and_check(
    item: IssueRef, epic: IssueRef, task: IssueRef, old_parent: IssueRef | None
) -> None:
    """Link *task* under *epic* and read its parent back.

    One read is enough for a plain link; one that had to clear a duplicate
    sub-issue link (#3839) gets the settle loop's two reads and old-parent check.
    """
    with _half_applied_on_error(item, epic, task):
        if _link(epic, task, item=item):
            _settle_parent(item, epic, task, old_parent)
        else:
            _check_parent(item, epic, task)


@contextlib.contextmanager
def _half_applied_on_error(item: IssueRef, epic: IssueRef, task: IssueRef) -> Generator[None]:
    """Turn a failed link or read-back of a one-off into :class:`RouteHalfAppliedError`.

    By then the item may already be moved, so a raw API error would leave it
    half-routed with nothing saying so (#3839). A missing token is a
    configuration error, not a routing state, and is left as it came.
    """
    try:
        yield
    except RouteHalfAppliedError, github.MissingGitHubTokenError:
        raise
    except (github.GitHubAPIError, RuntimeError) as exc:
        lead = f"moved {item.slug} to {task.slug}, but" if task != item else f"{item.slug}:"
        raise RouteHalfAppliedError(
            f"{lead} linking {task.slug} under {epic.slug} or reading its parent back failed "
            f"({error_text(exc)}); the route is half-applied. Check where it is with "
            f"vrg-queue, then fix it with: "
            f"vrg-epic-move --task {quoted_slug(task)} --epic {quoted_slug(epic)}"
        ) from exc


def _check_parent(item: IssueRef, epic: IssueRef, task: IssueRef) -> None:
    """Read *task* back once; raise unless its parent is *epic* and *epic* lists it."""
    _, placement = _observe(epic, task, None)
    if placement:
        raise _parent_mismatch(item, epic, task, placement)


def _placement(parent: IssueRef | None) -> str:
    return f"under {parent.slug}" if parent is not None else "unparented"


def _moved(item: IssueRef, task: IssueRef) -> str:
    """What the route did before it failed: ``moved A to B`` or ``linked A``."""
    return f"moved {item.slug} to {task.slug}" if task != item else f"linked {item.slug}"


def _parent_mismatch(
    item: IssueRef,
    epic: IssueRef,
    task: IssueRef,
    placement: str,
    *,
    duplicate: bool = False,
    old_parent: IssueRef | None = None,
) -> RouteHalfAppliedError:
    """The loud failure for a move whose final parent is not *epic*.

    A still-listed or not-listed item's parent field already names *epic*, so
    ``vrg-epic-move`` would report it done and change nothing; its recovery is
    a re-run of the one-off on *task*, which settles it (#4291). A still-listed
    item is only reported once *epic* lists it, so removing the old listing by
    hand cannot orphan it: that is the recovery when the re-run would not find
    *old_parent* among the queue epics it searches. Nor can ``vrg-epic-move``
    fix an item whose last link GitHub refused as a duplicate sub-issue
    (*duplicate*, #3839): it would be refused the same way, so the recovery is
    the GitHub UI.
    """
    moved = _moved(item, task)
    rerun = (
        f"vrg-intake-route {quoted_slug(task)} one-off --repo {_repo_of(task)} "
        f"--epic {quoted_slug(epic)}"
    )
    if placement.startswith(_NOT_LISTED):
        return RouteHalfAppliedError(
            f"{moved}, but it is {placement}, not settled under {epic.slug}; the route is "
            f"half-applied. Fix it with: {rerun}"
        )
    if duplicate and not placement.startswith("still listed"):
        return RouteHalfAppliedError(
            f"{moved}, but it is {placement}, not under {epic.slug}, although {epic.slug} "
            f"lists it as a sub-issue (GitHub refuses the link: {_DUPLICATE_SUB_ISSUE}); "
            f"the route is half-applied. "
            f"Set the parent of {task.slug} to {epic.slug} in the GitHub UI"
        )
    if placement.startswith("still listed"):
        if old_parent is not None and _rerun_searches(task, old_parent):
            clear = f"clear it with: {rerun}"
        else:
            listing = old_parent.slug if old_parent is not None else "the old epic"
            clear = f"remove {task.slug} from the sub-issues of {listing} in the GitHub UI"
        return RouteHalfAppliedError(
            f"{moved}, but it is {placement}, not settled under {epic.slug}; the route is "
            f"half-applied. Re-check with vrg-queue in a minute; if it is still listed "
            f"there, {clear}"
        )
    return RouteHalfAppliedError(
        f"{moved}, but it is {placement}, not under {epic.slug}; the route is half-applied. "
        f"Fix it with: vrg-epic-move --task {quoted_slug(task)} --epic {quoted_slug(epic)}"
    )


def _issue_ref_from_url(output: str, *, failure: str) -> IssueRef:
    """The issue a ``gh`` command's *output* names by URL; raise with *failure* if none."""
    match = _ISSUE_URL_RE.search(output)
    if match is None:
        raise RuntimeError(f"{failure}: could not read the new issue URL from {output!r}")
    return IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))


def _transferred_ref(ref: IssueRef, dest: str, epic: IssueRef, output: str) -> IssueRef:
    return _issue_ref_from_url(
        output,
        failure=f"transferred {ref.slug} to {dest} (link it under {epic.slug} by hand)",
    )


def _retarget_to_copy(ref: IssueRef, new: IssueRef, tracked: _Tracked) -> None:
    """Retarget every advisory of *tracked* from *ref* to its copy *new*, in body order.

    A failure leaves the original open and refuses the route, naming every
    advisory not yet retargeted with the command that finishes each.
    """
    ghsas = tracked.ghsas
    targets = {ghsa: tracked.targets(ghsa, (new,)) for ghsa in ghsas}
    for index, ghsa in enumerate(ghsas):
        try:
            _retarget(tracked, ref, ghsa, targets[ghsa])
        except _RETARGET_ERRORS as err:
            pending = ghsas[index:]
            raise RouteRefusedError(
                f"recreated {ref.slug} as {new.slug}, but {_not_retargeted(pending)} not "
                f"retargeted ({err}); {ref.slug} is left open and {new.slug} unlinked. "
                f"Finish by hand: {_retarget_commands(pending, ref, targets)}, then close "
                f"{ref.slug} with a pointer and link {new.slug} under its epic"
            ) from err


def _recreate(
    ref: IssueRef,
    dest: str,
    exc: subprocess.CalledProcessError,
    tracked: _Tracked | None,
    body: str,
) -> IssueRef:
    """Copy *ref* into *dest* with a link back, then close the original with a pointer.

    The copy's body is *body*, the trusted body read before the move
    (:func:`_trusted_body`), never one read again from the original.
    Every advisory a security item names (*tracked*, in body order) is
    retargeted from *ref* to the copy, keeping every other issue it tracks,
    before the original closes; if any retarget fails the original stays open
    and the route stops, naming every advisory not yet retargeted.
    """
    source = _repo_of(ref)
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", source, "--json", "title,labels,url"
    )
    names = [str(label.get("name", "")) for label in raw.get("labels") or []]
    registry = {entry["name"] for entry in load_labels()["labels"]}
    # A routed one-off is never an epic-create session underway, nor an epic
    # candidate: in-progress and every epic-size:* label stay behind.
    dropped = {*RETIRED_LABELS, epics.PLANNING_IN_PROGRESS_LABEL}
    kept = [name for name in names if name not in dropped and not name.startswith(LABEL_PREFIX)]
    carried = [name for name in kept if name in registry]
    uncarried = [name for name in kept if name not in registry]
    if carried:
        ensure_labels(dest, carried)
    original_url = str(raw["url"])
    moved_from = MOVED_FROM_LINE.format(url=original_url)
    body = body.rstrip()
    new_url = github.create_issue(
        repo=dest,
        title=str(raw["title"]),
        body=f"{body}\n\n{moved_from}\n" if body else f"{moved_from}\n",
        labels=carried,
    )
    new = _issue_ref_from_url(
        new_url, failure=f"recreated {ref.slug} in {dest} (original left open)"
    )
    if tracked is not None:
        _retarget_to_copy(ref, new, tracked)
    _close(ref, MOVED_TO_COMMENT.format(url=new_url), reason="not planned")
    detail = " ".join(error_text(exc).split())
    print(
        f"intake-route: {ref.slug} -> {dest}: transfer failed ({detail}); "
        f"recreated as {new_url}, original closed with a pointer",
        file=sys.stderr,
    )
    if uncarried:
        print(
            f"intake-route: label(s) not carried to {new.slug} (not in the label registry): "
            f"{', '.join(uncarried)}",
            file=sys.stderr,
        )
    return new


def _link(
    epic: IssueRef,
    task: IssueRef,
    *,
    replace: bool = False,
    item: IssueRef | None = None,
    force: bool = False,
) -> bool:
    """Link *task* under *epic*, replacing any current parent in one atomic call.

    With *replace*, an apparently unparented *task* is still linked with
    ``replaceParent``, so a parent that surfaces between the read and the link
    is replaced rather than refusing the link ("Sub issue may only have one
    parent"). With *force*, a *task* whose parent field already reads *epic*
    is linked again with ``replaceParent`` into the container of *epic*'s
    chain its parent field names (:func:`epic_chain.relink`): that
    container's sub-issue list lacks it, so the parent field is not to be
    trusted (#4291). Every other link goes into *epic*'s chain by first fit
    (:func:`epic_chain.link_into`, or :func:`epic_chain.move_into` to replace
    a parent), rolling over when the chain is full. Closed is closed: a
    closed epic is refused, never reopened (.github#1609).

    With *item* (the routed item, for its one-off's messages), a link GitHub
    refuses as a duplicate sub-issue is recovered by :func:`_clear_duplicate`
    and True is returned; the caller must then read the parent back with the
    settle loop. Without *item* the refusal is raised as it came.
    """
    current = epics.native_parent_of(task)
    if current == epic and not force:
        return False
    try:
        if current == epic:
            epic_chain.relink(epic, task)
        elif current is None and not replace:
            epic_chain.link_into(epic, task)
        else:
            epic_chain.move_into(epic, task)
    except (github.GitHubAPIError, RuntimeError) as exc:
        if item is None or not _is_duplicate_refusal(exc):
            raise
        _clear_duplicate(item, epic, task, current)
        return True
    return False


# GitHub's addSubIssue refusal when the parent already lists the child (#3839).
_DUPLICATE_SUB_ISSUE = "Issue may not contain duplicate sub-issues"


def _is_duplicate_refusal(exc: BaseException) -> bool:
    return _DUPLICATE_SUB_ISSUE in error_text(exc)


def _clear_duplicate(
    item: IssueRef, epic: IssueRef, task: IssueRef, current: IssueRef | None
) -> None:
    """Recover a link refused because *epic* already lists *task* (#3839).

    After a transfer GitHub can list *task* among *epic*'s sub-issues while its
    parent field still reads the old parent (*current*), and then refuses every
    ``addSubIssue`` under *epic*, ``replaceParent`` or not. The stale link is
    the old parent's, so it is removed and *epic*'s listing is left to stand;
    with no parent at all, *epic*'s own listing is removed and the link made
    again. Nothing is removed unless *epic*'s list really holds *task*. The
    caller reads the parent back afterwards: this only clears the way.

    *current* and *epic* are chain heads: the stale link is cleared from
    whichever container of *current*'s chain lists *task*, keeping the
    container of *epic*'s chain that lists it
    (:func:`epic_chain.clear_stale_listing`), and *epic*'s own listing, when
    there is no parent, from whichever container of its chain holds it.
    """
    moved = _moved(item, task)
    fix = f"Set the parent of {task.slug} to {epic.slug} in the GitHub UI"
    try:
        _, listings = epic_chain.observe_parent(epic, task)
    except (github.GitHubAPIError, RuntimeError) as exc:
        raise RouteHalfAppliedError(
            f"{moved}, but GitHub refused to link it under {epic.slug} "
            f"({_DUPLICATE_SUB_ISSUE}) and reading the sub-issues of {epic.slug} failed "
            f"({error_text(exc)}); it is {_placement(current)}; the route is half-applied. "
            f"{fix}"
        ) from exc
    if not listings:
        raise RouteHalfAppliedError(
            f"{moved}, but GitHub refused to link it under {epic.slug} "
            f"({_DUPLICATE_SUB_ISSUE}) although {epic.slug} does not list it; it is "
            f"{_placement(current)}; the route is half-applied. {fix}"
        )
    stale = current if current is not None else epic
    action = f"removing the stale link from {stale.slug}"
    if current is None:
        action += " and linking it again"
    print(
        f"intake-route: {epic.slug} already lists {task.slug} as a sub-issue, but its "
        f"parent reads {current.slug if current is not None else 'nothing'}; {action}",
        file=sys.stderr,
    )
    try:
        if current is None:
            epic_chain.unlink_from(epic, task)
            epic_chain.move_into(epic, task)
        else:
            epic_chain.clear_stale_listing(current, task, keep=listings[0])
    except (github.GitHubAPIError, RuntimeError, ValueError) as exc:
        raise RouteHalfAppliedError(
            f"{moved}, but it is {_placement(current)} while {epic.slug} already lists it "
            f"as a sub-issue, and {action} failed ({error_text(exc)}); the route is "
            f"half-applied. {fix}"
        ) from exc


def _comment(ref: IssueRef, body: str) -> None:
    github.run("issue", "comment", str(ref.number), "--repo", _repo_of(ref), "--body", body)


def _close(ref: IssueRef, comment: str, *, reason: str) -> None:
    github.run(
        "issue",
        "close",
        str(ref.number),
        "--repo",
        _repo_of(ref),
        "--reason",
        reason,
        "--comment",
        comment,
    )
