"""Read-only views of the intake, planning and deferred queues, and of the ad-hoc and hoshin tasks.

The intake queue is the open children of a home's ``Epic (intake): <name>``
standing epic, oldest first; every item is *waiting* (captured, not reviewed).
The planning queue is the open children of ``Epic (planning): <name>``, each in
one of three states computed from its ``in-progress`` label and the progress
markers ``epic-create`` writes as comments:

- **waiting** — no ``in-progress`` label;
- **in-progress** — labelled, but not every closing condition holds;
- **ready-to-close** — labelled, the ``Epic:``, ``Docs task:`` and
  ``Tasks filed: yes`` markers are all present, and the docs task is closed
  (closed means its spec + plan docs PR merged).

The deferred queue is the open children of ``Epic (deferred): <name>``; every
item is *waiting* (evaluated and parked until its revival conditions are met).
Its note is the latest deferral comment (``Deferred <day>: <reason>`` with its
``Revisit when:`` line), and it lists least recently deferred first, so a
refreshed item sinks to the bottom. An item with no deferral comment (moved by
hand, or legacy) has an empty note and sorts first.

A planning issue is never ``ready-to-close`` while an advisory holds it:
it stays ``in-progress``, with a note naming those advisories (by GHSA ID
only) and the ``vrg-security-record retarget … --from <issue>`` command
that hands each to its fix task. :func:`advisory_hold` is the one hold
rule, used by this view and by the docs-PR close
(:mod:`vergil_tooling.lib.planning_close`). It trusts the ``security``
label and the advisories' own ``Tracking issue:`` lines, never a body line
alone, and fails closed:

- any advisory naming the issue on a ``Tracking issue:`` line holds it,
  whether or not its body names that advisory;
- an advisory a ``security``-labelled issue's ``Security advisory:`` line
  names that the listing does not return cannot be read, and holds it too:
  absence is never "not tracking". The note says to re-run with a token
  that can read the repository's advisories, or have the human check it;
- a ``Security advisory:`` line on an issue without the ``security``
  label, with nothing tracking the issue, is a broken pair: the note
  reports it, and it does not hold the issue.

A line naming an org other than the issue's own is refused
(:class:`AdvisoryLineError`), never ignored.

Advisories are listed once per org per view. An org with no ``.github`` holds
no advisories, so its listing is skipped, unless one of its items names an
advisory: then the home is listed anyway, and a failed read fails the view.

The ad-hoc view is not a review queue: it is the open tasks under every live
``Epic (ad hoc): <repo>`` epic (``epic`` + ``ad-hoc``) in a home, grouped by
repo and oldest first within each, each with its kind (``task``,
``validation`` or ``deployment``), its open ``Blocked-by:`` refs and its
state: ``in-flight`` when any in-flight mark holds for it (see
:mod:`vergil_tooling.lib.in_flight`; it outranks ``blocked``), else
``blocked`` while any blocker is open, else ``ready``. An in-flight task
carries its marks and its disposition (``handed-off`` or ``resume``). The
epics come from one label search, refused when truncated; an issue carrying
those labels whose title is not a live epic's (an archive, in its current or
legacy form) is skipped. A home with no ad-hoc epic, or a ``repo`` with none,
is an empty list, not an error. The marks are read only for repos with open
ad-hoc tasks, each of which must have a main clone under the repo roots.

The hoshin view is the open tasks under the home's ``Epic (hoshin): <org>``
epic (``epic`` + ``hoshin``), the tasks ``/vergil:hoshin`` files, with the
ad-hoc view's fields, states, marks, dispositions and order: both share one
body. The epic is looked up, never created; a home with none is an empty
list. With a ``repo``, only the epic's children in that repo are listed.

The markers and notes come from **trusted comments** only (spec §5.2 of epic
vergil-project/.github#1467): each view reads every open item's comments
through :func:`vergil_tooling.lib.trusted_comments.bodies`, loading the trusted
authors once per view, so a comment the layer does not trust (an outsider's,
or one an outsider edited) never sets a planning marker, a deferral note or
the latest note. Each skipped comment is reported on stderr, metadata only,
and the view carries on; a read the layer cannot complete fails the view.

Planning and deferred items carry an **epic size**
(:mod:`vergil_tooling.lib.epic_size`): each view reads the item's labels and
its comments with their authors, and derives its size record from the
``epic-size:*`` label and the latest trusted sizing comment, reading the
trusted authors once per view (the loader's errors propagate). An item whose
size is in error keeps a held or deferral note with ``| size: <error>``
appended; otherwise ``size: <error>`` replaces its latest-comment note.
:func:`sort_planning` orders the planning queue by age, size or task count,
and :func:`filter_planning` narrows it to some sizes, always keeping the
items in error. Intake items carry no size.

Nothing here mutates GitHub: the queue epic is looked up with
:func:`epics.find_standing_epic` (never created), and a missing queue epic is
reported as :class:`QueueNotFoundError` rather than an empty queue.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from vergil_tooling.lib import (
    epic_size,
    epics,
    github,
    in_flight,
    live_branches,
    security_records,
    trusted_comments,
)
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.trusted_authors import load_trusted_authors

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vergil_tooling.lib.epic_size import SizeRecord
    from vergil_tooling.lib.in_flight import Disposition
    from vergil_tooling.lib.security_records import Advisory

# Progress-marker line prefixes. ``epic-create`` writes the first three as lines
# in planning-issue comments and the last in the new epic's body; the skill
# quotes these verbatim, so they are the single source of the format.
EPIC_MARKER = "Epic:"
DOCS_TASK_MARKER = "Docs task:"
TASKS_FILED_MARKER = "Tasks filed:"
PLANNING_MARKER = "Planning:"

_SLUG = r"([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#([0-9]+)"
_EPIC_RE = re.compile(rf"^{re.escape(EPIC_MARKER)}\s*{_SLUG}\s*$")
_DOCS_TASK_RE = re.compile(rf"^{re.escape(DOCS_TASK_MARKER)}\s*{_SLUG}\s*$")
_TASKS_FILED_RE = re.compile(rf"^{re.escape(TASKS_FILED_MARKER)}\s*(\S+)\s*$")

# A deferral comment's first line, and its ``Revisit when:`` line; the comment
# is written by ``vrg-intake-route <ref> defer``.
_DEFERRED_RE = re.compile(r"^Deferred (\d{4}-\d{2}-\d{2}): (.*)$")
_REVISIT_WHEN = "Revisit when:"

QueueState = Literal["waiting", "in-progress", "ready-to-close"]
AdhocState = Literal["ready", "blocked", "in-flight"]
PlanningSort = Literal["age", "size", "tasks"]

# The orders ``sort_planning`` accepts; ``age`` (oldest first) is the default.
PLANNING_SORTS: tuple[PlanningSort, ...] = ("age", "size", "tasks")

# The ``--size`` filter token that selects unsized items, beside the sizes.
UNSIZED_FILTER = "unsized"

# The labels that give an ad-hoc task an operational kind; any other is a "task".
_OPERATIONAL_KINDS = ("validation", "deployment")


class QueueNotFoundError(LookupError):
    """The home has no live queue epic of the requested kind."""


class AdvisoryLineError(ValueError):
    """A planning item's ``Security advisory:`` line names an org other than its own."""


