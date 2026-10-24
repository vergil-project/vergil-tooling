"""Audit for epic/task drift — work that slipped through auto-close.

``task_drift`` finds merged PRs whose ``Ref``'d task is still open;
``epic_drift`` finds open, non-perpetual epics whose children are all closed
(should have rolled up). ``render`` formats both sections for review;
``close_drift`` closes them with an explanatory comment — a human action, gated
by the caller.

The orphan pass (epic vergil-project/.github#176) enforces *every issue is a
legitimate top-level kind or has an epic parent*: it nativizes body-only
``Parent:`` links, strips the retired ``triage``/``idea``/``research`` labels,
moves a now-public repo's self-homed queue items under ``.github``, attaches
every orphan to its home's intake epic, and reports private-repo work under
public ``.github`` epics.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import epics, github, linkage, release, roadmap
from vergil_tooling.lib.labels import RETIRED_LABELS

# A closed-epic-with-open-child violation (issue #2259): the closed epic paired
# with the tuple of its still-open children.
EpicOpenChildViolation = tuple[epics.IssueRef, tuple[epics.IssueRef, ...]]


@dataclass(frozen=True)
class TaskDrift:
    """A merged PR whose tracking task is still open."""

    repo: str
    task: int
    pr_number: int
    pr_url: str


def task_drift(since: str, *, org: str) -> list[TaskDrift]:
    """Merged PRs (since *since*) whose ``Ref``'d task is still open."""
    raw: Any = github.read_json(
        "search",
        "prs",
        "--owner",
        org,
        # The --merged flag silently matches nothing; the merged:>= query term works.
        f"merged:>={since}",
        "--limit",
        "100",
        "--json",
        "number,repository,url,body",
    )
    drift: list[TaskDrift] = []
    for entry in raw if isinstance(raw, list) else []:
        pr_repo = str((entry.get("repository") or {}).get("nameWithOwner", ""))
        if not pr_repo:
            continue
        try:
            ref = linkage.extract_tracking_ref(str(entry.get("body") or ""))
        except ValueError:
            continue
        if ref is None:
            continue
        # ``ref`` is ``"#N"`` (same-repo) or ``"owner/repo#N"`` (cross-repo).
        # Resolve the repo the task actually lives in — honor a cross-repo ref
        # instead of assuming the PR's own repo (issue #2111).
        repo_part, _, num = ref.rpartition("#")
        task = int(num)
        task_repo = repo_part or pr_repo
        try:
            issue: Any = github.read_json(
                "issue", "view", str(task), "--repo", task_repo, "--json", "state,title,body,labels"
            )
        except github.GitHubAPIError:
            # The task is in a repo this run can't see (cross-org, private, or
            # deleted). Not this org's drift to close — warn and skip rather than
            # abort the whole sweep.
            print(
                f"vrg-epic-audit: skipping {task_repo}#{task} (referenced by "
                f"{pr_repo} PR #{entry.get('number')}) — not found or not "
                "accessible from this run.",
                file=sys.stderr,
            )
            continue
        if not isinstance(issue, dict):
            continue
        if str(issue.get("state") or "").upper() != "OPEN":
            continue
        labels = {str((label or {}).get("name", "")) for label in (issue.get("labels") or [])}
        if "epic" in labels:
            # A PR may legitimately ``Ref`` an epic (associating the PR with the
            # epic). That is not a slipped task: epic closure is owned by
            # epic_drift/rollup, which enforces all_children_closed. Closing an
            # epic here would bypass that guard and orphan its open children
            # (issue #2259, Fix A).
            continue
        if labels & epics.operational_labels():
            # An operational task (validation/deployment) closes on a recorded
            # ``Outcome:`` comment — a merged PR ``Ref``ing it is not the
            # slipped-task drift this sweep closes.
            continue
        # A merged release PR Refs its release: X.Y.Z tracking issue, which stays
        # open as vrg-release bookkeeping — not drift. Skip it so the audit does
        # not flag release issues as slipped tasks. See issue #1984.
        if release.is_release_tracking_issue(
            title=str(issue.get("title") or ""), body=str(issue.get("body") or "")
        ):
            continue
        # An intake or planning queue item is never a PR-tracked task: its parent
        # (the queue epic), not a kind label, marks it (epic .github#176).
        task_owner, task_bare = task_repo.split("/", 1)
        parent = epics.parent_of(epics.IssueRef(task_owner, task_bare, task))
        if parent is not None and epics.queue_kind(parent) is not None:
            continue
        drift.append(
            TaskDrift(
                repo=task_repo, task=task, pr_number=int(entry["number"]), pr_url=str(entry["url"])
            )
        )
    return drift


