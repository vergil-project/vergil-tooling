"""Audit for epic/task drift — work that slipped through auto-close.

``task_drift`` finds merged PRs whose ``Ref``'d task is still open;
``epic_drift`` finds open, non-standing epics whose children are all closed
(should have rolled up). ``render`` formats both sections for review;
``close_drift`` closes them with an explanatory comment — a human action, gated
by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import github, linkage, release, roadmap


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
        repo = str((entry.get("repository") or {}).get("nameWithOwner", ""))
        if not repo:
            continue
        try:
            task = linkage.extract_tracking_issue(str(entry.get("body") or ""))
        except ValueError:
            continue
        if task is None:
            continue
        issue = github.read_json(
            "issue", "view", str(task), "--repo", repo, "--json", "state,title,body"
        )
        if not isinstance(issue, dict):
            continue
        if str(issue.get("state") or "").upper() != "OPEN":
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
                repo=repo, task=task, pr_number=int(entry["number"]), pr_url=str(entry["url"])
            )
        )
    return drift


def epic_drift() -> list[roadmap.EpicSummary]:
    """Open, non-standing epics whose children are all closed (should roll up)."""
    return [epic for epic in roadmap.gather() if epic.total > 0 and epic.closed == epic.total]


def render(
    tasks: list[TaskDrift],
    epics: list[roadmap.EpicSummary],
    *,
    org: str,
    window_days: int,
) -> str:
    """Format the drift report; a clean state says so explicitly.

    The report opens with a banner naming the audited org and window and
    stating the run is read-only, so the output is never mistaken for a list of
    actions the tool took.
    """
    banner = (
        f"_Read-only audit of the **{org}** org (merged PRs from the last "
        f"{window_days} days) — this report changes nothing; run `--close` (as "
        "a human) to close what it lists._"
    )
    header = ["# Epic/task drift audit", "", banner, ""]
    if not tasks and not epics:
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
    if epics:
        for epic in sorted(epics, key=lambda e: e.number):
            lines.append(
                f"- [#{epic.number}]({epic.url}) {epic.title} — {epic.closed}/{epic.total} done"
            )
    else:
        lines.append("- _none_")
    lines.append("")
    return "\n".join(lines)


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


def render_closed(closed: list[str], *, org: str, window_days: int) -> str:
    """Summarize a ``--close`` run: what was actually closed."""
    banner = f"_Closed drift on the **{org}** org (merged PRs from the last {window_days} days)._"
    header = ["# Epic/task drift audit — closed", "", banner, ""]
    if not closed:
        return "\n".join([*header, "_No drift — nothing to close._", ""])
    lines = [*header, "## Closed", ""]
    lines += [f"- {slug}" for slug in closed]
    lines.append("")
    return "\n".join(lines)