@dataclass(frozen=True)
class QueueItem:
    """One open queue item, as the views render it.

    ``size`` is the item's size record: always set on planning and deferred
    items, never on intake items.
    """

    ref: IssueRef
    title: str
    created_at: str
    state: QueueState
    note: str
    epic: IssueRef | None
    size: SizeRecord | None = None


@dataclass(frozen=True)
class AdhocItem:
    """One open task under an ad-hoc epic, as ``vrg-queue adhoc`` renders it.

    ``kind`` is ``task``, ``validation`` or ``deployment``. ``blocked_by``
    holds only the blockers still open; ``marks`` names what shows the task in
    flight (:data:`in_flight.MARKS`, in that order), and ``disposition`` ranks
    them (``None`` when there are none).
    """

    ref: IssueRef
    title: str
    created_at: str
    kind: str
    blocked_by: tuple[IssueRef, ...]
    state: AdhocState
    marks: tuple[str, ...]
    disposition: Disposition | None


@dataclass(frozen=True)
class PlanningMarkers:
    """The progress markers parsed from a planning issue's comments."""

    epic: IssueRef | None
    docs_task: IssueRef | None
    tasks_filed: bool


@dataclass(frozen=True)
class Deferral:
    """The latest deferral recorded on a deferred item."""

    day: str
    reason: str
    revisit_when: str


