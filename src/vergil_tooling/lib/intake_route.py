"""Deterministic routing between the container epics (epics vergil-project/.github#176, #508).

Every queue review decides, per item, one route; this module owns the mechanics
so no skill hand-rolls them. An item is routed out of the **container epic** it
sits in (intake, planning, ad hoc or deferred), and :data:`ROUTES` says which
routes each container allows:

- **drop** — comment the reason, naming the queue it was dropped from, and
  close (not planned).
- **defer** — move the item into its queue home's deferred epic and record why
  it waits and when to look again, plus the container it came ``from``; on an
  item already deferred it is a **refresh**: a new record, no move, no
  ``Deferred from:`` line. The link lands before the record, and a record that
  fails after the move raises with the full intended comment.
- **intake** — reparent a deferred item back onto its home's intake epic.
- **one-off** — move the item into its target repo and link it under that
  repo's ad-hoc epic (or a named finite epic). A move *transfers* the issue,
  keeping its history; when GitHub refuses the transfer it falls back to
  recreating the item in the target repo with a link back and closing the
  original with a pointer — and says so on stderr. A transfer carries the
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
  read-back that fails outright.
- **planning** — reparent onto the planning epic of the same queue home.
- **split** — the replacement tasks already exist (made with
  ``vrg-issue-create``); comment the links and the reason, then close.

A **security** item (the ``security`` label and at least one ``Security
advisory:`` line, epics vergil-project/.github#517 and #573) keeps its advisory
links: a split or a recreate retargets the advisory (a recreate: every
advisory, in body order) to the new issue(s) *before* the original closes, and
stops with the original open if that fails; a transfer retargets every
advisory after the move and, if one fails, reports the moved-but-not-retargeted
state with the exact recovery command for each advisory still pending. A split
of an item naming several advisories is refused before any change, since which
advisory goes to which task cannot be inferred. The planning route leaves the
advisories alone.

A task in a development epic, an epic, and an unparented issue are refused
(``planning`` alone accepts an unparented issue: the ``epic-create`` free-text
path), the refusal naming the parent. A planning item whose ``epic-create``
session already recorded an ``Epic:`` marker refuses drop, and refuses defer
while any of its development epics is open (one an ``Epic:`` marker names, or
one whose body carries a ``Planning:`` line for it; #4001). An
ad-hoc item refuses defer while an open PR would close it. A private repo's item
is never routed to a public repo or epic. The whole route runs under the item
owner's App token (:func:`github.target_org`).
"""

from __future__ import annotations

import contextlib
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, cast

from vergil_tooling.lib import epics, github, security_records
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.labels import RETIRED_LABELS, ensure_labels, load_labels
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.queue_view import (
    PLANNING_MARKER,
    parse_epic_markers,
    parse_planning_markers,
)
from vergil_tooling.lib.shell_ref import quoted_slug

if TYPE_CHECKING:
    from collections.abc import Generator

DROP_COMMENT = "Dropped in {queue} review: {reason}"
DEFER_COMMENT = "Deferred {day}: {reason}\nRevisit when: {revisit_when}"
DEFERRED_FROM_LINE = "Deferred from: {epic}"
SPLIT_COMMENT = "Split into: {refs}. {reason}"
MOVED_TO_COMMENT = "Moved to {url}"
MOVED_FROM_LINE = "Moved from {url}"

# A transfer carries the old parent over eventually, not atomically: a link
# made right after it can be overwritten seconds later (#3556). Each settle
# round replaces the parent, waits, then re-reads it.
_SETTLE_ROUNDS = 4
_SETTLE_SECONDS = 3.0

_ISSUE_URL_RE = re.compile(r"https?://[^/\s]+/([^/\s]+)/([^/\s]+)/issues/([0-9]+)")


class RouteRefusedError(ValueError):
    """A route that must not run: wrong container, cross-org, or private → public."""


class RouteHalfAppliedError(RuntimeError):
    """A one-off that moved or linked the item but could not settle it under its epic."""


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
    pass