def epic_drift(org: str | None = None, *, home: str | None = None) -> list[roadmap.EpicSummary]:
    """Open, non-perpetual epics whose children are all closed (should roll up).

    Scoped to the resolved epic *home* (default: the org's ``.github``), so a
    ``--repo``-targeted audit sees only that repo's epics.
    """
    return [
        epic
        for epic in roadmap.gather(org, home=home)
        if epic.total > 0 and epic.closed == epic.total
    ]


@dataclass(frozen=True)
class OperationalStatus:
    """An epic's outstanding (open) operational children, split runnable vs blocked."""

    epic: epics.IssueRef
    runnable: tuple[epics.IssueRef, ...]  # open operational children, all blockers closed
    blocked: tuple[epics.IssueRef, ...]  # open operational children, a blocker still open
    by_kind: dict[epics.IssueRef, str]  # each pending child -> its kind (validation/deployment)

    @property
    def pending(self) -> tuple[epics.IssueRef, ...]:
        """All outstanding operational children (runnable + blocked)."""
        return self.runnable + self.blocked


def operational_status(epic: epics.IssueRef) -> OperationalStatus:
    """Classify *epic*'s open operational children as runnable vs blocked, by kind.

    An operational child (validation, deployment, …) is runnable when every task
    it is ``Blocked-by`` is closed; otherwise it is blocked. This
    runnable-vs-blocked split, tagged by kind, is the honest "what still has to
    run" signal — and the seed of a future automator that runs operational tasks
    as their dependencies land.
    """
    runnable: list[epics.IssueRef] = []
    blocked: list[epics.IssueRef] = []
    by_kind: dict[epics.IssueRef, str] = {}
    for child in epics.child_states(epic):
        if child.state != "OPEN":
            continue
        kind = epics.operational_kind(child.ref)
        if kind is None:
            continue
        by_kind[child.ref] = kind
        target = runnable if epics.all_blockers_closed(child.ref) else blocked
        target.append(child.ref)
    return OperationalStatus(
        epic=epic, runnable=tuple(runnable), blocked=tuple(blocked), by_kind=by_kind
    )


@dataclass(frozen=True)
class ChildStatus:
    """An epic's open children, split runnable vs blocked by their Blocked-by deps."""

    epic: epics.IssueRef
    runnable: tuple[epics.IssueRef, ...]  # open children, all blockers closed
    blocked: tuple[epics.IssueRef, ...]  # open children, a blocker still open


def child_status(epic: epics.IssueRef) -> ChildStatus:
    """Classify *every* open child of *epic* as runnable vs blocked.

    Where :func:`operational_status` is scoped to operational children (its
    runnable/blocked split feeds the "operation-pending" report), this classifies
    *all* open children — plain tasks included — through the already-generic
    :func:`epics.all_blockers_closed`. It makes an epic driver's plain-task
    frontier (the open tasks whose every ``Blocked-by`` dependency is closed)
    machine-derivable instead of inferred from plan prose (#2269).
    """
    runnable: list[epics.IssueRef] = []
    blocked: list[epics.IssueRef] = []
    for child in epics.child_states(epic):
        if child.state != "OPEN":
            continue
        target = runnable if epics.all_blockers_closed(child.ref) else blocked
        target.append(child.ref)
    return ChildStatus(epic=epic, runnable=tuple(runnable), blocked=tuple(blocked))