def parse_latest_deferral(comments: Sequence[str]) -> Deferral | None:
    """The latest deferral among *comments* (oldest first), or None when there is none.

    A deferral is a comment whose first line is ``Deferred <YYYY-MM-DD>: <reason>``;
    its ``revisit_when`` is the value of its ``Revisit when:`` line (``""`` if absent).
    """
    for comment in reversed(comments):
        lines = comment.strip().splitlines()
        if not lines or not (m := _DEFERRED_RE.match(lines[0].strip())):
            continue
        revisit_when = ""
        for raw in lines[1:]:
            line = raw.strip()
            if line.startswith(_REVISIT_WHEN):
                revisit_when = line.removeprefix(_REVISIT_WHEN).strip()
                break
        return Deferral(day=m.group(1), reason=m.group(2).strip(), revisit_when=revisit_when)
    return None


def _ref(match: re.Match[str]) -> IssueRef:
    return IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))


def parse_planning_markers(comments: Sequence[str]) -> PlanningMarkers:
    """Parse the marker lines from *comments* (oldest first); the latest of each wins."""
    epic: IssueRef | None = None
    docs_task: IssueRef | None = None
    tasks_filed = False
    for comment in comments:
        for raw in comment.splitlines():
            line = raw.strip()
            if m := _EPIC_RE.match(line):
                epic = _ref(m)
            elif m := _DOCS_TASK_RE.match(line):
                docs_task = _ref(m)
            elif m := _TASKS_FILED_RE.match(line):
                tasks_filed = m.group(1).lower() == "yes"
    return PlanningMarkers(epic=epic, docs_task=docs_task, tasks_filed=tasks_filed)


def parse_epic_markers(comments: Sequence[str]) -> tuple[IssueRef, ...]:
    """Every epic an ``Epic:`` line in *comments* names, first mention first, no repeats.

    :func:`parse_planning_markers` keeps only the latest; a planning issue that
    grew several epics names each of them in turn.
    """
    found: dict[IssueRef, None] = {}
    for comment in comments:
        for raw in comment.splitlines():
            if m := _EPIC_RE.match(raw.strip()):
                found.setdefault(_ref(m), None)
    return tuple(found)


def _view(ref: IssueRef, fields: str) -> dict[str, Any]:
    raw = github.read_json(
        "issue", "view", str(ref.number), "--repo", f"{ref.owner}/{ref.repo}", "--json", fields
    )
    if not isinstance(raw, dict):
        raise ValueError(f"unexpected 'gh issue view' response for {ref.slug}: {raw!r}")
    return raw


def _open_children(kind: epics.StandingKind, home: str, name: str) -> list[IssueRef]:
    epic = epics.find_standing_epic(kind, home, name)
    if epic is None:
        raise QueueNotFoundError(f"no open '{kind.title_prefix}{name}' {kind.key} epic in {home}")
    return [c.ref for c in epics.child_states(epic) if c.state == "OPEN"]


def _authored_comments(view: dict[str, Any]) -> list[tuple[str, str]]:
    """Each comment as ``(author login, body)``, oldest first.

    A comment whose author GitHub does not give (a deleted account) has the
    login ``""``, which no trusted author matches.
    """
    out: list[tuple[str, str]] = []
    for c in view.get("comments") or []:
        author = c.get("author") or {}
        out.append((str(author.get("login") or ""), str(c.get("body") or "")))
    return out


def _labels(view: dict[str, Any]) -> set[str]:
    return {str(lbl.get("name") or "") for lbl in view.get("labels") or []}


def _size_record(view: dict[str, Any], trusted: frozenset[str]) -> SizeRecord:
    return epic_size.size_record(_labels(view), _authored_comments(view), trusted)


def _with_size_error(note: str, size: SizeRecord, *, keep_note: bool) -> str:
    """*note*, with *size*'s error appended (*keep_note*) or in its place."""
    if size.error is None:
        return note
    if keep_note and note:
        return f"{note} | size: {size.error}"
    return f"size: {size.error}"


def _latest_note(comments: list[str]) -> str:
    """The first line of the latest comment, or ``""`` when there is none."""
    if not comments:
        return ""
    lines = comments[-1].strip().splitlines()
    return lines[0].strip() if lines else ""


def _oldest_first(items: list[QueueItem]) -> list[QueueItem]:
    return sorted(items, key=lambda i: (i.created_at, i.ref.slug))


def intake_items(home: str, name: str) -> list[QueueItem]:
    """The open intake items under *home*'s intake epic for *name*, oldest first."""
    authors = load_trusted_authors()
    items: list[QueueItem] = []
    for ref in _open_children(epics.INTAKE, home, name):
        view = _view(ref, "title,createdAt")
        items.append(
            QueueItem(
                ref=ref,
                title=str(view.get("title") or ""),
                created_at=str(view.get("createdAt") or ""),
                state="waiting",
                note=_latest_note(trusted_comments.bodies(ref, authors)),
                epic=None,
            )
        )
    return _oldest_first(items)