@dataclass(frozen=True)
class ToIntake:
    pass


@dataclass(frozen=True)
class Split:
    into: tuple[IssueRef, ...]
    reason: str


RouteAction = Drop | Defer | OneOff | ToPlanning | ToIntake | Split

# The routes each container allows, keyed by :func:`epics.container_kind` of the
# item's parent; ``None`` is an unparented item. A parent of any other kind (a
# development epic, an archive) allows nothing.
ROUTES: dict[str | None, frozenset[type]] = {
    "intake": frozenset({Drop, Defer, OneOff, ToPlanning, Split}),
    "planning": frozenset({Defer, Drop}),
    "ad-hoc": frozenset({Defer}),
    "deferred": frozenset({Defer, ToIntake, ToPlanning, OneOff, Drop, Split}),
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
    parent = epics.parent_of(ref)
    kind = _check_eligible(ref, action, parent)
    if isinstance(action, Drop):
        if kind == epics.PLANNING.key:
            _guard_planning_epic(ref, _view(ref, "labels,comments"), "dropping")
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
                "by hand with vrg-security-record retarget, then split"
            )
        if ghsas:
            [ghsa] = ghsas
            try:
                _retarget(ref, ghsa, action.into)
            except _RETARGET_ERRORS as exc:
                raise RouteRefusedError(
                    f"{ref.slug}: advisory {ghsa} was not retargeted to {refs} ({exc}); "
                    f"{ref.slug} is left open"
                ) from exc
        _close(ref, SPLIT_COMMENT.format(refs=refs, reason=action.reason), reason="completed")
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, ToPlanning):
        planning = epics.ensure_planning_epic(_repo_of(ref))
        _guard_visibility(ref, _repo_of(planning))
        _link(planning, ref)
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, ToIntake):
        intake = epics.ensure_intake_epic(_repo_of(ref))
        _guard_visibility(ref, _repo_of(intake))
        _link(intake, ref)
        return RouteResult(final=ref, moved_by="none")
    return _one_off(ref, action, parent)


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


def _view(ref: IssueRef, fields: str) -> dict[str, Any]:
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", _repo_of(ref), "--json", fields
    )
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected 'gh issue view' response for {ref.slug}: {raw!r}")
    return raw


def _label_names(view: dict[str, Any]) -> set[str]:
    return {str(label.get("name", "")) for label in view.get("labels") or []}


def _guard_planning_epic(ref: IssueRef, view: dict[str, Any], doing: str) -> None:
    """Refuse a planning item whose ``epic-create`` session already made an epic."""
    comments = [str(c.get("body") or "") for c in view.get("comments") or []]
    epic = parse_planning_markers(comments).epic
    if epic is not None:
        raise RouteRefusedError(
            f"{ref.slug} has a development epic ({epic.slug}); {doing} it would orphan that epic"
        )


def _planning_epic_states(ref: IssueRef, view: dict[str, Any]) -> dict[IssueRef, str]:
    """Each development epic of the planning item *ref* and its state, OPEN or CLOSED.

    The epics are those an ``Epic:`` marker in *view*'s comments names, plus
    every ``epic`` issue in *ref*'s org whose body carries a ``Planning: <ref>``
    line (``epic-create`` writes that line into each epic it creates, while the
    marker names only one). The search is full text, so each hit's body is
    checked for the exact line. A search that reaches its limit, a state that
    is neither OPEN nor CLOSED, or a failed read raises: nothing is assumed
    closed.
    """
    comments = [str(c.get("body") or "") for c in view.get("comments") or []]
    states: dict[IssueRef, str] = {}
    for epic in parse_epic_markers(comments):
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
            raise RuntimeError(f"search hit for {ref.slug} has no repository: {item!r}")
        owner, repo = name_with_owner.split("/")
        states.setdefault(IssueRef(owner, repo, int(item["number"])), str(item["state"]).upper())
    for epic, state in states.items():
        if state not in ("OPEN", "CLOSED"):
            raise RuntimeError(f"unexpected state {state!r} for {epic.slug}")
    return states