def operational_pending(org: str, *, home: str | None = None) -> list[OperationalStatus]:
    """Open finite epics with outstanding operational children.

    Reports the "code-complete, operation-pending" state that keeps an epic
    honestly not-done: its code tasks may all be merged, but until its
    operational tasks (validations, deployments, …) run the epic is not finished.
    Only epics with at least one open operational child appear. Scoped to the
    resolved epic *home* (default ``<org>/.github``).
    """
    if home is None:
        home = f"{org}/.github"
    home_owner, home_repo = home.split("/", 1)
    pending: list[OperationalStatus] = []
    for summary in roadmap.gather(org, home=home):
        status = operational_status(epics.IssueRef(home_owner, home_repo, summary.number))
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
    a comment. A closed operational-labelled issue whose comments carry no success
    ``Outcome:`` line is drift — most likely closed by hand — and is flagged.
    Searches every operational label. Report-only: like the other invariants, this
    is never auto-acted.
    """
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
            "--json",
            "number,repository",
        )
        for item in raw if isinstance(raw, list) else []:
            name_with_owner = str((item.get("repository") or {}).get("nameWithOwner", ""))
            if "/" not in name_with_owner:
                continue
            number = int(item["number"])
            detail: Any = github.read_json(
                "issue", "view", str(number), "--repo", name_with_owner, "--json", "comments"
            )
            comments = (detail.get("comments") or []) if isinstance(detail, dict) else []
            bodies = "\n".join(str((c or {}).get("body", "")) for c in comments)
            if not _OPERATIONAL_SUCCESS_RE.search(bodies):
                violations.append(f"{name_with_owner}#{number}")
    return violations


def epic_outside_dotgithub(org: str) -> list[str]:
    """Open ``epic``-labelled issues wrongly outside ``<org>/.github`` (invariant 1).

    Invariant 1 (epic #85, generalized for visibility-based homes in epic #130):
    a **public** repo's epics live centrally in the org's ``.github``; a
    **private** repo legitimately homes its own epics in itself. So an open
    epic-labelled issue outside ``.github`` is a violation only when its repo is
    public. Visibility is probed per repo (memoized); a probe error is fail-loud
    — a genuine leaked-out public epic must never be masked by a silent skip.
    Returns the ``owner/repo#n`` slugs of violations.
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
        "--json",
        "number,repository",
    )
    dotgithub = f"{org}/.github"
    violations: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        name_with_owner = str((item.get("repository") or {}).get("nameWithOwner", ""))
        if not name_with_owner or name_with_owner == dotgithub:
            continue
        if not github.is_public(name_with_owner):
            continue  # private repo legitimately self-homes its epics (fail-loud on probe error)
        violations.append(f"{name_with_owner}#{item['number']}")
    return violations


def stray_dotgithub_issue(org: str, *, home: str | None = None) -> list[str]:
    """Open issues in the epic *home* that violate invariant 2 (epic #85).

    The home holds only epics, the ``archive``-labelled per-quarter buckets (a
    legitimate top-level home issue, skipped like epics), and issues linked under
    an epic — managed tasks and intake/planning queue items alike (a queue item
    is parented under its queue epic, so no kind-label exemption is needed). An
    open issue that is none of these — an unlinked, non-epic, non-archive issue —
    is a stray; returns their
    slugs. *home* defaults to ``<org>/.github``; a ``--repo``-targeted audit passes
    the repo's resolved home. When a candidate's parent cannot be confirmed as an
    epic it is reported (fail-loud, the human decides).
    """
    if home is None:
        home = f"{org}/.github"
    home_owner, home_repo = home.split("/", 1)
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        "--state",
        "open",
        "--limit",
        "500",
        "--json",
        "number,labels",
    )
    strays: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        labels = {str((label or {}).get("name", "")) for label in (item.get("labels") or [])}
        if "epic" in labels or "archive" in labels:
            continue
        number = int(item["number"])
        parent = epics.parent_of(epics.IssueRef(home_owner, home_repo, number))
        if parent is not None and epics.is_epic(parent):
            continue
        strays.append(f"{home}#{number}")
    return strays


