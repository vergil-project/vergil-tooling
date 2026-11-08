"""Read-only views of the intake, planning and deferred queues.

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

A planning issue is never ``ready-to-close`` while any advisory still tracks
it (names it on a ``Tracking issue:`` line), whether or not its body names
that advisory: it stays ``in-progress``, with a note naming those advisories
(by GHSA ID only) and saying to retarget each to the fix task(s) first. The
``security`` label plays no part: an item is a security item by its body's
``Security advisory:`` line alone, since holding is the safe direction and
``vrg-epic-audit`` reports a line without its label as a broken pair. A line
naming an org other than the issue's own is refused
(:class:`AdvisoryLineError`), never ignored.

Advisories are listed once per org per view. An org with no public
``.github`` holds no advisories, so its listing is skipped, unless one of its
items names an advisory: then the home is resolved anyway and refused loudly.

Nothing here mutates GitHub: the queue epic is looked up with
:func:`epics.find_standing_epic` (never created), and a missing queue epic is
reported as :class:`QueueNotFoundError` rather than an empty queue.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from vergil_tooling.lib import epics, github, security_records
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from collections.abc import Sequence

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


class QueueNotFoundError(LookupError):
    """The home has no live queue epic of the requested kind."""


class AdvisoryLineError(ValueError):
    """A planning item's ``Security advisory:`` line names an org other than its own."""


@dataclass(frozen=True)
class QueueItem:
    """One open queue item, as the views render it."""

    ref: IssueRef
    title: str
    created_at: str
    state: QueueState
    note: str
    epic: IssueRef | None


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


def _comment_bodies(view: dict[str, Any]) -> list[str]:
    return [str(c.get("body") or "") for c in view.get("comments") or []]


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
    items: list[QueueItem] = []
    for ref in _open_children(epics.INTAKE, home, name):
        view = _view(ref, "title,createdAt,comments")
        items.append(
            QueueItem(
                ref=ref,
                title=str(view.get("title") or ""),
                created_at=str(view.get("createdAt") or ""),
                state="waiting",
                note=_latest_note(_comment_bodies(view)),
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


def _held_note(ghsa_ids: tuple[str, ...]) -> str:
    tracks = "advisory still tracks" if len(ghsa_ids) == 1 else "advisories still track"
    return (
        f"held: {len(ghsa_ids)} {tracks} this planning issue "
        f"({', '.join(ghsa_ids)}) — retarget each to the task or tasks "
        "that fix it with vrg-security-record retarget"
    )


def _names_advisory(ref: IssueRef, body: str) -> bool:
    """Whether *body* has a ``Security advisory:`` line; refuse one naming another org.

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
    return bool(lines)


def _org_advisories(org: str, *, named: bool) -> list[Advisory]:
    """Every advisory in *org*'s advisory home, or none when the org can hold none.

    Security records live only in a public ``<org>/.github``. When an item of
    *org* names an advisory, the home is resolved with
    :func:`security_records.advisory_home`, which refuses a home that is not
    public loudly. When none does, an org whose ``.github`` is absent or not
    public holds no advisory that could track an item, so nothing is listed;
    any failed GitHub call propagates.
    """
    if not named and not (
        github.repo_exists(org, ".github") and github.is_public(f"{org}/.github")
    ):
        return []
    return security_records.list_advisories(security_records.advisory_home(org))


def _tracking(views: Sequence[tuple[IssueRef, dict[str, Any]]]) -> dict[IssueRef, tuple[str, ...]]:
    """Each item's tracking advisories (GHSA IDs, in listing order), listed once per org."""
    named: dict[str, bool] = {}
    for ref, view in views:
        names = _names_advisory(ref, str(view.get("body") or ""))
        named[ref.owner] = named.get(ref.owner, False) or names
    held: dict[IssueRef, list[str]] = {}
    for org, any_named in named.items():
        for advisory in _org_advisories(org, named=any_named):
            for ref in advisory.tracking:
                held.setdefault(ref, []).append(advisory.ghsa_id)
    return {ref: tuple(dict.fromkeys(ghsas)) for ref, ghsas in held.items()}


def planning_items(home: str, name: str) -> list[QueueItem]:
    """The open planning items under *home*'s planning epic for *name*, oldest first.

    An item is held back from ``ready-to-close`` while any advisory still
    tracks it, whether or not its body names that advisory (spec 4.4b of epic
    vergil-project/.github#517, spec 4.2 of #573; #3826).
    """
    views = [
        (ref, _view(ref, "title,createdAt,labels,comments,body"))
        for ref in _open_children(epics.PLANNING, home, name)
    ]
    tracking = _tracking(views)
    items: list[QueueItem] = []
    for ref, view in views:
        comments = _comment_bodies(view)
        markers = parse_planning_markers(comments)
        labels = {str(lbl.get("name") or "") for lbl in view.get("labels") or []}
        holding = tracking.get(ref, ())
        state, demoted = _planning_state(labels, markers, advisory_held=bool(holding))
        items.append(
            QueueItem(
                ref=ref,
                title=str(view.get("title") or ""),
                created_at=str(view.get("createdAt") or ""),
                state=state,
                note=_held_note(holding) if demoted else _latest_note(comments),
                epic=markers.epic,
            )
        )
    return _oldest_first(items)


def deferred_items(home: str, name: str) -> list[QueueItem]:
    """The open deferred items under *home*'s deferred epic for *name*.

    Least recently deferred first; an item with no deferral comment sorts first,
    with an empty note.
    """
    keyed: list[tuple[str, QueueItem]] = []
    for ref in _open_children(epics.DEFERRED, home, name):
        view = _view(ref, "title,createdAt,comments")
        deferral = parse_latest_deferral(_comment_bodies(view))
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
            note=note,
            epic=None,
        )
        keyed.append((deferral.day if deferral is not None else "", item))
    keyed.sort(key=lambda pair: (pair[0], pair[1].created_at, pair[1].ref.slug))
    return [item for _, item in keyed]


def _age(created_at: str, now: datetime) -> str:
    created = datetime.fromisoformat(created_at)
    return f"{(now - created).days}d"


def render_text(items: Sequence[QueueItem], *, now: datetime) -> str:
    """One line per item: ``<slug>  <age>  <state>  <title>[  — <note>]``."""
    lines = []
    for item in items:
        line = f"{item.ref.slug}  {_age(item.created_at, now)}  {item.state}  {item.title}"
        if item.note:
            line += f"  — {item.note}"
        lines.append(line)
    return "\n".join(lines)


def to_json(items: Sequence[QueueItem]) -> list[dict[str, Any]]:
    """The items as JSON-serializable ``QueueItem`` dicts."""
    return [asdict(item) for item in items]