def _guard_open_planning_epics(ref: IssueRef, view: dict[str, Any]) -> None:
    """Refuse deferring a planning item while any of its development epics is open."""
    open_epics = [
        epic.slug for epic, state in _planning_epic_states(ref, view).items() if state == "OPEN"
    ]
    if open_epics:
        raise RouteRefusedError(
            f"{ref.slug} has an open development epic ({', '.join(open_epics)}); deferring "
            "it would orphan that epic. Close every development epic first"
        )


def _guard_open_closing_pr(ref: IssueRef) -> None:
    """Refuse deferring an ad-hoc item that an open PR would close (it would strand the PR)."""
    view = _view(ref, "closedByPullRequestsReferences")
    for pr in view.get("closedByPullRequestsReferences") or []:
        url = str(pr.get("url") or "")
        if not url:
            raise RuntimeError(f"closing PR of {ref.slug} has no URL: {pr!r}")
        state = github.read_output("pr", "view", url, "--json", "state", "--jq", ".state")
        if state.strip().upper() == "OPEN":
            raise RouteRefusedError(
                f"{ref.slug} has an open PR that would close it ({url}); deferring it would "
                "strand that work — finish or close the PR first"
            )


def _defer(ref: IssueRef, action: Defer, kind: str | None, parent: IssueRef, day: date) -> None:
    """Move *ref* into its home's deferred epic and record why; a refresh only records."""
    record = DEFER_COMMENT.format(
        day=day.isoformat(), reason=action.reason, revisit_when=action.revisit_when
    )
    if kind == epics.DEFERRED.key:
        _comment(ref, record)
        return
    view = _view(ref, "labels,comments")
    if kind == epics.PLANNING.key:
        _guard_open_planning_epics(ref, view)
    if kind == epics.ADHOC.key:
        _guard_open_closing_pr(ref)
    deferred = epics.ensure_deferred_epic(_repo_of(ref))
    _guard_visibility(ref, _repo_of(deferred))
    body = f"{record}\n{DEFERRED_FROM_LINE.format(epic=parent.slug)}"
    _link(deferred, ref)
    stranded = f"{ref.slug} is under the deferred epic {deferred.slug} without a deferral record"
    by_hand = (
        f"Post this comment on {ref.slug} by hand (re-running defer would be a refresh "
        f"and lose the 'Deferred from:' line):\n\n{body}"
    )
    if epics.PLANNING_IN_PROGRESS_LABEL in _label_names(view):
        label = epics.PLANNING_IN_PROGRESS_LABEL
        try:
            github.run(
                "issue", "edit", str(ref.number), "--repo", _repo_of(ref), "--remove-label", label
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"{stranded}: removing the '{label}' label failed "
                f"({' '.join(error_text(exc).split())}). Fix it with: gh issue edit "
                f"{ref.number} --repo {_repo_of(ref)} --remove-label {label}. {by_hand}"
            ) from exc
    try:
        _comment(ref, body)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"{stranded}: posting it failed ({' '.join(error_text(exc).split())}). {by_hand}"
        ) from exc


def _guard_visibility(ref: IssueRef, *targets: str) -> None:
    """Refuse routing a private repo's item into any public *targets* repo."""
    source = _repo_of(ref)
    others = [target for target in dict.fromkeys(targets) if target != source]
    if not others or github.is_public(source):
        return
    public = [target for target in others if github.is_public(target)]
    if public:
        raise RouteRefusedError(
            f"{ref.slug} is in private repo {source}; refusing to route it to public "
            f"{', '.join(public)}"
        )