def _planning_state(
    labels: set[str], markers: PlanningMarkers, *, advisory_held: bool
) -> tuple[QueueState, bool]:
    """The item's state, and whether an advisory hold demoted it from ready-to-close.

    A held item that would otherwise be ``ready-to-close`` is ``in-progress``:
    closing it would close the advisory with the fix unwritten.
    """
    if epics.PLANNING_IN_PROGRESS_LABEL not in labels:
        return "waiting", False
    if markers.epic is None or markers.docs_task is None or not markers.tasks_filed:
        return "in-progress", False
    docs = _view(markers.docs_task, "state")
    if str(docs.get("state")).upper() != "CLOSED":
        return "in-progress", False
    if advisory_held:
        return "in-progress", True
    return "ready-to-close", False


@dataclass(frozen=True)
class AdvisoryHold:
    """Whether security advisories hold a planning issue back from closing, and why.

    - ``tracking``: the GHSA IDs of every listed advisory whose ``Tracking
      issue:`` lines name the issue, whether or not its body names them.
    - ``unreadable``: the GHSA IDs a ``security``-labelled issue's
      ``Security advisory:`` lines name that the advisory listing does not
      return. The advisory cannot be read, so it counts as tracking: absence
      is never "not tracking".
    - ``broken``: a note for an issue with ``Security advisory:`` lines but
      no ``security`` label, when nothing tracks it. A body line alone never
      holds; the pair is reported as broken instead.
    """

    tracking: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    broken: str | None = None

    @property
    def held(self) -> bool:
        return bool(self.tracking or self.unreadable)


def _named_advisories(ref: IssueRef, body: str) -> tuple[str, ...]:
    """The GHSA IDs *body*'s ``Security advisory:`` lines name; refuse one naming another org.

    The line always names an advisory in the issue's own org's ``.github``;
    one naming any other org is refused with :class:`AdvisoryLineError`,
    never ignored. Org names compare case-insensitively, as GitHub's do.
    """
    lines = security_records.parse_advisory_lines(body)
    foreign = [
        f"{org}/.github {ghsa}" for org, ghsa in lines if org.casefold() != ref.owner.casefold()
    ]
    if foreign:
        raise AdvisoryLineError(
            f"{ref.slug}: its Security advisory: line names {', '.join(foreign)}, "
            f"not an advisory in {ref.owner}/.github, the issue's own org; "
            "correct the line before the planning queue can be read"
        )
    return tuple(dict.fromkeys(ghsa for _org, ghsa in lines))


def advisory_hold(
    ref: IssueRef, body: str, labels: set[str], advisories: Sequence[Advisory]
) -> AdvisoryHold:
    """The one planning-hold rule, for the issue *ref* with *body* and *labels*.

    *advisories* is the listing of *ref*'s org's advisory home. The rule
    trusts the ``security`` label and the advisories' own ``Tracking issue:``
    lines, never a body line alone (see :class:`AdvisoryHold`). A
    ``Security advisory:`` line naming another org is refused
    (:class:`AdvisoryLineError`). Pure: it reads nothing from GitHub.
    """
    named = _named_advisories(ref, body)
    key = ref.slug.casefold()
    tracking = tuple(
        dict.fromkeys(
            a.ghsa_id for a in advisories if any(t.slug.casefold() == key for t in a.tracking)
        )
    )
    labelled = security_records.SECURITY_LABEL in labels
    listed = {a.ghsa_id.casefold() for a in advisories}
    unreadable = tuple(ghsa for ghsa in named if ghsa.casefold() not in listed) if labelled else ()
    broken = None
    if named and not labelled and not tracking:
        broken = (
            f"broken pair: the issue names {', '.join(named)} on a Security advisory: line "
            "but has no security label, so no advisory holds it — have the human check the pair"
        )
    return AdvisoryHold(tracking=tracking, unreadable=unreadable, broken=broken)