def closed_epic_open_child(org: str, *, home: str | None = None) -> list[EpicOpenChildViolation]:
    """Closed epics that still have an open child (invariant, issue #2259 Fix B).

    Core invariant: *an epic must never be closed while it has open children*. A
    PR that legitimately ``Ref``'d an epic once tripped ``task_drift`` into
    closing the epic directly, orphaning its open tasks — and no audit check
    caught the result. This is that check. Uses **native children** as the
    authoritative signal (``child_states``); perpetual (``ad-hoc``) epics are
    skipped — they never roll up, so being closed-with-open-children is
    not a violation for them. Report-only: like the other invariants it is never
    auto-acted (remediation is :func:`reopen_epics_with_open_children`, gated to a
    human). Scoped to the resolved epic *home* (default ``<org>/.github``).
    """
    if home is None:
        home = f"{org}/.github"
    home_owner, home_repo = home.split("/", 1)
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        "--label",
        "epic",
        "--state",
        "closed",
        "--limit",
        "300",
        "--json",
        "number,title,labels",
    )
    violations: list[EpicOpenChildViolation] = []
    for item in raw if isinstance(raw, list) else []:
        labels = {str((label or {}).get("name", "")) for label in (item.get("labels") or [])}
        if epics.is_standing(labels):
            continue  # standing epics never roll up; closed-with-open-child doesn't apply
        epic = epics.IssueRef(home_owner, home_repo, int(item["number"]))
        open_kids = tuple(cs.ref for cs in epics.child_states(epic) if cs.state == "OPEN")
        if open_kids:
            violations.append((epic, open_kids))
    return violations


# -- orphan pass, label retirement, queue visibility (epic .github#176) --------
#
# Invariant: every open issue in a managed repo is a legitimate top-level kind
# (epic, archive, release tracking issue) or has an epic parent. The checks below
# share one org-wide enumeration (:func:`open_issues`); the ``--close`` sweep
# applies them in the order nativize -> strip labels -> move public queues ->
# attach orphans.