def _one_off(ref: IssueRef, action: OneOff, old_parent: IssueRef | None) -> RouteResult:
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
    # Check the target repo before resolving the epic: resolving 'adhoc' may
    # create the target's ad-hoc epic, which must not happen for a refused route.
    _guard_visibility(ref, dest)
    epic = epics.resolve_epic_ref(action.epic, repo=dest)
    _guard_visibility(ref, _repo_of(epic))
    if epics.queue_kind(epic) is not None:
        raise RouteRefusedError(
            f"{epic.slug} is a queue epic; a one-off goes under an ad-hoc or finite epic "
            "(use the planning route for planning)"
        )
    if dest == source:
        _link_and_check(ref, epic, ref, old_parent)
        return RouteResult(final=ref, moved_by="none")
    # Read before the move: a transfer drops labels the target repo lacks.
    ghsas = _item_advisories(ref)
    try:
        output = github.read_output("issue", "transfer", str(ref.number), dest, "--repo", source)
    except github.MissingGitHubTokenError:
        raise
    except subprocess.CalledProcessError as exc:
        new = _recreate(ref, dest, exc, ghsas)
        # A recreated issue has no parent to carry over: link it, then check.
        _link_and_check(ref, epic, new, None)
        return RouteResult(final=new, moved_by="recreate")
    final = _transferred_ref(ref, dest, epic, output)
    if not ghsas:
        with _half_applied_on_error(ref, epic, final):
            _settle_parent(ref, epic, final, old_parent)
        return RouteResult(final=final, moved_by="transfer")
    try:
        with _half_applied_on_error(ref, epic, final):
            _settle_parent(ref, epic, final, old_parent)
    except RouteHalfAppliedError as exc:
        raise RouteHalfAppliedError(f"{exc}; also, {_transfer_recovery(ghsas, final)}") from exc
    for index, ghsa in enumerate(ghsas):
        try:
            # The old number is the same issue: rewrite its tracking line, never unmark it.
            _retarget(ref, ghsa, (final,), renamed=(ref,))
        except _RETARGET_ERRORS as exc:
            # The move already happened, so this is not a refusal: report what is left.
            pending = _transfer_recovery(ghsas[index:], final)
            raise RuntimeError(f"{ref.slug} moved to {final.slug}, but {pending}") from exc
    return RouteResult(final=final, moved_by="transfer")


def _not_retargeted(ghsas: tuple[str, ...]) -> str:
    """``advisory A was`` / ``advisories A, B were``: the advisories left behind."""
    if len(ghsas) == 1:
        return f"advisory {ghsas[0]} was"
    return f"advisories {', '.join(ghsas)} were"


def _retarget_commands(ghsas: tuple[str, ...], target: IssueRef) -> str:
    """One ``vrg-security-record retarget`` recovery command per advisory, in order."""
    return ", then ".join(
        f"vrg-security-record retarget {ghsa} --to {quoted_slug(target)}" for ghsa in ghsas
    )


def _transfer_recovery(ghsas: tuple[str, ...], final: IssueRef) -> str:
    """What is left after a transfer: every advisory *ghsas* names, with its command."""
    return f"{_not_retargeted(ghsas)} not retargeted; run: {_retarget_commands(ghsas, final)}"


# The failures that leave an advisory not retargeted: the retarget itself, or
# resolving the org's advisory home (a private or unreadable ``.github``).
_RETARGET_ERRORS = (
    security_records.RetargetError,
    security_records.PrivateHomeError,
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


def _item_advisories(ref: IssueRef) -> tuple[str, ...]:
    """Read *ref*'s labels and body; return every advisory it tracks, in body order."""
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", _repo_of(ref), "--json", "labels,body"
    )
    names = {str(label.get("name", "")) for label in raw.get("labels") or []}
    return _security_ghsas(ref, names, str(raw.get("body") or ""))


def _retarget(
    ref: IssueRef,
    ghsa: str,
    targets: tuple[IssueRef, ...],
    *,
    renamed: tuple[IssueRef, ...] = (),
) -> None:
    """Move advisory *ghsa* (homed in *ref*'s org ``.github``) to track *targets*."""
    home = security_records.advisory_home(ref.owner)
    security_records.retarget(home, ghsa, targets, renamed=renamed)


