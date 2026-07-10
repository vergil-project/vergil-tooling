"""Audit for epic/task drift — work that slipped through auto-close.

``task_drift`` finds merged PRs whose ``Ref``'d task is still open;
``epic_drift`` finds open, non-perpetual epics whose children are all closed
(should have rolled up). ``render`` formats both sections for review;
``close_drift`` closes them with an explanatory comment — a human action, gated
by the caller.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import epics, github, linkage, release, roadmap

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
        if labels & epics.operational_labels() or labels & _INTAKE_LABELS:
            # An operational task (validation/deployment) closes on a recorded
            # ``Outcome:`` comment, and intake issues (triage/idea/research) are
            # never PR-tracked tasks — a merged PR ``Ref``ing either is not the
            # slipped-task drift this sweep closes.
            continue
        # A merged release PR Refs its release: X.Y.Z tracking issue, which stays
        # open as vrg-release bookkeeping — not drift. Skip it so the audit does
        # not flag release issues as slipped tasks. See issue #1984.
        if release.is_release_tracking_issue(
            title=str(issue.get("title") or ""), body=str(issue.get("body") or "")
        ):
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


_INTAKE_LABELS = frozenset({"triage", "idea", "research"})


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

    The home holds only epics, intake (``triage``/``idea``/``research``), and
    managed tasks whose closing PR lands there (a task linked under an epic). An
    open issue that is none of these — an unlinked, non-epic, non-intake issue —
    is a stray; returns their slugs. *home* defaults to ``<org>/.github``; a
    ``--repo``-targeted audit passes the repo's resolved home. When a candidate's
    parent cannot be confirmed as an epic it is reported (fail-loud, the human
    decides).
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
        if "epic" in labels or (labels & _INTAKE_LABELS):
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
    authoritative signal (``child_states``); perpetual (``ad-hoc``/``standing``)
    epics are skipped — they never roll up, so being closed-with-open-children is
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
        if labels & {"ad-hoc", "standing"}:
            continue  # perpetual epics never roll up; closed-with-open-child doesn't apply
        epic = epics.IssueRef(home_owner, home_repo, int(item["number"]))
        open_kids = tuple(cs.ref for cs in epics.child_states(epic) if cs.state == "OPEN")
        if open_kids:
            violations.append((epic, open_kids))
    return violations


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
) -> str:
    """Format the drift + invariant report; a clean state says so explicitly.

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
    if epics_outside or stray or closed_operational_no_success or closed_epic_open_children:
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
    lines.append("")
    return "\n".join(lines)


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
) -> str:
    """Summarize a ``--close`` run: what was closed and what was reopened.

    ``reopened`` is the closed-epic-with-open-child remediation (issue #2259):
    epics that were wrongly closed while a child was still open, now reopened.
    """
    reopened = reopened or []
    scope = f"the **{home}** repo" if home and home != f"{org}/.github" else f"the **{org}** org"
    banner = f"_Closed drift on {scope} (merged PRs from the last {window_days} days)._"
    header = ["# Epic/task drift audit — closed", "", banner, ""]
    if not closed and not reopened:
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
    return "\n".join(lines)