def hold_reason(ref: IssueRef, hold: AdvisoryHold, *, subject: str) -> str:
    """What holds *ref* (*subject* names it in the text) and the command that hands it on."""
    parts: list[str] = []
    if hold.tracking:
        n = len(hold.tracking)
        tracks = "advisory still tracks" if n == 1 else "advisories still track"
        parts.append(f"{n} {tracks} {subject} ({', '.join(hold.tracking)})")
    if hold.unreadable:
        n = len(hold.unreadable)
        what = "advisory" if n == 1 else "advisories"
        parts.append(
            f"{n} {what} {subject} names cannot be read ({', '.join(hold.unreadable)}): "
            "re-run with a token that can read the repository's advisories, or have the "
            "human check it"
        )
    command = security_records.retarget_command("<GHSA>", ref, ())
    return (
        f"{'; '.join(parts)} — retarget each to the task or tasks that fix it with "
        f"{command} --to <task>"
    )


def _held_note(ref: IssueRef, hold: AdvisoryHold) -> str:
    return f"held: {hold_reason(ref, hold, subject='this planning issue')}"


def _org_advisories(org: str, *, named: bool) -> list[Advisory]:
    """Every advisory in *org*'s advisory home, or none when the org can hold none.

    Security records live only in ``<org>/.github``. When an item of *org*
    names an advisory, the home is resolved with
    :func:`security_records.advisory_home`. When none does, an org with no
    ``.github`` holds no advisory that could track an item, so nothing is
    listed; any failed GitHub call propagates.
    """
    if not named and not github.repo_exists(org, ".github"):
        return []
    return security_records.list_advisories(security_records.advisory_home(org))


def read_advisory_hold(ref: IssueRef, body: str, labels: set[str]) -> AdvisoryHold:
    """:func:`advisory_hold` for one issue, listing its org's advisories itself.

    The docs-PR close (:func:`vergil_tooling.lib.planning_close.find`) uses it,
    so the close and the planning view apply the same rule. Any failed GitHub
    call propagates.
    """
    advisories = _org_advisories(ref.owner, named=bool(_named_advisories(ref, body)))
    return advisory_hold(ref, body, labels, advisories)


def _holds(views: Sequence[tuple[IssueRef, dict[str, Any]]]) -> dict[IssueRef, AdvisoryHold]:
    """Each item's :func:`advisory_hold`, with each org's advisories listed once."""
    named: dict[str, bool] = {}
    for ref, view in views:
        names = bool(_named_advisories(ref, str(view.get("body") or "")))
        named[ref.owner] = named.get(ref.owner, False) or names
    listings = {org: _org_advisories(org, named=any_named) for org, any_named in named.items()}
    return {
        ref: advisory_hold(ref, str(view.get("body") or ""), _labels(view), listings[ref.owner])
        for ref, view in views
    }


def planning_items(home: str, name: str) -> list[QueueItem]:
    """The open planning items under *home*'s planning epic for *name*, oldest first.

    An item is held back from ``ready-to-close`` while :func:`advisory_hold`
    holds it (spec 4.4b of epic vergil-project/.github#517, spec 4.2 of #573,
    spec 6.3 of #1331; #3826, #3996). A broken pair replaces the item's
    latest-comment note but does not hold it.
    """
    trusted = epic_size.trusted_logins()
    authors = load_trusted_authors()
    views = [
        (ref, _view(ref, "title,createdAt,labels,comments,body"))
        for ref in _open_children(epics.PLANNING, home, name)
    ]
    holds = _holds(views)
    items: list[QueueItem] = []
    for ref, view in views:
        comments = trusted_comments.bodies(ref, authors)
        markers = parse_planning_markers(comments)
        hold = holds[ref]
        state, demoted = _planning_state(_labels(view), markers, advisory_held=hold.held)
        size = _size_record(view, trusted)
        if demoted:
            note = _held_note(ref, hold)
        elif hold.broken is not None:
            note = hold.broken
        else:
            note = _latest_note(comments)
        items.append(
            QueueItem(
                ref=ref,
                title=str(view.get("title") or ""),
                created_at=str(view.get("createdAt") or ""),
                state=state,
                note=_with_size_error(note, size, keep_note=demoted or hold.broken is not None),
                epic=markers.epic,
                size=size,
            )
        )
    return _oldest_first(items)


def deferred_items(home: str, name: str) -> list[QueueItem]:
    """The open deferred items under *home*'s deferred epic for *name*.

    Least recently deferred first; an item with no deferral comment sorts first,
    with an empty note.
    """
    trusted = epic_size.trusted_logins()
    authors = load_trusted_authors()
    keyed: list[tuple[str, QueueItem]] = []
    for ref in _open_children(epics.DEFERRED, home, name):
        view = _view(ref, "title,createdAt,labels,comments")
        size = _size_record(view, trusted)
        deferral = parse_latest_deferral(trusted_comments.bodies(ref, authors))
        note = (
            f"Deferred {deferral.day}: {deferral.reason} | Revisit when: {deferral.revisit_when}"
            if deferral is not None
            else ""
        )
        item = QueueItem(
            ref=ref,
            title=str(view.get("title") or ""),
            created_at=str(view.get("createdAt") or ""),
            state="waiting",
            note=_with_size_error(note, size, keep_note=deferral is not None),
            epic=None,
            size=size,
        )
        keyed.append((deferral.day if deferral is not None else "", item))
    keyed.sort(key=lambda pair: (pair[0], pair[1].created_at, pair[1].ref.slug))
    return [item for _, item in keyed]