def _settle_parent(
    item: IssueRef, epic: IssueRef, task: IssueRef, old_parent: IssueRef | None
) -> None:
    """Make *epic* the transferred *task*'s parent, robust to a late carry-over.

    The transfer keeps the intake parent, but it may surface only after a first
    read, so every round replaces (never plainly adds) the parent, waits, and
    verifies; a parent that reverts is replaced again. One matching read does
    not settle it: the carry-over can land just after it (#4004), so the round
    waits again and re-reads, and the old parent's sub-issue list must no
    longer hold *task* on either read. Raise if it never holds.
    """
    placement = ""
    duplicate = False
    for attempt in range(1, _SETTLE_ROUNDS + 1):
        duplicate = _link(epic, task, replace=True, item=item)
        time.sleep(_SETTLE_SECONDS)
        current, placement = _observe(epic, task, old_parent)
        if current == epic and not placement:
            time.sleep(_SETTLE_SECONDS)
            current, placement = _observe(epic, task, old_parent)
            if current == epic and not placement:
                return
        next_step = "checking again" if current == epic else "replacing the parent"
        print(
            f"intake-route: {task.slug} is {placement} after linking it "
            f"under {epic.slug} (round {attempt}/{_SETTLE_ROUNDS}); {next_step}",
            file=sys.stderr,
        )
    raise _parent_mismatch(item, epic, task, placement, duplicate=duplicate)


def _observe(
    epic: IssueRef, task: IssueRef, old_parent: IssueRef | None
) -> tuple[IssueRef | None, str]:
    """Read *task*'s parent and, if *old_parent* is set, its sub-issue list.

    Returns the parent and the placement text, empty when *task* is settled
    under *epic*: its parent field names *epic* and *old_parent* no longer
    lists it.
    """
    current = epics.native_parent_of(task)
    if current != epic:
        return current, _placement(current)
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
    except (RouteHalfAppliedError, github.MissingGitHubTokenError):
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
    """Read *task*'s parent back once; raise unless it is *epic*."""
    current = epics.native_parent_of(task)
    if current != epic:
        raise _parent_mismatch(item, epic, task, _placement(current))


def _placement(parent: IssueRef | None) -> str:
    return f"under {parent.slug}" if parent is not None else "unparented"


def _moved(item: IssueRef, task: IssueRef) -> str:
    """What the route did before it failed: ``moved A to B`` or ``linked A``."""
    return f"moved {item.slug} to {task.slug}" if task != item else f"linked {item.slug}"


