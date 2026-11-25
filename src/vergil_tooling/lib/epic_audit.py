"""Audit for epic/task drift — work that slipped through auto-close.

``task_drift`` finds merged PRs whose linked task is still open;
``epic_drift`` finds open, non-perpetual epics whose children are all closed
(should have rolled up). ``render`` formats both sections for review;
``close_drift`` closes them with an explanatory comment — a human action, gated
by the caller.

The orphan pass (epic vergil-project/.github#176) enforces *every issue is a
legitimate top-level kind or has an epic parent*: it nativizes body-only
``Parent:`` links, strips the retired ``triage``/``idea``/``research`` labels,
and attaches every orphan to its home's intake epic.

The proxy checks (epic vergil-project/.github#1417): :func:`epic_children` lists
an epic's children with every proxy's beside them, :func:`proxy_link_problems`
checks one epic's ``Primary:``/``Proxy:`` pair, and :func:`proxy_link_drift`
checks every open proxy of a home.

**Trusted content** (epic vergil-project/.github#1467, spec §5.2 and §5.3).
The sweep acts only on trusted content: a merged PR's linkage lines count
only from a body the trust layer trusts (:func:`task_drift`), a body-only
``Parent:`` line only from a trusted body (:func:`body_only_parents`), a
child's close only when its actor is trusted (:func:`epic_drift`), a child's
reopen likewise (:func:`closed_epic_open_child`), and a proxy's ``Primary:``
line and its primary's close the same way (:func:`proxy_closures`). Untrusted
content a pass merely skips is reported on stderr, metadata only; an
untrusted close or reopen a pass would act on, or a read the layer cannot
complete, stops that item alone: it is added to the run's
:class:`~vergil_tooling.lib.trust.BatchStops`, and the run carries on.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import (
    epic_chain,
    epic_docs,
    epics,
    github,
    linkage,
    pending_release,
    proxies,
    release,
    roadmap,
    security_records,
    trust,
    trusted_comments,
)
from vergil_tooling.lib.labels import RETIRED_LABELS
from vergil_tooling.lib.trusted_authors import load_trusted_authors

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vergil_tooling.lib.trusted_authors import TrustedAuthors

# A closed-epic-with-open-child violation (issue #2259): the closed epic paired
# with the tuple of its still-open children.
EpicOpenChildViolation = tuple[epics.IssueRef, tuple[epics.IssueRef, ...]]


@dataclass(frozen=True)
class LinkPass:
    """What one of the orphan pass's link passes did.

    *linked* counts the items linked. *refused* holds one line per item not
    linked because its epic is closed, naming the item and the epic: closed is
    closed, so the pass reports it and goes on, and the run exits non-zero at
    the end (epic vergil-project/.github#1609).
    """

    linked: int
    refused: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskDrift:
    """A merged PR whose tracking task is still open."""

    repo: str
    task: int
    pr_number: int
    pr_url: str


#: Days in each merged-PR search of :func:`task_drift`. The org merges several
#: hundred PRs a month, close to search's 1000-result cap, so the window is
#: searched a week at a time and each week is held to the cap (#4038).
_DRIFT_SLICE_DAYS = 7


def _merged_qualifiers(since: str, today: date) -> list[str]:
    """``merged:`` search terms that cover *since* onward in week-long slices.

    The slices are disjoint and inclusive; the last is open-ended
    (``merged:>=``) so a PR merged today, in any time zone, is never missed.
    """
    start = date.fromisoformat(since)
    step = timedelta(days=_DRIFT_SLICE_DAYS)
    qualifiers: list[str] = []
    while start + step <= today:
        end = start + step - timedelta(days=1)
        qualifiers.append(f"merged:{start.isoformat()}..{end.isoformat()}")
        start = end + timedelta(days=1)
    qualifiers.append(f"merged:>={start.isoformat()}")
    return qualifiers


def _merged_prs(since: str, *, org: str, today: date) -> list[Any]:
    """Every PR in *org* merged on or after *since*, refusing a truncated slice."""
    prs: list[Any] = []
    for qualifier in _merged_qualifiers(since, today):
        raw: Any = github.read_json(
            "search",
            "prs",
            "--owner",
            org,
            # The --merged flag silently matches nothing; the merged: query term works.
            qualifier,
            "--limit",
            str(epics.LISTING_LIMIT),
            "--json",
            "number,repository,url,body",
        )
        epics.refuse_truncated(raw, epics.LISTING_LIMIT, f"the {org} PR search for {qualifier}")
        prs.extend(raw if isinstance(raw, list) else [])
    return prs


def _refs_to_check(body: str) -> list[str]:
    """The task refs a merged PR's *body* links, for :func:`task_drift` to check.

    A body with one linkage line is handled as it always was: its one ref,
    ``Ref`` or ``Closes``. A body with more than one (a batch PR, one
    ``Closes`` line per issue, epic vergil-project/.github#1019) yields every
    ``Closes`` ref and never a ``Ref`` one, which only associates the PR.
    """
    refs = linkage.extract_tracking_refs(body)
    if len(refs) == 1:
        return [refs[0][1]]
    return [ref for keyword, ref in refs if keyword == "Closes"]


def _task_of(ref: str, pr_repo: str) -> tuple[str, int]:
    """The ``(owner/repo, number)`` of linkage *ref* in a body of a PR in *pr_repo*."""
    # ``ref`` is ``"#N"`` (same-repo) or ``"owner/repo#N"`` (cross-repo).
    # Resolve the repo the task actually lives in — honor a cross-repo ref
    # instead of assuming the PR's own repo (issue #2111).
    repo_part, _, num = ref.rpartition("#")
    return repo_part or pr_repo, int(num)


def _open_task_drift(entry: Any, pr_repo: str, ref: str) -> TaskDrift | None:
    """The drift record for *ref*, linked by merged PR *entry*, or None if it is not drift."""
    task_repo, task = _task_of(ref, pr_repo)
    try:
        issue: Any = github.read_json(
            "issue", "view", str(task), "--repo", task_repo, "--json", "state,title,body,labels"
        )
    except github.GitHubAPIError:
        # The task is in a repo this run can't see (cross-org or
        # deleted). Not this org's drift to close — warn and skip rather than
        # abort the whole sweep.
        print(
            f"vrg-epic-audit: skipping {task_repo}#{task} (referenced by "
            f"{pr_repo} PR #{entry.get('number')}) — not found or not "
            "accessible from this run.",
            file=sys.stderr,
        )
        return None
    if not isinstance(issue, dict):
        return None
    if str(issue.get("state") or "").upper() != "OPEN":
        return None
    labels = {str((label or {}).get("name", "")) for label in (issue.get("labels") or [])}
    if "epic" in labels:
        # A PR may legitimately ``Ref`` an epic (associating the PR with the
        # epic). That is not a slipped task: epic closure is owned by
        # epic_drift/rollup, which enforces all_children_closed. Closing an
        # epic here would bypass that guard and orphan its open children
        # (issue #2259, Fix A).
        return None
    if labels & epics.operational_labels():
        # An operational task (validation/deployment) closes on a recorded
        # ``Outcome:`` comment — a merged PR ``Ref``ing it is not the
        # slipped-task drift this sweep closes.
        return None
    # A merged release PR Refs its release: X.Y.Z tracking issue, which stays
    # open as vrg-release bookkeeping — not drift. Skip it so the audit does
    # not flag release issues as slipped tasks. See issue #1984.
    if release.is_release_tracking_issue(
        task_repo, task, title=str(issue.get("title") or ""), body=str(issue.get("body") or "")
    ):
        return None
    # A queue (intake/planning/deferred) item is never a PR-tracked task: its parent
    # (the queue epic), not a kind label, marks it (epic .github#176). Nor is an
    # ops issue under the operations epic: it is closed by hand once the problem
    # is dealt with, never by a PR that mentions it (epic .github#1494).
    task_owner, task_bare = task_repo.split("/", 1)
    parent = epics.parent_of(epics.IssueRef(task_owner, task_bare, task))
    if parent is not None and epics.unbuildable_kind(parent) is not None:
        return None
    return TaskDrift(
        repo=task_repo, task=task, pr_number=int(entry["number"]), pr_url=str(entry["url"])
    )


def task_drift(
    since: str,
    *,
    org: str,
    stops: trust.BatchStops,
    authors: TrustedAuthors,
    today: date | None = None,
) -> list[TaskDrift]:
    """Merged PRs (since *since*) whose linked task is still open.

    Every task a PR links is checked, one per ``Closes`` line of a batch PR
    (see :func:`_refs_to_check`). *today* (default: the current UTC date)
    only sets where the week-long search slices end.

    A linkage line counts only from a PR body the trust layer trusts, by the
    closing-reference roles (:data:`pending_release.CLOSING_ROLES`): its author
    and every editor, so an outsider's edit made after the merge makes it
    untrusted too. The search's copy of the body only picks the candidates;
    each PR with an open task is read again through the layer, and only the
    tasks its trusted body links are drift. An untrusted body is reported on
    stderr, metadata only, and yields no drift; a body the layer cannot read
    in full is a stop on that PR alone, added to *stops*.
    """
    if today is None:
        today = datetime.now(UTC).date()
    drift: list[TaskDrift] = []
    for entry in _merged_prs(since, org=org, today=today):
        pr_repo = str((entry.get("repository") or {}).get("nameWithOwner", ""))
        if not pr_repo:
            continue
        found = [
            record
            for ref in _refs_to_check(str(entry.get("body") or ""))
            if (record := _open_task_drift(entry, pr_repo, ref)) is not None
        ]
        if found:
            drift += _trusted_task_drift(found, pr_repo, stops=stops, authors=authors)
    return drift


def _trusted_task_drift(
    found: list[TaskDrift], pr_repo: str, *, stops: trust.BatchStops, authors: TrustedAuthors
) -> list[TaskDrift]:
    """The records of *found*, one PR's, whose task the PR's trusted body links."""
    number = found[0].pr_number
    try:
        content = trust.read_issue(
            pr_repo, number, roles=pending_release.CLOSING_ROLES, authors=authors
        )
    except trust.TrustReadError as exc:
        _read_failure(stops, pr_repo, number, "pr", exc)
        return []
    trust.report_skipped(
        [item for item in content.skipped if item.field == _BODY_FIELD], stream=sys.stderr
    )
    if content.body is None:
        return []
    linked = {
        (repo.lower(), task)
        for repo, task in (_task_of(ref, pr_repo) for ref in _refs_to_check(content.body))
    }
    return [record for record in found if (record.repo.lower(), record.task) in linked]


#: The field of a stop or skip on an issue's or PR's body.
_BODY_FIELD = "body"


def _read_failure(  # noqa: PLR0913 - the Skipped record's fields
    stops: trust.BatchStops,
    repo: str,
    number: int,
    kind: str,
    exc: trust.TrustReadError,
    field: str = _BODY_FIELD,
) -> None:
    """Add a read failure on *repo*#*number*'s *field* to *stops*, *exc* as its detail."""
    item = trust.Skipped(repo, number, kind, None, "", field, trust.READ_FAILURE)
    stops.add(item, trust.READ_FAILURE, detail=str(exc))


def _stop(stops: trust.BatchStops, exc: trust.UntrustedContentError) -> None:
    """Add the untrusted item *exc* stopped on to *stops*, with its own cause."""
    stops.add(exc.item, exc.item.cause)


def _slug(ref: epics.IssueRef) -> str:
    return f"{ref.owner}/{ref.repo}"


def _closes_trusted(
    children: list[epics.ChildState], *, stops: trust.BatchStops, authors: TrustedAuthors
) -> bool:
    """True iff the close of every closed child of an epic was made by a trusted actor.

    A close by an untrusted actor is not a close (spec §5.2): it is a stop on
    the epic, added to *stops* and naming the child's close, as is a close
    that cannot be read. A child open again since the roadmap counted it
    makes the epic no drift, with no stop.
    """
    for child in children:
        if child.state != "CLOSED":
            return False
        try:
            with github.target_org(child.ref.owner):
                trust.require_trusted_close(_slug(child.ref), child.ref.number, authors=authors)
        except trust.UntrustedContentError as exc:
            _stop(stops, exc)
            return False
        except trust.TrustReadError as exc:
            _read_failure(stops, _slug(child.ref), child.ref.number, "event", exc, "close")
            return False
    return True


def epic_drift(
    org: str, *, stops: trust.BatchStops, authors: TrustedAuthors
) -> list[roadmap.EpicSummary]:
    """Open, non-perpetual epics whose children are all closed (should roll up).

    A proxy epic is excluded with the standing epics (``roadmap.gather``): it
    lives as long as its primary, so an all-closed proxy is not drift.

    A child's close counts only when its actor is trusted (spec §5.2): an
    epic with a child closed by an untrusted actor, or whose close cannot be
    read, is a stop on that epic alone, added to *stops*, and is not drift.

    Scoped to the org's ``.github``, the home of every epic.
    """
    drift: list[roadmap.EpicSummary] = []
    for epic in roadmap.gather(org):
        if epic.total == 0 or epic.closed != epic.total:
            continue
        ref = epics.IssueRef(org, ".github", epic.number)
        if _closes_trusted(epics.child_states(ref), stops=stops, authors=authors):
            drift.append(epic)
    return drift


@dataclass(frozen=True)
class OperationalStatus:
    """An epic's outstanding (open) operational children, split runnable vs blocked.

    ``stopped`` holds the open operational children whose body the trust layer
    does not trust or cannot read in full, so neither runnable nor blocked can
    be said of them (spec §5.3 of epic vergil-project/.github#1467; #4971).
    """

    epic: epics.IssueRef
    runnable: tuple[epics.IssueRef, ...]  # open operational children, all blockers closed
    blocked: tuple[epics.IssueRef, ...]  # open operational children, a blocker still open
    by_kind: dict[epics.IssueRef, str]  # each pending child -> its kind (validation/deployment)
    stopped: tuple[epics.IssueRef, ...] = ()  # open operational children, body not trusted

    @property
    def pending(self) -> tuple[epics.IssueRef, ...]:
        """All outstanding operational children (runnable + blocked + stopped)."""
        return self.runnable + self.blocked + self.stopped


def _is_open(child: epics.ChildState) -> bool:
    return child.state == "OPEN"


def _blockers_closed(child: epics.ChildState, blockers: list[epics.IssueRef]) -> bool:
    """True iff every issue *child* is blocked by is closed, body and native alike.

    *blockers* are its ``Blocked-by:`` refs, already read
    (:func:`epics.blockers_or_stop`). Its native blocked-by relationships, a
    blocker in another repo or org included, come from the listing
    (:func:`epics.child_states_with_blockers`) or, for a reflink-fallback
    child, from their own read (:func:`epics.native_blockers_of`) (#5109). A
    native read that fails, or gives fewer blockers than GitHub counts,
    raises: a child is never runnable on a list that could not be read.
    """
    native = (
        child.native_blockers
        if child.native_blockers is not None
        else epics.native_blockers_of(child.ref)
    )
    if any(blocker.state != "CLOSED" for blocker in native):
        return False
    return epics.all_blockers_closed(blockers)


def operational_status(epic: epics.IssueRef, *, stops: trust.BatchStops) -> OperationalStatus:
    """Classify *epic*'s open operational children as runnable vs blocked, by kind.

    An operational child (validation, deployment, …) is runnable when every
    issue it is blocked by, through a ``Blocked-by:`` line or a native
    relationship (:func:`_blockers_closed`), is closed; otherwise it is
    blocked. This runnable-vs-blocked split, tagged by kind, is the honest
    "what still has to run" signal — and the seed of a future automator that
    runs operational tasks as their dependencies land.

    A child whose body is untrusted, or cannot be read in full, is a stop on
    that child only (:func:`epics.blockers_or_stop`): it is added to *stops*,
    listed as ``stopped`` (still pending, with its kind), and the rest are
    classified. A native blocked-by list that cannot be read in full raises
    :class:`epics.NativeBlockersReadError`, never "runnable"; only an open
    operational child's list is checked, so no other child fails the read.
    """
    runnable: list[epics.IssueRef] = []
    blocked: list[epics.IssueRef] = []
    stopped: list[epics.IssueRef] = []
    by_kind: dict[epics.IssueRef, str] = {}
    kinds: dict[epics.IssueRef, str | None] = {}

    def kind_of(child: epics.ChildState) -> str | None:
        """*child*'s operational kind, or None for a closed or plain child (read once)."""
        if child.state != "OPEN":
            return None
        if child.ref not in kinds:
            kinds[child.ref] = epics.operational_kind(child.ref)
        return kinds[child.ref]

    for child in epics.child_states_with_blockers(epic, wanted=lambda c: kind_of(c) is not None):
        kind = kind_of(child)
        if kind is None:
            continue
        by_kind[child.ref] = kind
        blockers = epics.blockers_or_stop(child.ref, stops)
        if blockers is None:
            stopped.append(child.ref)
            continue
        target = runnable if _blockers_closed(child, blockers) else blocked
        target.append(child.ref)
    return OperationalStatus(
        epic=epic,
        runnable=tuple(runnable),
        blocked=tuple(blocked),
        by_kind=by_kind,
        stopped=tuple(stopped),
    )


@dataclass(frozen=True)
class ChildStatus:
    """An epic's open children, split runnable vs blocked by their blockers.

    ``stopped`` holds the open children whose body is untrusted or cannot be
    read in full, so their dependencies are unknown (#4971).
    """

    epic: epics.IssueRef
    runnable: tuple[epics.IssueRef, ...]  # open children, all blockers closed
    blocked: tuple[epics.IssueRef, ...]  # open children, a blocker still open
    stopped: tuple[epics.IssueRef, ...] = ()  # open children, body not trusted


def child_status(epic: epics.IssueRef, *, stops: trust.BatchStops) -> ChildStatus:
    """Classify *every* open child of *epic* as runnable vs blocked.

    Where :func:`operational_status` is scoped to operational children (its
    runnable/blocked split feeds the "operation-pending" report), this classifies
    *all* open children — plain tasks included — by their ``Blocked-by:``
    lines and native blocked-by relationships (:func:`_blockers_closed`). It
    makes an epic driver's plain-task frontier (the open tasks whose every
    dependency is closed) machine-derivable instead of inferred from plan
    prose (#2269). A child whose body is untrusted, or cannot be read in full,
    is added to *stops* and listed as ``stopped``, never as runnable; the rest
    are classified. An open child's native list that cannot be read in full
    raises :class:`epics.NativeBlockersReadError`, never "runnable".
    """
    runnable: list[epics.IssueRef] = []
    blocked: list[epics.IssueRef] = []
    stopped: list[epics.IssueRef] = []
    for child in epics.child_states_with_blockers(epic, wanted=_is_open):
        if not _is_open(child):
            continue
        blockers = epics.blockers_or_stop(child.ref, stops)
        if blockers is None:
            stopped.append(child.ref)
            continue
        target = runnable if _blockers_closed(child, blockers) else blocked
        target.append(child.ref)
    return ChildStatus(
        epic=epic, runnable=tuple(runnable), blocked=tuple(blocked), stopped=tuple(stopped)
    )


def operational_pending(org: str, *, stops: trust.BatchStops) -> list[OperationalStatus]:
    """Open finite epics with outstanding operational children.

    Reports the "code-complete, operation-pending" state that keeps an epic
    honestly not-done: its code tasks may all be merged, but until its
    operational tasks (validations, deployments, …) run the epic is not finished.
    Only epics with at least one open operational child appear, a stopped one
    included; each stop is added to *stops*. Scoped to ``<org>/.github``, the
    home of every epic.
    """
    pending: list[OperationalStatus] = []
    for summary in roadmap.gather(org):
        status = operational_status(epics.IssueRef(org, ".github", summary.number), stops=stops)
        if status.pending:
            pending.append(status)
    return pending


# An operational task records its result as a comment; the unified success marker
# is ``Outcome: SUCCESS`` (``Outcome: PASS`` is recognized as a legacy alias, so
# validations closed before the unification are not falsely flagged). Kept narrow
# so an unresolved ``Outcome: SUCCESS / FAILURE`` template line does not read as a
# success.
_OPERATIONAL_SUCCESS_RE = re.compile(
    r"^\s*[-*]?\s*Outcome:\s*(?:SUCCESS|PASS)\s*$", re.MULTILINE | re.IGNORECASE
)


def closed_operational_without_success(org: str) -> list[str]:
    """Closed operational tasks with no recorded success comment (invariant).

    An operational task must close only after it runs and *succeeds*, recorded as
    a comment. A closed operational-labelled issue whose **trusted** comments carry
    no success ``Outcome:`` line is drift — most likely closed by hand — and is
    flagged. The comments are read through the trust layer
    (:func:`trusted_comments.bodies`, the trusted authors loaded once), so an
    outsider's ``Outcome: SUCCESS`` never hides a hand-closed task; each skipped
    comment is reported on stderr, metadata only, and a read the layer cannot
    complete raises. Searches every operational label. Report-only: like the
    other invariants, this is never auto-acted.
    """
    authors = load_trusted_authors()
    violations: list[str] = []
    for label in sorted(epics.operational_labels()):
        raw: Any = github.read_json(
            "search",
            "issues",
            "--owner",
            org,
            "--label",
            label,
            "--state",
            "closed",
            "--limit",
            str(epics.LISTING_LIMIT),
            "--json",
            "number,repository",
        )
        epics.refuse_truncated(
            raw, epics.LISTING_LIMIT, f"the {org} search for closed {label} issues"
        )
        for item in raw if isinstance(raw, list) else []:
            name_with_owner = str((item.get("repository") or {}).get("nameWithOwner", ""))
            if "/" not in name_with_owner:
                continue
            number = int(item["number"])
            owner, repo = name_with_owner.split("/", 1)
            ref = epics.IssueRef(owner, repo, number)
            bodies = "\n".join(trusted_comments.bodies(ref, authors))
            if not _OPERATIONAL_SUCCESS_RE.search(bodies):
                violations.append(f"{name_with_owner}#{number}")
    return violations


def epic_outside_dotgithub(org: str) -> list[str]:
    """Open ``epic``-labelled issues wrongly outside ``<org>/.github`` (invariant 1).

    Invariant 1 (epic #85): every epic lives in the org's ``.github``, so every
    open epic-labelled issue outside it is a violation. Returns the
    ``owner/repo#n`` slugs of violations.
    """
    raw: Any = github.read_json(
        "search",
        "issues",
        "--owner",
        org,
        "--label",
        "epic",
        "--state",
        "open",
        "--limit",
        str(epics.LISTING_LIMIT),
        "--json",
        "number,repository",
    )
    epics.refuse_truncated(raw, epics.LISTING_LIMIT, f"the {org} search for open epics")
    dotgithub = f"{org}/.github"
    violations: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        name_with_owner = str((item.get("repository") or {}).get("nameWithOwner", ""))
        if not name_with_owner or name_with_owner == dotgithub:
            continue
        violations.append(f"{name_with_owner}#{item['number']}")
    return violations


def stray_dotgithub_issue(org: str) -> list[str]:
    """Open issues in ``<org>/.github`` that violate invariant 2 (epic #85).

    The home holds only epics, the ``archive``-labelled per-quarter buckets (a
    legitimate top-level home issue, skipped like epics), and issues linked under
    an epic — managed tasks and queue (intake/planning/deferred) items alike (a
    queue item is parented under its queue epic, so no kind-label exemption is needed). An
    open issue that is none of these — an unlinked, non-epic, non-archive issue —
    is a stray; returns their
    slugs. When a candidate's parent cannot be confirmed as an epic it is
    reported (fail-loud, the human decides).
    """
    home = f"{org}/.github"
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        "--state",
        "open",
        "--limit",
        str(epics.LISTING_LIMIT),
        "--json",
        "number,labels",
    )
    epics.refuse_truncated(raw, epics.LISTING_LIMIT, f"the open issues of {home}")
    strays: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        labels = {str((label or {}).get("name", "")) for label in (item.get("labels") or [])}
        if "epic" in labels or "archive" in labels:
            continue
        number = int(item["number"])
        parent = epics.parent_of(epics.IssueRef(org, ".github", number))
        if parent is not None and epics.is_epic(parent):
            continue
        strays.append(f"{home}#{number}")
    return strays


def closed_epic_open_child(
    org: str,
    *,
    stops: trust.BatchStops,
    authors: TrustedAuthors,
    home: str | None = None,
    listing: list[epic_docs.EpicRecord] | None = None,
) -> list[EpicOpenChildViolation]:
    """Closed epics that still have an open child (invariant, issue #2259 Fix B).

    Core invariant: *an epic must never be closed while it has open children*. A
    PR that legitimately ``Ref``'d an epic once tripped ``task_drift`` into
    closing the epic directly, orphaning its open tasks — and no audit check
    caught the result. This is that check. Uses **native children** as the
    authoritative signal (``child_states``); perpetual (``ad-hoc``) epics are
    skipped — they never roll up, so being closed-with-open-children is
    not a violation for them. Report-only: like the other invariants it is never
    auto-acted, and closed is closed, so the sweep never reopens the epic; it
    is drift for a human. Scoped to the resolved epic *home* (default
    ``<org>/.github``).

    *listing* is the home's :func:`list_home_epics`, shared with
    :func:`epic_documents` so the audit lists the epics once; it is read here
    when not given. Only its closed epics are checked. A continuation is
    never one (epic vergil-project/.github#1609): its items are the chain's,
    read with its head's, and a closed continuation is no closed epic. An
    epic whose chain breaks a rule of spec §4.1 is left out here: its
    children cannot be read past the drift, which :func:`chain_drift`
    reports by name.

    A child is open for this check only when it was never reopened or its
    latest reopen was made by a trusted actor (spec §5.2): a child reopened by
    an untrusted actor, or one whose events cannot be read, is a stop on its
    epic alone, added to *stops*, and the epic is not a violation, so it is
    never reported on that child's account.
    """
    if home is None:
        home = f"{org}/.github"
    home_owner, home_repo = home.split("/", 1)
    if listing is None:
        listing = list_home_epics(home)
    violations: list[EpicOpenChildViolation] = []
    for record in listing:
        if record["state"] != "CLOSED":
            continue
        labels = set(record["labels"])
        if epics.is_standing(labels):
            continue  # standing epics never roll up; closed-with-open-child doesn't apply
        if epics.is_continuation(labels):
            continue  # a container of its head's chain, judged with the head
        epic = epics.IssueRef(home_owner, home_repo, record["number"])
        try:
            states = epics.child_states(epic)
        except epic_chain.ChainDriftError:
            continue  # its chain cannot be read past the drift, which chain_drift reports
        open_kids = tuple(cs.ref for cs in states if cs.state == "OPEN")
        if open_kids and _opens_trusted(open_kids, stops=stops, authors=authors):
            violations.append((epic, open_kids))
    return violations


def _opens_trusted(
    children: tuple[epics.IssueRef, ...], *, stops: trust.BatchStops, authors: TrustedAuthors
) -> bool:
    """True iff no child of *children* is open by an untrusted reopen; a stop otherwise."""
    for child in children:
        try:
            with github.target_org(child.owner):
                trust.require_trusted_reopen(_slug(child), child.number, authors=authors)
        except trust.UntrustedContentError as exc:
            _stop(stops, exc)
            return False
        except trust.TrustReadError as exc:
            _read_failure(stops, _slug(child), child.number, "event", exc, "reopen")
            return False
    return True


def list_home_epics(home: str) -> list[epic_docs.EpicRecord]:
    """Every ``epic``-labelled issue of *home*, open and closed, with no skips.

    The one epic listing the audit reads, shared by :func:`closed_epic_open_child`
    and :func:`epic_documents`, which each apply their own skips.
    """
    return epic_docs.list_epics(home, apply_skips=False)


# -- Epic documents (epic vergil-project/.github#819) --------------------------


@dataclass(frozen=True)
class EpicDocsFinding:
    """One problem with an epic's Documents section: epic *number* in the home."""

    number: int
    state: str
    problem: str


def epic_documents(
    org: str,
    *,
    home: str | None = None,
    listing: list[epic_docs.EpicRecord] | None = None,
) -> list[EpicDocsFinding]:
    """Problems with the Documents section of every epic of *home* (spec §6).

    Container and ``[archived]`` epics are skipped (:func:`epic_docs.is_skipped`).
    Every other epic needs a well-formed section: both markers, the heading and
    exactly the three entries, each a link or one of the notes. On a closed
    epic, no entry may read ``pending`` and every link must name a file (not a
    directory) that exists on its repo's default branch; an open epic's links are not checked,
    since a link written at handoff resolves only once its PR merges. Where the
    section sits in the body is not checked.

    Report-only. A contents-API 404 is a finding; any other API error
    propagates. The reads run under *home*'s owner's App token, so a 404 never
    comes from a token that cannot see the org. *listing* is
    :func:`list_home_epics`, read here when not given; a caller that passes it
    reads it under the same token.

    Runs on the read-only report path only: ``vrg-epic-audit --close`` does
    not run this check.
    """
    if home is None:
        home = f"{org}/.github"
    owner, repo = home.split("/", 1)
    with github.target_org(owner):
        if listing is None:
            listing = list_home_epics(home)
        branches: dict[str, str | None] = {}
        findings: list[EpicDocsFinding] = []
        for record in listing:
            if epic_docs.is_skipped(record["title"], record["labels"]):
                continue
            findings += [
                EpicDocsFinding(record["number"], record["state"], problem)
                for problem in _epic_docs_problems(record, owner, repo, branches)
            ]
    return findings


_MALFORMED = "malformed Documents section: "


def _epic_docs_problems(
    record: epic_docs.EpicRecord, owner: str, repo: str, branches: dict[str, str | None]
) -> list[str]:
    """The problems with one epic's section; *branches* caches default branches."""
    try:
        entries = epic_docs.parse(record["body"] or "", repo)
    except epic_docs.EpicDocsFormatError as exc:
        # The half-section error already names the section; say it only once.
        message = str(exc)
        if message.startswith(_MALFORMED):
            return [message]
        return [f"{_MALFORMED}{message}"]
    if entries is None:
        return ["no Documents section"]
    if record["state"] != "CLOSED":
        return []
    problems: list[str] = []
    for doc in epic_docs.DOCS:
        entry = entries[doc]
        label = doc.capitalize()
        if entry == epic_docs.PENDING:
            problems.append(f"{label} is still `pending` on a closed epic")
        elif isinstance(entry, epic_docs.DocLink):
            problems += _link_problems(label, entry, owner, branches)
    return problems


def _link_problems(
    label: str, link: epic_docs.DocLink, owner: str, branches: dict[str, str | None]
) -> list[str]:
    """Why *link* does not name a file on its repo's default branch, if it does not."""
    where = f"`{link.repo}/{link.path}`"
    if link.owner.lower() != owner.lower():
        return [f"{label} links {where} outside the {owner} org"]
    key = link.repo.lower()
    if key not in branches:
        try:
            branches[key] = epic_docs.default_branch(link.owner, link.repo)
        except github.GitHubAPIError as exc:
            if isinstance(exc, github.MissingGitHubTokenError) or not epic_docs.is_not_found(exc):
                raise
            branches[key] = None
    branch = branches[key]
    if branch is None:
        return [f"{label} links {where} in a repo that does not exist"]
    problems: list[str] = []
    if link.branch != branch:
        problems.append(
            f"{label} links {where} on branch `{link.branch}`, not the default branch `{branch}`"
        )
    kind = epic_docs.content_type(link.owner, link.repo, link.path, branch)
    if kind is None:
        problems.append(f"{label} link {where} does not resolve on `{branch}`")
    elif kind == "dir":
        problems.append(f"{label} links a directory, {where}, on `{branch}`, not a file")
    elif kind != "file":
        problems.append(f"{label} links {where}, a {kind} on `{branch}`, not a file")
    return problems


# -- orphan pass and label retirement (epic .github#176) -----------------------
#
# Invariant: every open issue in a managed repo is a legitimate top-level kind
# (epic, archive, release tracking issue) or has an epic parent. The checks below
# share one org-wide enumeration (:func:`open_issues`); the ``--close`` sweep
# applies them in the order nativize -> strip labels -> attach orphans.


@dataclass(frozen=True)
class OpenIssue:
    """One open issue (never a PR) in a managed repo, as the orphan pass sees it.

    ``native_parent`` is the epic of the issue's native link only, never a
    body ``Parent:`` line: the head of the chain it is linked in, so an issue
    in a continuation reads as under its head (epic vergil-project/.github#1609).
    """

    ref: epics.IssueRef
    title: str
    body: str
    labels: frozenset[str]
    native_parent: epics.IssueRef | None

    @property
    def body_parent(self) -> epics.IssueRef | None:
        """The parent a body ``Parent:`` line declares, or None.

        Read from the listing's copy of the body, which the trust layer has not
        judged: it only picks the candidates :func:`body_only_parents` reads
        again through the layer.
        """
        return _declared_parent(self.body)


def _declared_parent(body: str) -> epics.IssueRef | None:
    """The parent *body*'s first ``Parent:`` line declares, or None."""
    match = epics._PARENT_RE.search(body)
    if match is None:
        return None
    return epics.IssueRef(match.group(1), match.group(2), int(match.group(3)))


def open_issues(org: str) -> list[OpenIssue]:
    """Every open issue in *org*'s managed repos — the orphan pass's shared domain.

    Managed means non-archived with a ``vergil.toml`` (:func:`epics.is_managed_repo`);
    unmanaged repos are never read, so never written to. Enumerated per repo with
    the one batched listing of :func:`epics.list_open_issues` rather than ``gh
    search issues``: search caps at 1000 results and does not return the native
    parent, which every check here needs. Each issue's ``native_parent`` is the
    listing's ``epic``, the head of the chain it is linked in.
    """
    issues: list[OpenIssue] = []
    for bare in github.list_org_repos(org):
        if not epics.is_managed_repo(f"{org}/{bare}"):
            continue
        issues += [
            OpenIssue(
                ref=record.ref,
                title=record.title,
                body=record.body,
                labels=record.labels,
                native_parent=record.epic,
            )
            for record in epics.list_open_issues(f"{org}/{bare}")
        ]
    return issues


@dataclass(frozen=True)
class BodyParents:
    """The open issues' body-only ``Parent:`` links, as the trust layer judges them.

    *pairs* are the ``(task, epic)`` links of a trusted body naming an epic,
    which ``--close`` links natively; *held* are the issues whose body could
    not be read in full (each a stop), which no pass nativizes or attaches.
    """

    pairs: tuple[tuple[epics.IssueRef, epics.IssueRef], ...] = ()
    held: frozenset[epics.IssueRef] = frozenset()

    def parented(self) -> frozenset[epics.IssueRef]:
        """The issues the orphan attach leaves alone: linked by a trusted line, or held."""
        return frozenset(task for task, _ in self.pairs) | self.held


@dataclass(frozen=True)
class Orphan:
    """An open issue with no epic parent, and the queue home whose intake epic takes it.

    The intake epic is resolved (and created if absent) only on attach, so the
    read-only preview never writes.
    """

    ref: epics.IssueRef
    home: str  # the intake epic's home repo, "owner/repo"
    name: str  # the <name> in "Epic (intake): <name>"


def orphan_drift(
    org: str, *, parents: BodyParents, issues: list[OpenIssue] | None = None
) -> list[Orphan]:
    """Open issues in managed repos that are neither legitimate top-level nor parented.

    Excludes legitimate top-level kinds (:func:`epics.is_legitimate_top_level`:
    epics, archives, release tracking issues — so a release in flight is never
    attached), issues with a native parent, and the issues *parents* leaves
    alone: those whose trusted body ``Parent:`` line names an epic (they count
    as parented; :func:`nativize_parents` converts them) and those held by a
    stop. *parents* is :func:`body_only_parents` of the same *issues*, so a
    ``Parent:`` line in an untrusted body never counts as a parent. Each
    orphan targets the intake epic of its queue home (:func:`epics.queue_home`),
    ``<org>/.github``.
    """
    if issues is None:
        issues = open_issues(org)
    parented = parents.parented()
    orphans: list[Orphan] = []
    for issue in issues:
        if epics.is_legitimate_top_level(issue.ref, set(issue.labels), issue.title, issue.body):
            continue
        if issue.native_parent is not None:
            continue
        if issue.ref in parented:
            continue
        home, name = epics.queue_home(f"{issue.ref.owner}/{issue.ref.repo}")
        orphans.append(Orphan(ref=issue.ref, home=home, name=name))
    return orphans


class StandingEpicFull(RuntimeError):  # noqa: N818 - the name is the plan's interface
    """An intake epic is at the open-children cap; the orphan pass stopped before overflowing it.

    Raised before any write that would exceed the cap, naming every orphan not
    yet attached, so nothing is attached past the cap and nothing is silently
    dropped.
    """

    def __init__(
        self, epic: epics.IssueRef, open_children: int, pending: list[epics.IssueRef]
    ) -> None:
        self.epic = epic
        self.open_children = open_children
        self.pending = pending
        listed = ", ".join(ref.slug for ref in pending)
        super().__init__(
            f"intake epic {epic.slug} is full ({open_children} open); "
            f"{len(pending)} orphan(s) not attached: {listed}. Run epic-review intake."
        )


class _QueueCapacity:
    """Open-children counts per intake epic, checked before each link (the cap)."""

    def __init__(self) -> None:
        self._open: dict[epics.IssueRef, int] = {}

    def reserve(self, epic: epics.IssueRef, pending: list[epics.IssueRef]) -> None:
        """Count one more open child under *epic*, or raise if it is already full."""
        if epic not in self._open:
            self._open[epic] = sum(1 for c in epics.child_states(epic) if c.state == "OPEN")
        if self._open[epic] >= epics._ADHOC_ARCHIVE_SEGMENT_CAP:
            raise StandingEpicFull(epic, self._open[epic], pending)
        self._open[epic] += 1


def attach_orphans(
    orphans: list[Orphan], *, stops: trust.BatchStops, authors: TrustedAuthors
) -> LinkPass:
    """Attach each orphan, in order, under its home's intake epic.

    The intake epic is resolved (created if absent) once per home. Before each
    attach the epic's open children are checked against the cap; at the cap this
    raises :class:`StandingEpicFull` naming every orphan not yet attached. Writes
    to GitHub; the caller gates it and scopes the org token.

    A home whose intake epic is closed (only a closed one exists, or the epic
    is closed by the time of the link) attaches nothing there and reopens
    nothing: each of its orphans is a refused line in the result naming the
    orphan and the closed epic, and the other homes' orphans are still
    attached (closed is closed, epic vergil-project/.github#1609).

    An orphan an outsider opened (its author holds no role, as the trust layer
    judges it) is never attached: it is reported on stderr, metadata only
    (counts under Actions), and left out of the count and the cap, with a
    clean exit (spec §5.3 of epic vergil-project/.github#1467). An orphan the
    layer cannot read in full is a stop on that orphan alone, added to
    *stops*: it is not attached, and the rest are. Every orphan is judged
    before the first attach.
    """
    orphans = _trusted_orphans(orphans, stops=stops, authors=authors)
    targets: dict[tuple[str, str], epics.IssueRef | epics.ClosedEpicError] = {}
    capacity = _QueueCapacity()
    linked = 0
    refused: list[str] = []
    for index, orphan in enumerate(orphans):
        key = (orphan.home, orphan.name)
        if key not in targets:
            try:
                targets[key] = epics.ensure_standing_epic(epics.INTAKE, orphan.home, orphan.name)
            except epics.ClosedEpicError as exc:
                targets[key] = exc
        target = targets[key]
        if isinstance(target, epics.ClosedEpicError):
            refused.append(_closed_refusal(orphan.ref, target))
            continue
        pending = [o.ref for o in orphans[index:]]
        capacity.reserve(target, pending)
        try:
            epics.add_child(target, orphan.ref)
        except epics.ClosedEpicError as exc:
            targets[key] = exc
            refused.append(_closed_refusal(orphan.ref, exc))
            continue
        linked += 1
    return LinkPass(linked=linked, refused=tuple(refused))


def _closed_refusal(task: epics.IssueRef, exc: epics.ClosedEpicError) -> str:
    """The orphan pass's line for *task*, not linked because its epic is closed."""
    return f"{task.slug} not linked: {exc}"


def _trusted_orphans(
    orphans: list[Orphan], *, stops: trust.BatchStops, authors: TrustedAuthors
) -> list[Orphan]:
    """*orphans* less the outsider-opened (each reported) and the unreadable (each a stop)."""
    kept: list[Orphan] = []
    skipped: list[trust.Skipped] = []
    for orphan in orphans:
        ref = orphan.ref
        try:
            content = trust.read_issue(_slug(ref), ref.number, authors=authors)
            opened = trust.outsider_opened(content)
        except trust.TrustReadError as exc:
            _read_failure(stops, _slug(ref), ref.number, "issue", exc)
            continue
        if opened is None:
            kept.append(orphan)
        else:
            skipped.append(opened)
    trust.report_skipped(skipped, stream=sys.stderr)
    return kept


@dataclass(frozen=True)
class RetiredLabels:
    """An open issue still carrying retired intake kind labels (sorted)."""

    ref: epics.IssueRef
    labels: tuple[str, ...]


def retired_label_drift(org: str, *, issues: list[OpenIssue] | None = None) -> list[RetiredLabels]:
    """Open issues carrying ``triage``/``idea``/``research``, parented or not."""
    if issues is None:
        issues = open_issues(org)
    return [
        RetiredLabels(ref=issue.ref, labels=tuple(sorted(issue.labels & RETIRED_LABELS)))
        for issue in issues
        if issue.labels & RETIRED_LABELS
    ]


def strip_retired_labels(drift: list[RetiredLabels]) -> int:
    """Remove the retired kind labels from each issue; return the issue count.

    Only removes labels — an issue's parent is never touched (an item already
    routed keeps its epic; an unparented one is attached by the orphan pass).
    """
    for item in drift:
        github.run(
            "issue",
            "edit",
            str(item.ref.number),
            "--repo",
            f"{item.ref.owner}/{item.ref.repo}",
            "--remove-label",
            ",".join(item.labels),
        )
    return len(drift)


def body_only_parents(
    org: str,
    *,
    stops: trust.BatchStops,
    authors: TrustedAuthors,
    issues: list[OpenIssue] | None = None,
) -> BodyParents:
    """The issues whose only link is a body ``Parent:`` line naming an epic, judged.

    A ``Parent:`` line counts only from a body the trust layer trusts (spec
    §5.2): each candidate (no native parent, and a listed body whose line
    names an epic) is read again through the layer, and its pair is taken
    from the trusted body. An untrusted body is reported on stderr, metadata
    only, and gives no pair, so the issue is an orphan; a body the layer
    cannot read in full is a stop on that issue alone, added to *stops*, and
    the issue is held.
    """
    if issues is None:
        issues = open_issues(org)
    pairs: list[tuple[epics.IssueRef, epics.IssueRef]] = []
    held: set[epics.IssueRef] = set()
    skipped: list[trust.Skipped] = []
    for issue in issues:
        if issue.native_parent is not None:
            continue
        declared = issue.body_parent
        if declared is None or not epics.is_epic(declared):
            continue
        ref = issue.ref
        try:
            content = trust.read_issue(_slug(ref), ref.number, authors=authors)
        except trust.TrustReadError as exc:
            _read_failure(stops, _slug(ref), ref.number, "issue", exc)
            held.add(ref)
            continue
        skipped += [item for item in content.skipped if item.field == _BODY_FIELD]
        parent = _declared_parent(content.body or "")
        if parent is None or (parent != declared and not epics.is_epic(parent)):
            continue
        pairs.append((ref, parent))
    trust.report_skipped(skipped, stream=sys.stderr)
    return BodyParents(pairs=tuple(pairs), held=frozenset(held))


def unlabelled_advisory_lines(
    org: str,
    *,
    stops: trust.BatchStops,
    authors: TrustedAuthors,
    issues: list[OpenIssue] | None = None,
) -> list[epics.IssueRef]:
    """The open issues with a ``Security advisory:`` line but no ``security`` label.

    Each is a broken pair (spec 6.3 of epic vergil-project/.github#1331),
    reported whether or not an advisory tracks it. The listed body only picks
    the candidates; a line counts only from a body the trust layer trusts, so
    each candidate is read again through the layer. An untrusted body is
    reported on stderr, metadata only, and is not a broken pair; a body the
    layer cannot read in full is a stop on that issue alone, added to *stops*.
    The result is issue refs only, never the lines' GHSA IDs.
    """
    if issues is None:
        issues = open_issues(org)
    found: list[epics.IssueRef] = []
    skipped: list[trust.Skipped] = []
    for issue in issues:
        if security_records.SECURITY_LABEL in issue.labels:
            continue
        if not security_records.parse_advisory_refs(issue.body):
            continue
        ref = issue.ref
        try:
            content = trust.read_issue(_slug(ref), ref.number, authors=authors)
        except trust.TrustReadError as exc:
            _read_failure(stops, _slug(ref), ref.number, "issue", exc)
            continue
        skipped += [item for item in content.skipped if item.field == _BODY_FIELD]
        if content.body is not None and security_records.parse_advisory_refs(content.body):
            found.append(ref)
    trust.report_skipped(skipped, stream=sys.stderr)
    return found


def nativize_parents(pairs: list[tuple[epics.IssueRef, epics.IssueRef]]) -> LinkPass:
    """Link each body-declared task under its epic natively.

    A task whose epic is closed is not linked and the epic is not reopened: it
    is a refused line in the result naming the task and the epic, and the
    other tasks are still linked (closed is closed, epic
    vergil-project/.github#1609).
    """
    linked = 0
    refused: list[str] = []
    for task, epic in pairs:
        try:
            epics.add_child(epic, task)
        except epics.ClosedEpicError as exc:
            refused.append(_closed_refusal(task, exc))
            continue
        linked += 1
    return LinkPass(linked=linked, refused=tuple(refused))


@dataclass(frozen=True)
class MisplacedArchive:
    """An open archive in the wrong shape: parented, unlabelled or unstamped."""

    ref: epics.IssueRef
    problem: str


def misplaced_archives(
    org: str, *, issues: list[OpenIssue] | None = None
) -> list[MisplacedArchive]:
    """Open archives that are parented, unlabelled or unstamped (report only).

    An archive (:func:`epics.is_archive`, by label or stamped title) is a
    top-level container: nothing ever parents one, and one under a live queue
    epic is counted there as a queue item (vergil-tooling#3817). A stamped
    archive title without the label is the shape a partial legacy normalize
    left, which the drain can no longer find or close. An ``archive`` label on
    a title that is not stamped is the shape a normalize leaves when its
    second edit drops ``epic`` but fails to retitle: no normalize path, legacy
    lookup or drain listing finds it again. Tooling never repairs any of
    these: a human unlinks, relabels or retitles the archive.
    """
    if issues is None:
        issues = open_issues(org)
    found: list[MisplacedArchive] = []
    for issue in issues:
        if not epics.is_archive(issue.labels, issue.title):
            continue
        problems: list[str] = []
        parent = issue.native_parent
        if parent is not None:
            kind = epics.queue_kind(parent)
            where = f"the {kind} queue epic {parent.slug}" if kind else parent.slug
            problems.append(f"parented under {where}")
        if "archive" not in issue.labels:
            problems.append("missing the `archive` label")
        elif not epics.is_stamped_archive_title(issue.title):
            problems.append("title is not a stamped archive title")
        if problems:
            found.append(MisplacedArchive(ref=issue.ref, problem="; ".join(problems)))
    return found


# -- Misplaced ad-hoc tasks (epic vergil-project/.github#1417, spec §4.8) ------

STANDALONE_CONFIRMED = "Standalone-confirmed"
_LOGBOOK_PREFIX = "Logbook:"
# An issue a task body names: ``owner/repo#N`` or a github.com issue URL. The
# lookbehind keeps a slug from starting inside a path or a longer word.
_BODY_ISSUE_RE = re.compile(
    r"(?<![\w./-])([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)#([0-9]+)\b"
    r"|https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/issues/([0-9]+)\b"
)


@dataclass(frozen=True)
class MisplacedTask:
    """An open task under a live ad-hoc epic that looks misplaced, and why."""

    task: epics.IssueRef
    reason: str


def _cross_org_refs(body: str, org: str) -> list[epics.IssueRef]:
    """The issues *body* names outside *org*, in first-mention order, without repeats."""
    found: dict[str, epics.IssueRef] = {}
    for match in _BODY_ISSUE_RE.finditer(body):
        groups = match.groups()
        owner, repo, number = groups[:3] if groups[0] is not None else groups[3:]
        if owner.lower() == org.lower():
            continue
        ref = epics.IssueRef(owner, repo, int(number))
        found.setdefault(ref.slug.lower(), ref)
    return list(found.values())


def _standalone_confirmed(task: epics.IssueRef, authors: TrustedAuthors) -> bool:
    """True iff a trusted comment on *task* carries ``Standalone-confirmed`` alone on its line.

    The comments are read through the trust layer
    (:func:`~vergil_tooling.lib.trusted_comments.bodies`): each one it leaves
    out is reported on stderr, metadata only.
    """
    with github.target_org(task.owner):
        bodies = trusted_comments.bodies(task, authors)
    return any(
        line.strip() == STANDALONE_CONFIRMED for body in bodies for line in body.splitlines()
    )


def misplaced_adhoc(org: str, *, issues: list[OpenIssue] | None = None) -> list[MisplacedTask]:
    """Open tasks under a live ad-hoc epic that look misplaced (report only, spec §4.8).

    A task is a candidate when its title starts ``Logbook:`` (it belongs under
    the hoshin epic) or its body names an ``epic``-labelled issue in another
    org (it belongs under a proxy). A cross-org ref whose labels cannot be
    read (deleted, or in an org the App cannot reach) is a candidate of its
    own, never raised and never skipped. A candidate is dropped when a trusted
    comment on it carries ``Standalone-confirmed`` alone on its line: the
    human ruled it standalone. Nothing is refused, moved, closed or labelled.

    It reads the trusted authors only once a candidate is found. *issues* is
    the shared :func:`open_issues` enumeration.
    """
    if issues is None:
        issues = open_issues(org)
    live_adhoc = {
        issue.ref for issue in issues if epics.standing_kind_of(set(issue.labels)) is epics.ADHOC
    }
    epic_label: dict[str, bool | None] = {}  # cross-org ref -> is an epic; None if unreadable
    authors: TrustedAuthors | None = None
    found: list[MisplacedTask] = []
    for issue in sorted(issues, key=lambda i: (i.ref.owner, i.ref.repo, i.ref.number)):
        if issue.native_parent not in live_adhoc:
            continue
        reasons: list[str] = []
        if issue.title.startswith(_LOGBOOK_PREFIX):
            reasons.append(f"titled `{_LOGBOOK_PREFIX}`; it belongs under the hoshin epic")
        for ref in _cross_org_refs(issue.body, issue.ref.owner):
            key = ref.slug.lower()
            if key not in epic_label:
                try:
                    with github.target_org(ref.owner):
                        epic_label[key] = epics.is_epic(ref)
                except _LINK_READ_ERRORS:
                    epic_label[key] = None
            if epic_label[key] is None:
                reasons.append(f"names {ref.slug}, which could not be read")
            elif epic_label[key]:
                reasons.append(f"names {ref.slug}, an epic in another org")
        if not reasons:
            continue
        if authors is None:
            authors = load_trusted_authors()
        if _standalone_confirmed(issue.ref, authors):
            continue
        found += [MisplacedTask(task=issue.ref, reason=reason) for reason in reasons]
    return found


# -- Proxy children and the link check (epic vergil-project/.github#1417) ------


def _same_issue(a: epics.IssueRef, b: epics.IssueRef) -> bool:
    """True iff *a* and *b* name one issue (GitHub owner and repo names ignore case)."""
    return a.slug.lower() == b.slug.lower()


def epic_children(epic: epics.IssueRef) -> list[epics.ChildState]:
    """*epic*'s own children, then the children of every proxy its trusted comments name.

    Each proxy is read under its own org's token, and its ``Primary:`` line must
    name *epic*: a proxy whose link is broken or disagrees raises
    :class:`proxies.ProxyLinkError`, never a silently shorter list. A failed
    read, or a failed trusted-author load, propagates, and so does a proxy
    body the trust layer does not trust (its ``Primary:`` line counts for
    nothing: :class:`~vergil_tooling.lib.trust.UntrustedContentError`) or
    cannot read in full (:class:`~vergil_tooling.lib.trust.TrustReadError`).
    """
    return _walk_epic_and_proxies(epic, epics.child_states)


@dataclass(frozen=True)
class ListedChild:
    """A child of :func:`epic_children_with_blockers`, with the issues still blocking it.

    ``open_blockers`` holds every issue not closed that the child is blocked
    by, through a native blocked-by relationship or a trusted ``Blocked-by:``
    body line, deduplicated and sorted. A closed child's blockers are not
    read, so it holds none.
    """

    child: epics.ChildState
    open_blockers: tuple[epics.IssueRef, ...] = ()


def epic_children_with_blockers(epic: epics.IssueRef) -> list[ListedChild]:
    """:func:`epic_children`, with each open child's open blockers read too (#5124).

    The children are listed as :func:`epic_children` lists them, proxies
    included and their links checked the same way, each epic's children and
    their blockers read under that epic's org token. An open child's native
    blocked-by list comes from the listing
    (:func:`epics.child_states_with_blockers`), or from its own read
    (:func:`epics.native_blockers_of`) for a reflink-fallback child; its
    ``Blocked-by:`` lines from its body (:func:`epics.blockers_of`). Every
    read fails loudly, never "no blockers": a native list that cannot be read
    in full raises :class:`epics.NativeBlockersReadError`, a body the trust
    layer does not trust raises
    :class:`~vergil_tooling.lib.trust.UntrustedContentError`, one it cannot
    read in full :class:`~vergil_tooling.lib.trust.TrustReadError`, and a
    blocker whose state cannot be read raises the read's error.
    """

    def listed(of: epics.IssueRef) -> list[ListedChild]:
        return [
            ListedChild(child, _open_blockers(child) if _is_open(child) else ())
            for child in epics.child_states_with_blockers(of, checked=_is_open)
        ]

    return _walk_epic_and_proxies(epic, listed)


def _open_blockers(child: epics.ChildState) -> tuple[epics.IssueRef, ...]:
    """Every issue not closed that open *child* is blocked by, native and body alike."""
    native = (
        child.native_blockers
        if child.native_blockers is not None
        else epics.native_blockers_of(child.ref)
    )
    states = {blocker.ref: blocker.state for blocker in native}
    for ref in epics.blockers_of(child.ref):
        if ref not in states:
            states[ref] = epics.issue_state(ref)
    return tuple(
        sorted(
            (ref for ref, state in states.items() if state != "CLOSED"),
            key=lambda r: (r.owner, r.repo, r.number),
        )
    )


def _walk_epic_and_proxies[T](
    epic: epics.IssueRef, list_children: Callable[[epics.IssueRef], list[T]]
) -> list[T]:
    """*list_children* of *epic*, then of every proxy its trusted comments name.

    Each list is read under its epic's org token, and each proxy's
    ``Primary:`` line must name *epic* (:func:`epic_children` says what
    raises).
    """
    with github.target_org(epic.owner):
        children = list(list_children(epic))
        listed = proxies.proxies_of(epic)
    for proxy in listed:
        with github.target_org(proxy.owner):
            primary = proxies.primary_of(proxy)
            if not _same_issue(primary, epic):
                raise proxies.ProxyLinkError(
                    f"{epic.slug} lists the proxy {proxy.slug}, whose 'Primary:' line names"
                    f" {primary.slug}"
                )
            children += list_children(proxy)
    return children


# A read that fails while checking a link is a link problem, reported, never skipped.
_LINK_READ_ERRORS = (github.GitHubAPIError, github.NoInstallationError)


def _proxy_side_problems(proxy: epics.IssueRef) -> list[str]:
    """What is wrong with *proxy*'s link: its ``Primary:`` line, or the primary's comment."""
    try:
        with github.target_org(proxy.owner):
            primary = proxies.primary_of(proxy)
        with github.target_org(primary.owner):
            listed = proxies.proxies_of(primary)
    except proxies.ProxyLinkError as exc:
        return [str(exc)]
    except _LINK_READ_ERRORS as exc:
        return [f"the link of proxy {proxy.slug} could not be read: {exc}"]
    if any(_same_issue(ref, proxy) for ref in listed):
        return []
    return [proxies.unlisted_message(proxy, primary)]


def _primary_side_problems(primary: epics.IssueRef) -> list[str]:
    """What is wrong with each proxy that *primary*'s trusted comments name."""
    try:
        with github.target_org(primary.owner):
            listed = proxies.proxies_of(primary)
    except proxies.ProxyLinkError as exc:
        return [str(exc)]
    problems: list[str] = []
    for proxy in listed:
        try:
            with github.target_org(proxy.owner):
                if not proxies.is_proxy_ref(proxy):
                    problems.append(
                        f"{primary.slug} lists {proxy.slug} as a proxy, which is not a proxy"
                        f" epic (it lacks the 'epic' + '{epics.PROXY_LABEL}' labels)"
                    )
                    continue
                named = proxies.primary_of(proxy)
        except proxies.ProxyLinkError as exc:
            problems.append(str(exc))
            continue
        except _LINK_READ_ERRORS as exc:
            problems.append(
                f"{primary.slug} lists the proxy {proxy.slug}, which could not be read: {exc}"
            )
            continue
        if not _same_issue(named, primary):
            problems.append(
                f"{primary.slug} lists the proxy {proxy.slug}, whose 'Primary:' line names"
                f" {named.slug}"
            )
    return problems


def proxy_link_problems(epic: epics.IssueRef) -> list[str]:
    """Every problem with *epic*'s ``Primary:``/``Proxy:`` links; ``[]`` when they agree.

    For a primary (any epic that is not a proxy): each proxy its trusted
    comments name must exist, be a proxy, and carry a ``Primary:`` line naming
    *epic*. For a proxy: its ``Primary:`` line must resolve to an epic whose
    trusted comments name the proxy. A link that cannot be parsed or read is a
    problem, never a skipped item. A failed read of *epic* itself, or a failed
    trusted-author load, propagates, as does a proxy body the trust layer does
    not trust or cannot read in full (:func:`proxies.primary_of`).
    """
    with github.target_org(epic.owner):
        is_proxy = proxies.is_proxy_ref(epic)
    return _proxy_side_problems(epic) if is_proxy else _primary_side_problems(epic)


def _is_open_proxy(record: epic_docs.EpicRecord) -> bool:
    """True iff *record* is an open proxy epic, and never a continuation.

    A continuation carries no kind label of its own (epic
    vergil-project/.github#1609, spec §4.1): its kind is its head's, so one
    that carries ``proxy`` by mistake is still a container of a chain, never
    a proxy whose link is checked or closed.
    """
    labels = set(record["labels"])
    return (
        record["state"] == "OPEN" and epics.is_proxy(labels) and not epics.is_continuation(labels)
    )


def proxy_link_drift(
    home: str, *, stops: trust.BatchStops, listing: list[epic_docs.EpicRecord] | None = None
) -> list[str]:
    """The link problems of every open proxy in *home* (report only).

    *listing* is the home's :func:`list_home_epics`, shared with the other
    per-epic checks; it is read here when not given. A proxy whose body the
    trust layer does not trust (so its ``Primary:`` line counts for nothing),
    or cannot read in full, is a stop on that proxy alone, added to *stops*.
    """
    home_owner, home_repo = home.split("/", 1)
    if listing is None:
        listing = list_home_epics(home)
    problems: list[str] = []
    for record in sorted(listing, key=lambda r: r["number"]):
        if not _is_open_proxy(record):
            continue
        proxy = epics.IssueRef(home_owner, home_repo, record["number"])
        try:
            problems += _proxy_side_problems(proxy)
        except trust.UntrustedContentError as exc:
            _stop(stops, exc)
        except trust.TrustReadError as exc:
            _read_failure(stops, _slug(proxy), proxy.number, "issue", exc)
    return problems


# -- The sweep's proxy close pass (epic vergil-project/.github#1417) -----------


@dataclass(frozen=True)
class ProxyClosure:
    """An open proxy whose primary has closed, and how many of its tasks are open."""

    proxy: epics.IssueRef
    primary: epics.IssueRef
    open_tasks: int


def proxy_closures(
    home: str,
    *,
    stops: trust.BatchStops,
    authors: TrustedAuthors,
    listing: list[epic_docs.EpicRecord] | None = None,
) -> tuple[list[ProxyClosure], list[str]]:
    """Each open proxy in *home* whose primary is closed, and the proxies that could not be judged.

    Returns ``(closures, proxy_errors)``. A proxy lives as long as its primary,
    so only a closed primary makes it a candidate; its open tasks are counted
    so :func:`close_proxies` closes only an empty one. A proxy whose
    ``Primary:`` link fails (malformed, not an epic, or unreadable) is one line
    of *proxy_errors*, never raised, so one broken proxy cannot stop the
    sweep's other passes, as :func:`epics.drain_standing_org` treats a
    corrupted home; the caller fails the run once every pass has run.

    The ``Primary:`` line counts only from a body the trust layer trusts, and
    the primary's close only when its actor holds a role in the primary's org
    (spec §5.2): its own org's automation App is trusted there, read from any
    org with the delivered configuration. A proxy whose body or primary's
    close is untrusted, or cannot be read in full, is a stop on that proxy
    alone, added to *stops*, and is never closed.

    *listing* is the home's :func:`list_home_epics`; it is read here when not
    given.
    """
    home_owner, home_repo = home.split("/", 1)
    if listing is None:
        listing = list_home_epics(home)
    closures: list[ProxyClosure] = []
    errors: list[str] = []
    for record in sorted(listing, key=lambda r: r["number"]):
        if not _is_open_proxy(record):
            continue
        proxy = epics.IssueRef(home_owner, home_repo, record["number"])
        try:
            with github.target_org(proxy.owner):
                primary = proxies.primary_of(proxy)
            with github.target_org(primary.owner):
                primary_state = epics.issue_state(primary)
                if primary_state != "CLOSED":
                    continue
                trust.require_trusted_close(_slug(primary), primary.number, authors=authors)
            with github.target_org(proxy.owner):
                open_tasks = sum(1 for c in epics.child_states(proxy) if c.state == "OPEN")
        except proxies.ProxyLinkError as exc:
            errors.append(str(exc))
            continue
        except _LINK_READ_ERRORS as exc:
            errors.append(f"proxy {proxy.slug} could not be judged: {exc}")
            continue
        except trust.UntrustedContentError as exc:
            _stop(stops, exc)
            continue
        except trust.TrustReadError as exc:
            _read_failure(stops, _slug(proxy), proxy.number, "issue", exc, "primary")
            continue
        closures.append(ProxyClosure(proxy=proxy, primary=primary, open_tasks=open_tasks))
    return closures, errors


_PROXY_CLOSE_COMMENT = (
    "Closed by `vrg-epic-audit`: its primary epic {primary} is closed and no task under"
    " this proxy is open."
)


def proxy_drift_line(closure: ProxyClosure) -> str:
    """The report line for a proxy left open because a task under it is still open."""
    return (
        f"{closure.proxy.slug} — its primary {closure.primary.slug} is closed, but"
        f" {closure.open_tasks} task(s) under it are still open; left open"
    )


def close_proxies(closures: list[ProxyClosure]) -> list[str]:
    """Close each proxy of *closures* with no open task; return the rest as drift lines.

    A proxy with an open task after its primary closed is never closed
    silently: it stays open and its line is returned for the report. Each
    close leaves a comment and runs under the proxy's org token. This performs
    outward-effecting GitHub writes; the caller gates it to a human or the sweep.
    """
    drift: list[str] = []
    for closure in closures:
        if closure.open_tasks:
            drift.append(proxy_drift_line(closure))
            continue
        proxy = closure.proxy
        with github.target_org(proxy.owner):
            github.run(
                "issue",
                "close",
                str(proxy.number),
                "--repo",
                f"{proxy.owner}/{proxy.repo}",
                "--comment",
                _PROXY_CLOSE_COMMENT.format(primary=closure.primary.slug),
            )
    return drift


# -- Chain drift (epic vergil-project/.github#1609, spec §4.7) ------------------

#: The kinds of :class:`ChainDrift`. Every kind but ``closed-continuation`` is
#: drift; that one is a note: a continuation closed by hand in an open chain
#: never blocks the chain (spec §4.2).
CHAIN_DRIFT_KINDS = (
    "drift",
    "full-tail",
    "duplicate",
    "closed-head-open-continuation",
    "orphan",
    "closed-continuation",
)


@dataclass(frozen=True)
class ChainDrift:
    """One problem with an epic's chain of containers, naming the containers.

    *kind* is one of :data:`CHAIN_DRIFT_KINDS`. For ``drift`` and
    ``duplicate``, *rule* is the :class:`~vergil_tooling.lib.epic_chain.ChainDriftError`
    rule the first of *containers* broke (``two-continuations`` for a
    duplicate); every other kind has no rule (``""``). *head* is the epic the
    chain belongs to, or, for a continuation whose head cannot be resolved
    past the drift, that continuation itself.
    """

    head: epics.IssueRef
    kind: str
    containers: tuple[epics.IssueRef, ...]
    rule: str = ""


def chain_drift(
    home: str, *, listing: list[epic_docs.EpicRecord] | None = None
) -> list[ChainDrift]:
    """Every problem with the chains of *home*'s epics (report only; spec §4.7).

    Each epic of *home* that is no continuation, open or closed, has its
    chain walked (:func:`epic_chain.containers`). A walk that breaks a rule
    of spec §4.1 is ``drift`` (``duplicate`` for two continuations under one
    container). A closed head with an open continuation, as an interrupted
    :func:`epic_chain.close_chain` leaves, is ``closed-head-open-continuation``;
    a closed continuation in an open chain is the note ``closed-continuation``.
    A tail holding GitHub's 100 sub-issues and no continuation is
    ``full-tail``, reported only while rollover is on: while it is off a full
    head is today's limit, which every link refuses by name.

    Then every open issue of *home* labelled ``continuation``
    (:func:`epic_chain.open_continuations`, which lists one that lost its
    ``epic`` label too) that no walk reached is resolved to its head
    (:func:`epic_chain.head_of`): one whose head's chain is whole is an
    ``orphan``, of an open or a closed head; one whose head's walk already
    broke is that drift, reported once; one that cannot be resolved, or
    whose head is no epic of *home*, is ``drift``.

    *listing* is :func:`list_home_epics`, read here when not given. The
    reads run under *home*'s owner's token. Nothing is written: the sweep
    heals from this list.
    """
    owner, repo = home.split("/", 1)
    found: list[ChainDrift] = []
    walked: set[epics.IssueRef] = set()
    broken: set[epics.IssueRef] = set()
    reached: set[epics.IssueRef] = set()

    def walk(head: epics.IssueRef) -> None:
        walked.add(head)
        try:
            chain = epic_chain.containers(head)
        except epic_chain.ChainDriftError as exc:
            broken.add(head)
            reached.add(exc.container)  # accounted for: reported here, once
            kind = "duplicate" if exc.rule == "two-continuations" else "drift"
            found.append(ChainDrift(head, kind, (exc.container,), exc.rule))
            return
        reached.update(container.ref for container in chain)
        found.extend(_chain_problems(head, chain))

    with github.target_org(owner):
        if listing is None:
            listing = list_home_epics(home)
        ordered = sorted(listing, key=lambda r: r["number"])
        for record in ordered:
            if not epics.is_continuation(set(record["labels"])):
                walk(epics.IssueRef(owner, repo, record["number"]))
        for continuation, _ in epic_chain.open_continuations(home):
            if continuation in reached:
                continue
            try:
                head = epic_chain.head_of(continuation)
            except epic_chain.ChainDriftError as exc:
                found.append(ChainDrift(continuation, "drift", (exc.container,), exc.rule))
                continue
            if head not in walked:
                # Its trusted Continues: line names no epic of this home.
                found.append(
                    ChainDrift(continuation, "drift", (continuation,), "continues-mismatch")
                )
                continue
            if head in broken or continuation in reached:
                continue
            found.append(ChainDrift(head, "orphan", (continuation,)))
    return found


def _chain_problems(head: epics.IssueRef, chain: list[epic_chain.Container]) -> list[ChainDrift]:
    """The problems of *head*'s whole *chain*, as :func:`chain_drift` reports them."""
    rest = chain[1:]
    if chain[0].state == "CLOSED":
        still_open = tuple(c.ref for c in rest if c.state == "OPEN")
        if still_open:
            return [ChainDrift(head, "closed-head-open-continuation", still_open)]
        return []
    problems = [
        ChainDrift(head, "closed-continuation", (c.ref,)) for c in rest if c.state == "CLOSED"
    ]
    tail = chain[-1]
    if epic_chain.ROLLOVER_ENABLED and tail.item_count >= epic_chain.SUBISSUE_LIMIT:
        problems.append(ChainDrift(head, "full-tail", (tail.ref,)))
    return problems


def chain_drift_line(drift: ChainDrift) -> str:
    """The report line for one :class:`ChainDrift`, naming its head and containers."""
    names = ", ".join(ref.slug for ref in drift.containers)
    if drift.kind == "drift":
        if drift.containers == (drift.head,):
            return f"{names} breaks the {drift.rule} rule"
        return f"epic {drift.head.slug}: {names} breaks the {drift.rule} rule"
    if drift.kind == "duplicate":
        return f"epic {drift.head.slug}: {names} holds two continuations or more"
    if drift.kind == "full-tail":
        return (
            f"epic {drift.head.slug}: its tail {names} holds {epic_chain.SUBISSUE_LIMIT}"
            " sub-issues and no continuation"
        )
    if drift.kind == "closed-head-open-continuation":
        return f"epic {drift.head.slug} is closed, but its continuation(s) {names} are open"
    if drift.kind == "orphan":
        return (
            f"epic {drift.head.slug}: {names} is an orphaned continuation, open and not"
            " reached from the head"
        )
    if drift.kind == "closed-continuation":
        return (
            f"epic {drift.head.slug}: its continuation {names} is closed; its items are still"
            " read, and nothing is linked into it"
        )
    raise ValueError(f"unknown chain drift kind {drift.kind!r}")


def render_epic_children(
    epic: epics.IssueRef,
    children: Sequence[epics.ChildState | ListedChild],
    *,
    containers: int,
    primary: epics.IssueRef | None = None,
) -> str:
    """Machine-readable JSON enumeration of *epic*'s children with their state.

    Emits a stable, trivially-parseable object — the epic slug plus a ``children``
    array whose entries each carry ``repo`` (``owner/name``), ``number``,
    ``title``, and ``state`` (``"OPEN"``/``"CLOSED"``). This is the sanctioned
    replacement for the brittle ``gh issue view <epic> --json subIssues --jq`` path
    that failed with ``expected an object but got: array`` (issue #2538): callers
    filter ``.children[] | select(.state=="OPEN")`` over a proper array of objects
    instead of GitHub's raw sub-issue shape, and get each child's repo/number/title
    without a second per-issue lookup. *children* is the output of
    :func:`epics.child_states` (native sub-issues, reflink fallback), so the native
    vs portable mechanism is transparent to the caller. Entries are sorted by
    (owner, repo, number) so the output is deterministic across runs.

    *containers* is the number of containers in *epic*'s chain, the head
    included (1 for an epic with no continuation; epic
    vergil-project/.github#1609): the payload's ``containers`` key.

    *primary* is given only for a proxy epic: the payload then gains a
    ``"primary"`` key naming it, which merge review reads. Any other epic's
    payload has no such key.

    A :class:`ListedChild` entry (:func:`epic_children_with_blockers`) also
    carries ``open_blockers``, the ``owner/repo#N`` refs of the issues not
    closed that it is blocked by (native and ``Blocked-by:`` alike, sorted),
    and ``blocked``, true iff that list is not empty (#5124). A closed
    child's list is always empty.
    """
    entries: list[tuple[epics.ChildState, tuple[epics.IssueRef, ...] | None]] = [
        (c.child, c.open_blockers) if isinstance(c, ListedChild) else (c, None) for c in children
    ]
    entries.sort(key=lambda e: (e[0].ref.owner, e[0].ref.repo, e[0].ref.number))
    payload: dict[str, object] = {"epic": epic.slug}
    if primary is not None:
        payload["primary"] = primary.slug
    payload["containers"] = containers
    rendered: list[dict[str, object]] = []
    for child, blockers in entries:
        entry: dict[str, object] = {
            "repo": f"{child.ref.owner}/{child.ref.repo}",
            "number": child.ref.number,
            "title": child.title,
            "state": child.state,
        }
        if blockers is not None:
            entry["open_blockers"] = [ref.slug for ref in blockers]
            entry["blocked"] = bool(blockers)
        rendered.append(entry)
    payload["children"] = rendered
    return json.dumps(payload, indent=2)


def render(
    tasks: list[TaskDrift],
    epic_summaries: list[roadmap.EpicSummary],
    *,
    org: str,
    window_days: int,
    epics_outside: list[str] | None = None,
    stray: list[str] | None = None,
    pending_operational: list[OperationalStatus] | None = None,
    closed_operational_no_success: list[str] | None = None,
    closed_epic_open_children: list[EpicOpenChildViolation] | None = None,
    orphans: list[Orphan] | None = None,
    retired_labels: list[RetiredLabels] | None = None,
    body_only: list[tuple[epics.IssueRef, epics.IssueRef]] | None = None,
    misplaced: list[MisplacedArchive] | None = None,
    epic_docs_findings: list[EpicDocsFinding] | None = None,
    proxy_link_drift: list[str] | None = None,
    proxy_closures: list[ProxyClosure] | None = None,
    proxy_errors: list[str] | None = None,
    misplaced_adhoc: list[MisplacedTask] | None = None,
    chain_drift: list[ChainDrift] | None = None,
) -> str:
    """Format the drift + invariant report; a clean state says so explicitly.

    The orphan pass (epic .github#176) adds ``orphans``, ``retired_labels``
    and ``body_only`` (what ``--close`` would apply). ``misplaced`` is the
    report-only :func:`misplaced_archives` invariant (vergil-tooling#3817).

    The report opens with a banner naming the audited org and window and
    stating the run is read-only, so the output is never mistaken for a list of
    actions the tool took. ``epics_outside`` and ``stray`` are the invariant
    violations (epic #85). ``pending_operational`` lists epics still gated on
    operational tasks (validations/deployments, runnable vs blocked);
    ``closed_operational_no_success`` is the operational invariant (a closed
    operational task with no recorded success comment). ``closed_epic_open_children``
    is the closed-epic-with-open-child invariant (issue #2259).
    ``epic_docs_findings`` are the :func:`epic_documents` problems (epic
    .github#819). ``proxy_link_drift`` is the report-only :func:`proxy_link_drift`
    check (epic .github#1417); ``proxy_closures`` and ``proxy_errors`` preview
    the ``--close`` proxy close pass (:func:`proxy_closures`), and
    ``misplaced_adhoc`` is the report-only :func:`misplaced_adhoc` check.
    ``chain_drift`` is the report-only :func:`chain_drift` check (epic
    .github#1609): its drift in one section, its closed-continuation notes in
    another. Each section appears only when it has something to report.
    """
    epics_outside = epics_outside or []
    stray = stray or []
    pending_operational = pending_operational or []
    closed_operational_no_success = closed_operational_no_success or []
    closed_epic_open_children = closed_epic_open_children or []
    orphans = orphans or []
    retired_labels = retired_labels or []
    body_only = body_only or []
    misplaced = misplaced or []
    epic_docs_findings = epic_docs_findings or []
    proxy_link_drift = proxy_link_drift or []
    proxy_closures = proxy_closures or []
    proxy_errors = proxy_errors or []
    misplaced_adhoc = misplaced_adhoc or []
    chain_drift = chain_drift or []
    banner = (
        f"_Read-only audit of the **{org}** org (merged PRs from the last "
        f"{window_days} days) — this report changes nothing; run `--close` (as "
        "a human) to close what it lists._"
    )
    header = ["# Epic/task drift audit", "", banner, ""]
    if not any(
        [
            tasks,
            epic_summaries,
            epics_outside,
            stray,
            pending_operational,
            closed_operational_no_success,
            closed_epic_open_children,
            orphans,
            retired_labels,
            body_only,
            misplaced,
            epic_docs_findings,
            proxy_link_drift,
            proxy_closures,
            proxy_errors,
            misplaced_adhoc,
            chain_drift,
        ]
    ):
        return "\n".join([*header, "_No drift — everything that should be closed is closed._", ""])
    lines = [*header, "## Task drift (merged PR, task still open)", ""]
    if tasks:
        for task in sorted(tasks, key=lambda t: (t.repo, t.task)):
            lines.append(
                f"- {task.repo}#{task.task} — open; PR [#{task.pr_number}]({task.pr_url}) merged"
            )
    else:
        lines.append("- _none_")
    lines += ["", "## Epic drift (all children closed, epic still open)", ""]
    if epic_summaries:
        for epic in sorted(epic_summaries, key=lambda e: e.number):
            lines.append(
                f"- [#{epic.number}]({epic.url}) {epic.title} — {epic.closed}/{epic.total} done"
            )
    else:
        lines.append("- _none_")
    if pending_operational:
        lines += ["", "## Operational tasks pending (epic not done until they run)", ""]
        for status in sorted(pending_operational, key=lambda s: s.epic.number):
            runnable = (
                ", ".join(f"{ref.slug} ({status.by_kind.get(ref, '?')})" for ref in status.runnable)
                or "none"
            )
            blocked = (
                ", ".join(f"{ref.slug} ({status.by_kind.get(ref, '?')})" for ref in status.blocked)
                or "none"
            )
            line = f"- {status.epic.slug} — runnable: {runnable}; blocked: {blocked}"
            if status.stopped:
                # A count only: under GitHub Actions an untrusted-content stop is
                # never named (spec §5.3); stderr names each one where it may.
                line += f"; stopped: {len(status.stopped)} (see stderr)"
            lines.append(line)
    lines += _render_proxy_closures(proxy_closures)
    lines += _render_proxy_errors(proxy_errors)
    lines += _render_orphan_pass(orphans, retired_labels, body_only)
    if (
        epics_outside
        or stray
        or closed_operational_no_success
        or closed_epic_open_children
        or misplaced
    ):
        lines += ["", "## Invariant violations (issues in the wrong place)", ""]
        if epics_outside:
            lines.append("**Epics outside `.github`** — move each to the org's `.github`:")
            lines += [f"- {slug}" for slug in sorted(epics_outside)]
        if stray:
            lines.append("**Stray `.github` issues** — not an epic, intake, or linked task:")
            lines += [f"- {slug}" for slug in sorted(stray)]
        if closed_operational_no_success:
            lines.append("**Validation tasks closed without a PASS comment** — re-open and run:")
            lines += [f"- {slug}" for slug in sorted(closed_operational_no_success)]
        lines += _render_closed_epic_open_children(closed_epic_open_children)
        lines += _render_misplaced_archives(misplaced)
    lines += _render_proxy_link_drift(proxy_link_drift)
    lines += _render_misplaced_adhoc(misplaced_adhoc)
    lines += _render_chain_drift(chain_drift)
    lines += _render_epic_documents(epic_docs_findings, f"{org}/.github")
    lines.append("")
    return "\n".join(lines)


def _render_closed_epic_open_children(violations: list[EpicOpenChildViolation]) -> list[str]:
    """The closed-epic-with-open-child lines, for a human; none when clean.

    Closed is closed (epic vergil-project/.github#1609): neither the audit nor
    the sweep reopens the epic, so each is listed for a person to settle.
    """
    if not violations:
        return []
    lines = [
        "**Closed epics with open children** — an epic must not be closed while a "
        "child is open; the tooling never reopens one, so a person settles each "
        "(reopen it by hand, or close or move the open children):"
    ]
    for closed_epic, kids in sorted(violations, key=lambda v: (v[0].owner, v[0].repo, v[0].number)):
        open_kids = ", ".join(
            kid.slug for kid in sorted(kids, key=lambda k: (k.owner, k.repo, k.number))
        )
        lines.append(f"- {closed_epic.slug} — open children: {open_kids}")
    return lines


def _render_epic_documents(findings: list[EpicDocsFinding], home: str) -> list[str]:
    """The ``## Epic documents`` section, one line per problem; none when clean."""
    if not findings:
        return []
    lines = [
        "",
        "## Epic documents",
        "",
        "Settle each with `vrg-epic-docs set` or `vrg-epic-docs backfill`:",
        "",
    ]
    lines += [
        f"- {home}#{f.number} ({f.state.lower()}) — {f.problem}"
        for f in sorted(findings, key=lambda f: f.number)
    ]
    return lines


def _render_proxy_link_drift(problems: list[str]) -> list[str]:
    """The ``## Proxy link drift`` section, one line per problem; none when clean."""
    if not problems:
        return []
    return [
        "",
        "## Proxy link drift",
        "",
        "A proxy and its primary must name each other; fix each link by hand:",
        "",
        *[f"- {problem}" for problem in problems],
    ]


def _render_chain_drift(found: list[ChainDrift]) -> list[str]:
    """The ``## Chain drift`` and ``## Chain notes`` sections; none when clean."""
    drift = [d for d in found if d.kind != "closed-continuation"]
    notes = [d for d in found if d.kind == "closed-continuation"]
    lines: list[str] = []
    if drift:
        lines += [
            "",
            "## Chain drift (report only)",
            "",
            "Each line names an epic whose chain of containers needs attention:",
            "",
            *[f"- {chain_drift_line(d)}" for d in drift],
        ]
    if notes:
        lines += ["", "## Chain notes", "", *[f"- {chain_drift_line(d)}" for d in notes]]
    return lines


def _render_misplaced_adhoc(misplaced: list[MisplacedTask]) -> list[str]:
    """The misplaced ad-hoc task report, one line per reason; none when clean."""
    if not misplaced:
        return []
    return [
        "",
        "## Ad-hoc tasks that look misplaced (report only)",
        "",
        "Move each misplaced task under its proxy or hoshin epic; for a task you rule"
        f" standalone, post a comment with the line `{STANDALONE_CONFIRMED}` alone on its line:",
        "",
        *[f"- {m.task.slug} — {m.reason}" for m in misplaced],
    ]


def _render_proxy_closures(closures: list[ProxyClosure]) -> list[str]:
    """The ``--close`` proxy close pass preview: what it would close and leave open."""
    if not closures:
        return []
    lines = ["", "## Proxies whose primary has closed (`--close` closes the empty ones)", ""]
    for closure in closures:
        if closure.open_tasks:
            lines.append(f"- {proxy_drift_line(closure)}")
        else:
            lines.append(f"- {closure.proxy.slug} — its primary {closure.primary.slug} is closed")
    return lines


def _render_proxy_errors(errors: list[str]) -> list[str]:
    """The ``## Proxy link errors`` section: proxies the close pass could not judge."""
    if not errors:
        return []
    return [
        "",
        "## Proxy link errors (the run fails; fix each link by hand)",
        "",
        *[f"- {error}" for error in errors],
    ]


_MISPLACED_ARCHIVES_HEADING = (
    "**Misplaced archives** — an archive stands top-level with the `archive` "
    "label and a stamped title; fix each one by hand:"
)


def _render_misplaced_archives(misplaced: list[MisplacedArchive]) -> list[str]:
    if not misplaced:
        return []
    ordered = sorted(misplaced, key=lambda m: (m.ref.owner, m.ref.repo, m.ref.number))
    return [_MISPLACED_ARCHIVES_HEADING, *[f"- {m.ref.slug} — {m.problem}" for m in ordered]]


def _render_orphan_pass(
    orphans: list[Orphan],
    retired_labels: list[RetiredLabels],
    body_only: list[tuple[epics.IssueRef, epics.IssueRef]],
) -> list[str]:
    """The ``--close`` orphan-pass preview, one section per non-empty pass, in apply order."""
    lines: list[str] = []
    if body_only:
        lines += ["", "## Body-only Parent: links (`--close` links them natively)", ""]
        lines += [f"- {task.slug} → {epic.slug}" for task, epic in body_only]
    if retired_labels:
        lines += ["", "## Retired kind labels (`--close` removes them)", ""]
        lines += [f"- {item.ref.slug} — {', '.join(item.labels)}" for item in retired_labels]
    if orphans:
        lines += ["", "## Orphans (`--close` attaches each to its intake epic)", ""]
        lines += [
            f"- {o.ref.slug} → {epics.INTAKE.title_prefix}{o.name} in {o.home}" for o in orphans
        ]
    return lines


_TASK_CLOSE_COMMENT = (
    "Closed by `vrg-epic-audit`: PR #{pr} merged but the tracking task's auto-close did not fire."
)
_EPIC_CLOSE_COMMENT = (
    "Closed by `vrg-epic-audit`: all {total} child tasks are closed; the epic rolled up."
)


def close_drift(
    tasks: list[TaskDrift],
    epics: list[roadmap.EpicSummary],
    *,
    org: str,
) -> list[str]:
    """Close each drifted task and rolled-up epic, leaving a comment on each.

    Epics live in ``{org}/.github``. Returns the ``owner/repo#n`` slugs closed,
    in the order acted on. This performs outward-effecting GitHub writes; the
    caller is responsible for gating it to a human.
    """
    closed: list[str] = []
    for task in sorted(tasks, key=lambda t: (t.repo, t.task)):
        github.run(
            "issue",
            "close",
            str(task.task),
            "--repo",
            task.repo,
            "--comment",
            _TASK_CLOSE_COMMENT.format(pr=task.pr_number),
        )
        closed.append(f"{task.repo}#{task.task}")
    epic_repo = f"{org}/.github"
    for epic in sorted(epics, key=lambda e: e.number):
        github.run(
            "issue",
            "close",
            str(epic.number),
            "--repo",
            epic_repo,
            "--comment",
            _EPIC_CLOSE_COMMENT.format(total=epic.total),
        )
        closed.append(f"{epic_repo}#{epic.number}")
    return closed


def render_closed(
    closed: list[str],
    *,
    org: str,
    window_days: int,
    closed_epic_open_children: list[EpicOpenChildViolation] | None = None,
    misplaced: list[MisplacedArchive] | None = None,
    proxies_closed: list[str] | None = None,
    proxy_drift: list[str] | None = None,
    proxy_errors: list[str] | None = None,
    proxy_link_drift: list[str] | None = None,
    misplaced_adhoc: list[MisplacedTask] | None = None,
) -> str:
    """Summarize a ``--close`` run: what was closed, and what needs a human.

    ``closed_epic_open_children`` (:func:`closed_epic_open_child`) are epics
    closed while a child is still open: report-only, since the tooling never
    reopens an epic (closed is closed). It and ``misplaced`` are never acted
    on, surfaced here so the scheduled sweep's log carries them, as are
    ``proxy_link_drift``
    (:func:`proxy_link_drift`) and ``misplaced_adhoc`` (:func:`misplaced_adhoc`),
    the trusted-comment checks.

    ``proxies_closed`` are the proxies :func:`close_proxies` closed;
    ``proxy_drift`` the ones it left open with an open task, and
    ``proxy_errors`` the ones :func:`proxy_closures` could not judge.
    """
    proxies_closed = proxies_closed or []
    proxy_drift = proxy_drift or []
    proxy_errors = proxy_errors or []
    closed_epic_open_children = closed_epic_open_children or []
    misplaced = misplaced or []
    proxy_link_drift = proxy_link_drift or []
    misplaced_adhoc = misplaced_adhoc or []
    banner = f"_Closed drift on the **{org}** org (merged PRs from the last {window_days} days)._"
    header = ["# Epic/task drift audit — closed", "", banner, ""]
    if not any(
        [
            closed,
            closed_epic_open_children,
            misplaced,
            proxies_closed,
            proxy_drift,
            proxy_errors,
            proxy_link_drift,
            misplaced_adhoc,
        ]
    ):
        return "\n".join([*header, "_No drift — nothing to close._", ""])
    lines = [*header]
    if closed:
        lines += ["## Closed", "", *[f"- {slug}" for slug in closed], ""]
    if proxies_closed:
        lines += [
            "## Proxies closed (primary closed, no open task)",
            "",
            *[f"- {slug}" for slug in proxies_closed],
            "",
        ]
    if any(
        [
            closed_epic_open_children,
            misplaced,
            proxy_drift,
            proxy_errors,
            proxy_link_drift,
            misplaced_adhoc,
        ]
    ):
        lines += ["## Needs a human (report only)", ""]
        lines += _render_closed_epic_open_children(closed_epic_open_children)
        lines += _render_misplaced_archives(misplaced)
        if proxy_drift:
            lines.append("**Proxies left open** — the primary closed with a task still open:")
            lines += [f"- {line}" for line in proxy_drift]
        if proxy_errors:
            lines.append("**Proxy link errors** — the run fails; fix each link by hand:")
            lines += [f"- {error}" for error in proxy_errors]
        if proxy_link_drift:
            lines.append("**Proxy link drift** — fix each link by hand:")
            lines += [f"- {problem}" for problem in proxy_link_drift]
        if misplaced_adhoc:
            lines.append(
                "**Ad-hoc tasks that look misplaced** — move each one, or record"
                f" `{STANDALONE_CONFIRMED}` on it:"
            )
            lines += [f"- {m.task.slug} — {m.reason}" for m in misplaced_adhoc]
        lines.append("")
    return "\n".join(lines)