_OPEN_ISSUES_QUERY = """
query($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    issues(states: OPEN, first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        labels(first: 100) { totalCount nodes { name } }
        parent { number repository { name owner { login } } }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class OpenIssue:
    """One open issue (never a PR) in a managed repo, as the orphan pass sees it."""

    ref: epics.IssueRef
    title: str
    body: str
    labels: frozenset[str]
    native_parent: epics.IssueRef | None  # the native sub-issue parent only
    public: bool  # the issue's repo is PUBLIC

    @property
    def body_parent(self) -> epics.IssueRef | None:
        """The parent a body ``Parent:`` line declares, or None."""
        match = epics._PARENT_RE.search(self.body)
        if match is None:
            return None
        return epics.IssueRef(match.group(1), match.group(2), int(match.group(3)))


def _repo_open_issues(owner: str, bare: str, *, public: bool) -> list[OpenIssue]:
    """Every open issue in ``owner/bare`` via paginated GraphQL (PRs excluded)."""
    found: list[OpenIssue] = []
    after: str | None = None
    while True:
        variables: dict[str, object] = {"owner": owner, "name": bare}
        if after is not None:
            variables["after"] = after
        data: Any = github.graphql(_OPEN_ISSUES_QUERY, **variables)
        page = ((data or {}).get("repository") or {}).get("issues") or {}
        for node in page.get("nodes") or []:
            ref = epics.IssueRef(owner, bare, int(node["number"]))
            label_conn = node.get("labels") or {}
            names = frozenset(str(n["name"]) for n in label_conn.get("nodes") or [])
            if int(label_conn.get("totalCount") or 0) > len(names):
                # Never judge an issue on a truncated label set (fail loud).
                raise RuntimeError(f"{ref.slug} has more labels than one page returns")
            parent = node.get("parent")
            found.append(
                OpenIssue(
                    ref=ref,
                    title=str(node.get("title") or ""),
                    body=str(node.get("body") or ""),
                    labels=names,
                    native_parent=epics._ref_from_node(parent) if parent else None,
                    public=public,
                )
            )
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return found
        after = str(info["endCursor"])


def open_issues(org: str) -> list[OpenIssue]:
    """Every open issue in *org*'s managed repos — the orphan pass's shared domain.

    Managed means non-archived with a ``vergil.toml`` (:func:`epics.is_managed_repo`);
    unmanaged repos are never read, so never written to. Enumerated per repo with
    paginated GraphQL rather than ``gh search issues``: search caps at 1000
    results and does not return the native parent, which every check here needs.
    Visibility is probed once per repo (memoized by :func:`github.is_public`).
    """
    issues: list[OpenIssue] = []
    for bare in github.list_org_repos(org):
        repo = f"{org}/{bare}"
        if not epics.is_managed_repo(repo):
            continue
        issues += _repo_open_issues(org, bare, public=github.is_public(repo))
    return issues


def _epic_body_parent(issue: OpenIssue) -> epics.IssueRef | None:
    """The epic a body-only ``Parent:`` line names (no native parent), else None."""
    if issue.native_parent is not None:
        return None
    declared = issue.body_parent
    if declared is not None and epics.is_epic(declared):
        return declared
    return None


@dataclass(frozen=True)
class Orphan:
    """An open issue with no epic parent, and the queue home whose intake epic takes it.

    The intake epic is resolved (and created if absent) only on attach, so the
    read-only preview never writes.
    """

    ref: epics.IssueRef
    home: str  # the intake epic's home repo, "owner/repo"
    name: str  # the <name> in "Epic (intake): <name>"


def orphan_drift(org: str, *, issues: list[OpenIssue] | None = None) -> list[Orphan]:
    """Open issues in managed repos that are neither legitimate top-level nor parented.

    Excludes legitimate top-level kinds (:func:`epics.is_legitimate_top_level`:
    epics, archives, release tracking issues — so a release in flight is never
    attached), issues with a native parent, and issues whose body ``Parent:``
    line names an epic (those count as parented; :func:`nativize_parents`
    converts them). Each orphan targets the intake epic of its queue home
    (:func:`epics.queue_home`): ``<org>/.github`` for a public repo, the repo
    itself for a private one.
    """
    if issues is None:
        issues = open_issues(org)
    orphans: list[Orphan] = []
    for issue in issues:
        if epics.is_legitimate_top_level(set(issue.labels), issue.title, issue.body):
            continue
        if issue.native_parent is not None:
            continue
        if issue.body_parent is not None and epics.is_epic(issue.body_parent):
            continue
        home, name = epics.queue_home(f"{issue.ref.owner}/{issue.ref.repo}")
        orphans.append(Orphan(ref=issue.ref, home=home, name=name))
    return orphans


_QUEUE_FULL_HINT = {
    "intake": "Run intake-review.",
    "planning": "Work the planning queue (epic-create).",
}


class StandingEpicFull(RuntimeError):  # noqa: N818 - the name is the plan's interface
    """A queue epic is at the open-children cap; the pass stopped before overflowing it.

    Raised before any write that would exceed the cap, naming every item not yet
    linked, so nothing is attached past the cap and nothing is silently dropped.
    """

    def __init__(
        self,
        epic: epics.IssueRef,
        open_children: int,
        pending: list[epics.IssueRef],
        *,
        kind: str = "intake",
        noun: str = "orphan(s) not attached",
    ) -> None:
        self.epic = epic
        self.open_children = open_children
        self.pending = pending
        listed = ", ".join(ref.slug for ref in pending)
        super().__init__(
            f"{kind} epic {epic.slug} is full ({open_children} open); "
            f"{len(pending)} {noun}: {listed}. {_QUEUE_FULL_HINT[kind]}"
        )


class _QueueCapacity:
    """Open-children counts per queue epic, checked before each link (the cap)."""

    def __init__(self) -> None:
        self._open: dict[epics.IssueRef, int] = {}

    def reserve(
        self, epic: epics.IssueRef, pending: list[epics.IssueRef], *, kind: str, noun: str
    ) -> None:
        """Count one more open child under *epic*, or raise if it is already full."""
        if epic not in self._open:
            self._open[epic] = sum(1 for c in epics.child_states(epic) if c.state == "OPEN")
        if self._open[epic] >= epics._ADHOC_ARCHIVE_SEGMENT_CAP:
            raise StandingEpicFull(epic, self._open[epic], pending, kind=kind, noun=noun)
        self._open[epic] += 1


def attach_orphans(orphans: list[Orphan]) -> int:
    """Attach each orphan, in order, under its home's intake epic; return the count.

    The intake epic is resolved (created if absent) once per home. Before each
    attach the epic's open children are checked against the cap; at the cap this
    raises :class:`StandingEpicFull` naming every orphan not yet attached. Writes
    to GitHub; the caller gates it and scopes the org token.
    """
    targets: dict[tuple[str, str], epics.IssueRef] = {}
    capacity = _QueueCapacity()
    for index, orphan in enumerate(orphans):
        key = (orphan.home, orphan.name)
        if key not in targets:
            targets[key] = epics.ensure_standing_epic(epics.INTAKE, orphan.home, orphan.name)
        target = targets[key]
        pending = [o.ref for o in orphans[index:]]
        capacity.reserve(target, pending, kind="intake", noun="orphan(s) not attached")
        epics.add_child(target, orphan.ref)
    return len(orphans)


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
    org: str, *, issues: list[OpenIssue] | None = None
) -> list[tuple[epics.IssueRef, epics.IssueRef]]:
    """``(task, epic)`` pairs whose only link is a body ``Parent:`` line naming an epic."""
    if issues is None:
        issues = open_issues(org)
    pairs: list[tuple[epics.IssueRef, epics.IssueRef]] = []
    for issue in issues:
        epic = _epic_body_parent(issue)
        if epic is not None:
            pairs.append((issue.ref, epic))
    return pairs


def nativize_parents(pairs: list[tuple[epics.IssueRef, epics.IssueRef]]) -> int:
    """Link each body-declared task under its epic natively; return the count."""
    for task, epic in pairs:
        epics.add_child(epic, task)
    return len(pairs)


def public_self_homed_queues(
    org: str, *, issues: list[OpenIssue] | None = None
) -> list[epics.IssueRef]:
    """Live intake/planning epics outside ``<org>/.github`` in a repo that is now public."""
    if issues is None:
        issues = open_issues(org)
    queues = (epics.INTAKE, epics.PLANNING)
    return [
        issue.ref
        for issue in issues
        if issue.public
        and issue.ref.repo != ".github"
        and epics.standing_kind_of(set(issue.labels)) in queues
    ]


def move_public_queues(queue_epics: list[epics.IssueRef], *, org: str) -> int:
    """Move a now-public repo's queue items under the ``.github`` queues; return the count.

    For each self-homed queue epic: reparent every open child under the
    ``<org>/.github`` epic of the same kind (created if absent), then close the
    emptied epic with a comment naming where the items went. The cap is checked
    before every move; at the cap this raises :class:`StandingEpicFull` and the
    old epic is left open, so nothing is half-moved silently.
    """
    capacity = _QueueCapacity()
    moved = 0
    for old in queue_epics:
        key = epics.queue_kind(old)
        if key is None:
            raise ValueError(f"{old.slug} is not an intake or planning epic")
        kind = epics.INTAKE if key == "intake" else epics.PLANNING
        target = epics.ensure_standing_epic(kind, f"{org}/.github", org)
        open_kids = [c.ref for c in epics.child_states(old) if c.state == "OPEN"]
        for index, child in enumerate(open_kids):
            capacity.reserve(target, open_kids[index:], kind=key, noun="queue item(s) not moved")
            epics.reparent_child(target, child)
            moved += 1
        github.run(
            "issue",
            "close",
            str(old.number),
            "--repo",
            f"{old.owner}/{old.repo}",
            "--comment",
            f"Repo is now public; queue items moved to {target.slug}.",
        )
    return moved


def private_under_public_epic(org: str, *, issues: list[OpenIssue] | None = None) -> list[str]:
    """Open private-repo issues parented under a public ``<org>/.github`` epic (report only).

    A likely unsupported public → private flip: tooling never migrates or
    retracts it; a human resolves it. Native and body ``Parent:`` links both
    count. A private ``.github`` means the whole org is private and homes there,
    so nothing is reported then.
    """
    if issues is None:
        issues = open_issues(org)
    dotgithub = f"{org}/.github"
    flagged: list[str] = []
    for issue in issues:
        if issue.public:
            continue
        parent = issue.native_parent or issue.body_parent
        if parent is not None and f"{parent.owner}/{parent.repo}" == dotgithub:
            flagged.append(issue.ref.slug)
    if flagged and not github.is_public(dotgithub):
        return []
    return flagged


def render_epic_children(epic: epics.IssueRef, children: list[epics.ChildState]) -> str:
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
    """
    ordered = sorted(children, key=lambda c: (c.ref.owner, c.ref.repo, c.ref.number))
    payload = {
        "epic": epic.slug,
        "children": [
            {
                "repo": f"{child.ref.owner}/{child.ref.repo}",
                "number": child.ref.number,
                "title": child.title,
                "state": child.state,
            }
            for child in ordered
        ],
    }
    return json.dumps(payload, indent=2)