def _adhoc_epics(home: str, bare: str | None) -> list[IssueRef]:
    """The live ad-hoc epics in *home*, lowest number first; only *bare*'s when given.

    One ``epic`` + ``ad-hoc`` label search, refused when it reaches its limit.
    A row is a live epic only when its title starts ``Epic (ad hoc): `` and is
    not the legacy archive form ``Epic (ad hoc): <bare> — <quarter>`` (a
    current-form archive is titled ``Archive (ad hoc): …``). Two live epics for
    one repo are refused, never guessed between.
    """
    owner, home_repo = home.split("/", 1)
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        *[arg for label in epics.ADHOC.labels for arg in ("--label", label)],
        "--state",
        "open",
        "--limit",
        str(epics.LISTING_LIMIT),
        "--json",
        "number,title",
    )
    epics.refuse_truncated(raw, epics.LISTING_LIMIT, f"the open epic + ad-hoc issues of {home}")
    if not isinstance(raw, list):
        raise ValueError(f"unexpected 'gh issue list' response for {home}: {raw!r}")
    by_name: dict[str, list[int]] = {}
    for row in raw:
        if not isinstance(row, dict):
            raise ValueError(f"unexpected 'gh issue list' row for {home}: {row!r}")
        title = str(row.get("title") or "")
        if not title.startswith(epics.ADHOC.title_prefix):
            continue
        if epics._LEGACY_ADHOC_ARCHIVE_RE.match(title):
            continue
        name = title.removeprefix(epics.ADHOC.title_prefix)
        if bare is not None and name != bare:
            continue
        by_name.setdefault(name, []).append(int(row["number"]))
    refs: list[IssueRef] = []
    for name, numbers in by_name.items():
        if len(numbers) > 1:
            ordered = sorted(numbers)
            title = f"{epics.ADHOC.title_prefix}{name}"
            raise epics.EpicTitleAmbiguousError(
                f"multiple ad-hoc epics titled {title!r} in {home} "
                f"({', '.join(f'#{n}' for n in ordered)})",
                [IssueRef(owner, home_repo, n) for n in ordered],
            )
        refs.append(IssueRef(owner, home_repo, numbers[0]))
    return sorted(refs, key=lambda r: r.number)


def _adhoc_kind(labels: set[str]) -> str:
    """``validation`` or ``deployment`` when the task carries that label, else ``task``."""
    for kind in _OPERATIONAL_KINDS:
        if kind in labels:
            return kind
    return "task"


def _repo_of(ref: IssueRef) -> str:
    return f"{ref.owner}/{ref.repo}"


def _in_flight_signals(
    refs: Sequence[IssueRef], roots: Sequence[Path] | None
) -> dict[str, in_flight.RepoSignals]:
    """The in-flight signals of each repo among *refs*, read from its main clone.

    Nothing is read when *refs* is empty. *roots* default to
    :func:`live_branches.default_repo_roots`, which raises
    :class:`live_branches.RepoSetError` where the platform has none.
    """
    repos = sorted({_repo_of(ref) for ref in refs})
    if not repos:
        return {}
    clones = in_flight.find_clones(
        repos, roots if roots is not None else live_branches.default_repo_roots()
    )
    return {repo: in_flight.repo_signals(repo, clones[repo]) for repo in repos}


def adhoc_items(
    home: str, *, repo: str | None = None, roots: Sequence[Path] | None = None
) -> list[AdhocItem]:
    """The open tasks under every live ad-hoc epic in *home*.

    Grouped by repo (repos in name order), oldest first within each. With
    *repo* (``owner/name``), only that repo's ad-hoc epic is read, and a repo
    with none gives an empty list. The caller resolves *home*, the org's
    ``.github``, with :func:`epics.require_org_home`. A task with any in-flight mark is
    ``in-flight``, whatever its blockers; the marks of each repo with open
    tasks are read from its main clone under *roots* (default: the platform's
    repo roots). A failed read of the epics, a sub-issue list, a task, a
    blocker list, a blocker's state or any in-flight source raises, as does a
    truncated epic search or a repo with no main clone; nothing is shown
    short or wrongly ready.
    """
    bare = None if repo is None else _bare_name(repo)
    return _standing_items(_adhoc_epics(home, bare), repo=None, roots=roots)