def _parent_mismatch(
    item: IssueRef, epic: IssueRef, task: IssueRef, placement: str, *, duplicate: bool = False
) -> RouteHalfAppliedError:
    """The loud failure for a move whose final parent is not *epic*.

    A still-listed item's parent field already names *epic*, so
    ``vrg-epic-move`` would report it done and change nothing; its recovery is
    to re-check the queue and, if the stale listing stays, remove it by hand.
    Nor can ``vrg-epic-move`` fix an item whose last link GitHub refused as a
    duplicate sub-issue (*duplicate*, #3839): it would be refused the same way,
    so the recovery is the GitHub UI.
    """
    moved = _moved(item, task)
    if duplicate and not placement.startswith("still listed"):
        return RouteHalfAppliedError(
            f"{moved}, but it is {placement}, not under {epic.slug}, although {epic.slug} "
            f"lists it as a sub-issue (GitHub refuses the link: {_DUPLICATE_SUB_ISSUE}); "
            f"the route is half-applied. "
            f"Set the parent of {task.slug} to {epic.slug} in the GitHub UI"
        )
    if placement.startswith("still listed"):
        return RouteHalfAppliedError(
            f"{moved}, but it is {placement}, not settled under {epic.slug}; the route is "
            "half-applied. Re-check with vrg-queue in a minute; if it is still listed "
            f"there, remove {task.slug} from the sub-issues of the old epic in the GitHub UI"
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


def _recreate(
    ref: IssueRef, dest: str, exc: subprocess.CalledProcessError, ghsas: tuple[str, ...]
) -> IssueRef:
    """Copy *ref* into *dest* with a link back, then close the original with a pointer.

    Every advisory a security item names (*ghsas*, in body order) is retargeted
    to the copy before the original closes; if any retarget fails the original
    stays open and the route stops, naming every advisory not yet retargeted.
    """
    source = _repo_of(ref)
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", source, "--json", "title,body,labels,url"
    )
    names = [str(label.get("name", "")) for label in raw.get("labels") or []]
    registry = {entry["name"] for entry in load_labels()["labels"]}
    kept = [name for name in names if name not in RETIRED_LABELS]
    carried = [name for name in kept if name in registry]
    uncarried = [name for name in kept if name not in registry]
    if carried:
        ensure_labels(dest, carried)
    original_url = str(raw["url"])
    moved_from = MOVED_FROM_LINE.format(url=original_url)
    body = (raw.get("body") or "").rstrip()
    new_url = github.create_issue(
        repo=dest,
        title=str(raw["title"]),
        body=f"{body}\n\n{moved_from}\n" if body else f"{moved_from}\n",
        labels=carried,
    )
    new = _issue_ref_from_url(
        new_url, failure=f"recreated {ref.slug} in {dest} (original left open)"
    )
    for index, ghsa in enumerate(ghsas):
        try:
            _retarget(ref, ghsa, (new,))
        except _RETARGET_ERRORS as err:
            pending = ghsas[index:]
            raise RouteRefusedError(
                f"recreated {ref.slug} as {new.slug}, but {_not_retargeted(pending)} not "
                f"retargeted ({err}); {ref.slug} is left open and {new.slug} unlinked. "
                f"Finish by hand: {_retarget_commands(pending, new)}, then close "
                f"{ref.slug} with a pointer and link {new.slug} under its epic"
            ) from err
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
    epic: IssueRef, task: IssueRef, *, replace: bool = False, item: IssueRef | None = None
) -> bool:
    """Link *task* under *epic*, replacing any current parent in one atomic call.

    With *replace*, an apparently unparented *task* is still linked with
    ``replaceParent``, so a parent that surfaces between the read and the link
    is replaced rather than refusing the link ("Sub issue may only have one
    parent").

    With *item* (the routed item, for its one-off's messages), a link GitHub
    refuses as a duplicate sub-issue is recovered by :func:`_clear_duplicate`
    and True is returned; the caller must then read the parent back with the
    settle loop. Without *item* the refusal is raised as it came.
    """
    current = epics.native_parent_of(task)
    if current == epic:
        return False
    try:
        if current is None and not replace:
            epics.add_child(epic, task)  # reopens a closed epic itself
            return False
        state = github.read_output(
            "api", f"repos/{epic.owner}/{epic.repo}/issues/{epic.number}", "--jq", ".state"
        )
        if state.lower() == "closed":
            github.run("issue", "reopen", str(epic.number), "--repo", _repo_of(epic))
        epics.reparent_child(epic, task)
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
    """
    moved = _moved(item, task)
    fix = f"Set the parent of {task.slug} to {epic.slug} in the GitHub UI"
    try:
        listed = task in epics.native_children(epic)
    except (github.GitHubAPIError, RuntimeError) as exc:
        raise RouteHalfAppliedError(
            f"{moved}, but GitHub refused to link it under {epic.slug} "
            f"({_DUPLICATE_SUB_ISSUE}) and reading the sub-issues of {epic.slug} failed "
            f"({error_text(exc)}); it is {_placement(current)}; the route is half-applied. "
            f"{fix}"
        ) from exc
    if not listed:
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
        epics.remove_child(stale, task)
        if current is None:
            epics.reparent_child(epic, task)
    except (github.GitHubAPIError, RuntimeError) as exc:
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