def render(
    tasks: list[TaskDrift],
    epic_summaries: list[roadmap.EpicSummary],
    *,
    org: str,
    window_days: int,
    home: str | None = None,
    epics_outside: list[str] | None = None,
    stray: list[str] | None = None,
    pending_operational: list[OperationalStatus] | None = None,
    closed_operational_no_success: list[str] | None = None,
    closed_epic_open_children: list[EpicOpenChildViolation] | None = None,
    orphans: list[Orphan] | None = None,
    retired_labels: list[RetiredLabels] | None = None,
    body_only: list[tuple[epics.IssueRef, epics.IssueRef]] | None = None,
    public_queues: list[epics.IssueRef] | None = None,
    private_under_public: list[str] | None = None,
) -> str:
    """Format the drift + invariant report; a clean state says so explicitly.

    The orphan pass (epic .github#176) adds ``orphans``, ``retired_labels``,
    ``body_only`` and ``public_queues`` (what ``--close`` would apply) and the
    report-only ``private_under_public`` invariant.

    The report opens with a banner naming the audited org and window and
    stating the run is read-only, so the output is never mistaken for a list of
    actions the tool took. ``epics_outside`` and ``stray`` are the invariant
    violations (epic #85). ``pending_operational`` lists epics still gated on
    operational tasks (validations/deployments, runnable vs blocked);
    ``closed_operational_no_success`` is the operational invariant (a closed
    operational task with no recorded success comment). ``closed_epic_open_children``
    is the closed-epic-with-open-child invariant (issue #2259). Each section
    appears only when it has something to report.
    """
    epics_outside = epics_outside or []
    stray = stray or []
    pending_operational = pending_operational or []
    closed_operational_no_success = closed_operational_no_success or []
    closed_epic_open_children = closed_epic_open_children or []
    orphans = orphans or []
    retired_labels = retired_labels or []
    body_only = body_only or []
    public_queues = public_queues or []
    private_under_public = private_under_public or []
    scope = f"the **{home}** repo" if home and home != f"{org}/.github" else f"the **{org}** org"
    banner = (
        f"_Read-only audit of {scope} (merged PRs from the last "
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
            public_queues,
            private_under_public,
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
            lines.append(f"- {status.epic.slug} — runnable: {runnable}; blocked: {blocked}")
    lines += _render_orphan_pass(orphans, retired_labels, body_only, public_queues)
    if (
        epics_outside
        or stray
        or closed_operational_no_success
        or closed_epic_open_children
        or private_under_public
    ):
        lines += ["", "## Invariant violations (issues in the wrong place)", ""]
        if epics_outside:
            lines.append(
                "**Epics outside `.github`** (public repos) — move each to the org's `.github`:"
            )
            lines += [f"- {slug}" for slug in sorted(epics_outside)]
        if stray:
            lines.append("**Stray `.github` issues** — not an epic, intake, or linked task:")
            lines += [f"- {slug}" for slug in sorted(stray)]
        if closed_operational_no_success:
            lines.append("**Validation tasks closed without a PASS comment** — re-open and run:")
            lines += [f"- {slug}" for slug in sorted(closed_operational_no_success)]
        if closed_epic_open_children:
            lines.append(
                "**Closed epics with open children** — an epic must not be closed "
                "while a child is open; reopen each (`--close` remediates):"
            )
            for closed_epic, kids in sorted(
                closed_epic_open_children, key=lambda v: (v[0].owner, v[0].repo, v[0].number)
            ):
                open_kids = ", ".join(
                    kid.slug for kid in sorted(kids, key=lambda k: (k.owner, k.repo, k.number))
                )
                lines.append(f"- {closed_epic.slug} — open children: {open_kids}")
        lines += _render_private_under_public(private_under_public)
    lines.append("")
    return "\n".join(lines)


_PRIVATE_UNDER_PUBLIC_HEADING = (
    "**Private repo work under public .github epics** — an unsupported public → "
    "private flip? Resolve by hand (spec: Private repos and visibility):"
)


def _render_private_under_public(slugs: list[str]) -> list[str]:
    if not slugs:
        return []
    return [_PRIVATE_UNDER_PUBLIC_HEADING, *[f"- {slug}" for slug in sorted(slugs)]]


def _render_orphan_pass(
    orphans: list[Orphan],
    retired_labels: list[RetiredLabels],
    body_only: list[tuple[epics.IssueRef, epics.IssueRef]],
    public_queues: list[epics.IssueRef],
) -> list[str]:
    """The ``--close`` orphan-pass preview, one section per non-empty pass, in apply order."""
    lines: list[str] = []
    if body_only:
        lines += ["", "## Body-only Parent: links (`--close` links them natively)", ""]
        lines += [f"- {task.slug} → {epic.slug}" for task, epic in body_only]
    if retired_labels:
        lines += ["", "## Retired kind labels (`--close` removes them)", ""]
        lines += [f"- {item.ref.slug} — {', '.join(item.labels)}" for item in retired_labels]
    if public_queues:
        lines += [
            "",
            "## Queues of now-public repos (`--close` moves their items to `.github`)",
            "",
        ]
        lines += [f"- {ref.slug}" for ref in public_queues]
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
_EPIC_REOPEN_COMMENT = (
    "Reopened by `vrg-epic-audit`: the epic was closed while child {child} is still "
    "open — an epic cannot be closed while it has open children."
)


def reopen_epics_with_open_children(violations: list[EpicOpenChildViolation]) -> list[str]:
    """Reopen each closed epic that still has an open child (issue #2259 Fix C).

    The inverse of ``epic_drift``'s close: it restores the *an epic must never be
    closed while it has open children* invariant that ``task_drift`` violated,
    leaving an explanatory comment naming an open child. *violations* is the
    output of :func:`closed_epic_open_child` (already closed epics), so every
    reopen is meaningful; ``gh issue reopen`` on an already-open epic is a no-op,
    so a repeat run is idempotent. Returns the reopened epic slugs. This performs
    outward-effecting GitHub writes; the caller gates it to a human.
    """
    reopened: list[str] = []
    for epic, open_kids in sorted(violations, key=lambda v: (v[0].owner, v[0].repo, v[0].number)):
        github.run(
            "issue",
            "reopen",
            str(epic.number),
            "--repo",
            f"{epic.owner}/{epic.repo}",
            "--comment",
            _EPIC_REOPEN_COMMENT.format(child=open_kids[0].slug),
        )
        reopened.append(epic.slug)
    return reopened


def close_drift(
    tasks: list[TaskDrift],
    epics: list[roadmap.EpicSummary],
    *,
    org: str,
    home: str | None = None,
) -> list[str]:
    """Close each drifted task and rolled-up epic, leaving a comment on each.

    Epics live in the resolved *home* (default ``{org}/.github``). Returns the
    ``owner/repo#n`` slugs closed, in the order acted on. This performs
    outward-effecting GitHub writes; the caller is responsible for gating it to a
    human.
    """
    if home is None:
        home = f"{org}/.github"
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
    epic_repo = home
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
    home: str | None = None,
    reopened: list[str] | None = None,
    private_under_public: list[str] | None = None,
) -> str:
    """Summarize a ``--close`` run: what was closed and what was reopened.

    ``reopened`` is the closed-epic-with-open-child remediation (issue #2259):
    epics that were wrongly closed while a child was still open, now reopened.
    ``private_under_public`` is report-only (never acted on), surfaced here too so
    the scheduled sweep's log carries it.
    """
    reopened = reopened or []
    private_under_public = private_under_public or []
    scope = f"the **{home}** repo" if home and home != f"{org}/.github" else f"the **{org}** org"
    banner = f"_Closed drift on {scope} (merged PRs from the last {window_days} days)._"
    header = ["# Epic/task drift audit — closed", "", banner, ""]
    if not closed and not reopened and not private_under_public:
        return "\n".join([*header, "_No drift — nothing to close._", ""])
    lines = [*header]
    if closed:
        lines += ["## Closed", "", *[f"- {slug}" for slug in closed], ""]
    if reopened:
        lines += [
            "## Reopened (epic was closed with an open child)",
            "",
            *[f"- {slug}" for slug in reopened],
            "",
        ]
    if private_under_public:
        lines += ["## Needs a human (report only)", ""]
        lines += [*_render_private_under_public(private_under_public), ""]
    return "\n".join(lines)