def hoshin_items(
    home: str, *, repo: str | None = None, roots: Sequence[Path] | None = None
) -> list[AdhocItem]:
    """The open tasks under *home*'s hoshin epic, as :func:`adhoc_items` gives them.

    The epic is ``Epic (hoshin): <owner>``, *home*'s owner, looked up with
    :func:`epics.find_standing_epic` (never created); a home with none gives
    an empty list. The caller resolves *home*, the org's ``.github``, with
    :func:`epics.require_org_home`. With *repo*
    (``owner/name``), only the epic's children in that repo are listed.
    Fields, states, in-flight marks, dispositions, order and failures are
    :func:`adhoc_items`'s.
    """
    if repo is not None:
        _bare_name(repo)
    owner = home.split("/", 1)[0]
    epic = epics.find_standing_epic(epics.HOSHIN, home, owner)
    return _standing_items([] if epic is None else [epic], repo=repo, roots=roots)


def _bare_name(repo: str) -> str:
    """The name part of an ``owner/name`` *repo*; a bare name is a ``ValueError``."""
    if "/" not in repo:
        raise ValueError(f"repo must be 'owner/name' (got {repo!r})")
    return repo.split("/", 1)[1]


def _standing_items(
    epic_refs: Sequence[IssueRef], *, repo: str | None, roots: Sequence[Path] | None
) -> list[AdhocItem]:
    """The open children of *epic_refs* as tasks, by repo and oldest first.

    The shared body of :func:`adhoc_items` and :func:`hoshin_items`. With
    *repo* (``owner/name``), a child in any other repo is left out before it
    is read (GitHub names ignore case). Every read failure raises.
    """
    found: list[tuple[IssueRef, dict[str, Any], tuple[IssueRef, ...]]] = []
    for epic in epic_refs:
        for child in epics.child_states(epic):
            if child.state != "OPEN":
                continue
            if repo is not None and _repo_of(child.ref).casefold() != repo.casefold():
                continue
            view = _view(child.ref, "title,createdAt,labels")
            open_blockers = tuple(
                dep for dep in epics.blockers_of(child.ref) if epics.issue_state(dep) != "CLOSED"
            )
            found.append((child.ref, view, open_blockers))
    signals = _in_flight_signals([ref for ref, _, _ in found], roots)
    items: list[AdhocItem] = []
    for ref, view, open_blockers in found:
        labels = _labels(view)
        marks = in_flight.marks_for(ref.number, signals[_repo_of(ref)])
        state: AdhocState = "in-flight" if marks else "blocked" if open_blockers else "ready"
        items.append(
            AdhocItem(
                ref=ref,
                title=str(view.get("title") or ""),
                created_at=str(view.get("createdAt") or ""),
                kind=_adhoc_kind(labels),
                blocked_by=open_blockers,
                state=state,
                marks=marks,
                disposition=in_flight.disposition(marks),
            )
        )
    return sorted(items, key=lambda i: (_repo_of(i.ref), i.created_at, i.ref.number))


def _age(created_at: str, now: datetime) -> str:
    created = datetime.fromisoformat(created_at)
    return f"{(now - created).days}d"


_SIZED, _UNSIZED, _IN_ERROR = 0, 1, 2


def _size_group(item: QueueItem) -> int:
    """Sized, unsized (or no size record at all) or in error."""
    if item.size is None:
        return _UNSIZED
    if item.size.error is not None:
        return _IN_ERROR
    return _UNSIZED if item.size.size is None else _SIZED


def sort_planning(items: Sequence[QueueItem], key: PlanningSort) -> list[QueueItem]:
    """*items* in the order *key* names; ties oldest first, then by slug.

    ``age`` is oldest first. ``size`` orders the sized items by
    :data:`epic_size.SIZES`, then the unsized, then those in error. ``tasks``
    orders the sized items by estimated task count, then the unsized, then
    those in error. An item with no size record sorts as unsized.
    """
    if key not in PLANNING_SORTS:
        raise ValueError(f"unknown planning sort {key!r} (expected one of {PLANNING_SORTS})")

    def order(item: QueueItem) -> tuple[int, int, str, str]:
        if key == "age":
            return (0, 0, item.created_at, item.ref.slug)
        group = _size_group(item)
        rank = 0
        if group == _SIZED and item.size is not None and item.size.size is not None:
            # A sized record always has its task count.
            tasks = item.size.tasks or 0
            rank = epic_size.SIZES.index(item.size.size) if key == "size" else tasks
        return (group, rank, item.created_at, item.ref.slug)

    return sorted(items, key=order)


def parse_size_filter(text: str) -> frozenset[str]:
    """The tokens of a ``--size`` value (``S,M``, ``S,unsized``); refuses an unknown one.

    Tokens are the sizes of :data:`epic_size.SIZES` and ``unsized``, comma
    separated; an empty or unknown token raises ValueError.
    """
    allowed = (*epic_size.SIZES, UNSIZED_FILTER)
    tokens = [token.strip() for token in text.split(",")]
    unknown = [token for token in tokens if token not in allowed]
    if unknown:
        raise ValueError(
            f"unknown size {', '.join(repr(t) for t in unknown)} "
            f"(expected a comma list of {', '.join(allowed)})"
        )
    return frozenset(tokens)


def filter_planning(items: Sequence[QueueItem], sizes: frozenset[str]) -> list[QueueItem]:
    """The *items* whose size is in *sizes* (``unsized`` for the unsized), and every item in error.

    A filter never hides a problem: an item in error is always kept. An item
    with no size record counts as unsized.
    """
    kept: list[QueueItem] = []
    for item in items:
        group = _size_group(item)
        if group == _IN_ERROR:
            kept.append(item)
        elif group == _UNSIZED:
            if UNSIZED_FILTER in sizes:
                kept.append(item)
        elif item.size is not None and item.size.size in sizes:
            kept.append(item)
    return kept


def size_errors(items: Sequence[QueueItem]) -> list[tuple[QueueItem, str]]:
    """Each item in *items* whose size is in error, with its error, in order."""
    return [
        (item, item.size.error)
        for item in items
        if item.size is not None and item.size.error is not None
    ]


def render_text(items: Sequence[QueueItem], *, now: datetime) -> str:
    """One line per item: ``<slug>  <age>  [<size>  ]<state>  <title>[  — <note>]``.

    The size (``M/14``, ``?`` unsized, ``!`` in error) is shown only for an
    item with a size record (planning and deferred).
    """
    lines = []
    for item in items:
        line = f"{item.ref.slug}  {_age(item.created_at, now)}  "
        if item.size is not None:
            line += f"{item.size.display}  "
        line += f"{item.state}  {item.title}"
        if item.note:
            line += f"  — {item.note}"
        lines.append(line)
    return "\n".join(lines)


def to_json(items: Sequence[QueueItem]) -> list[dict[str, Any]]:
    """The items as JSON-serializable ``QueueItem`` dicts.

    A size record is flattened into ``epic_size``, ``estimated_tasks``,
    ``confidence`` and ``size_error``; an item without one (intake) has none
    of these keys, and no ``size`` key.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        row = asdict(item)
        del row["size"]
        if item.size is not None:
            row["epic_size"] = item.size.size
            row["estimated_tasks"] = item.size.tasks
            row["confidence"] = item.size.confidence
            row["size_error"] = item.size.error
        out.append(row)
    return out


def render_adhoc_text(items: Sequence[AdhocItem], *, now: datetime) -> str:
    """One line per task: ``<slug>  <age>  <state>  <kind>  <title>`` and its notes.

    An in-flight task adds ``  — <disposition>: <marks>``; a task with an open
    blocker adds ``  — blocked by <refs>``.
    """
    lines = []
    for item in items:
        line = (
            f"{item.ref.slug}  {_age(item.created_at, now)}  {item.state}  "
            f"{item.kind}  {item.title}"
        )
        if item.marks:
            line += f"  — {item.disposition}: {', '.join(item.marks)}"
        if item.blocked_by:
            line += f"  — blocked by {', '.join(dep.slug for dep in item.blocked_by)}"
        lines.append(line)
    return "\n".join(lines)


def adhoc_to_json(items: Sequence[AdhocItem]) -> list[dict[str, Any]]:
    """The tasks as JSON-serializable ``AdhocItem`` dicts (tuples become lists)."""
    out: list[dict[str, Any]] = []
    for item in items:
        row = asdict(item)
        row["blocked_by"] = list(row["blocked_by"])
        row["marks"] = list(row["marks"])
        out.append(row)
    return out
