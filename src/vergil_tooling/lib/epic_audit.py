"""Audit for epic/task drift — work that slipped through auto-close.

A read-only safety net (agents cannot close issues). ``task_drift`` finds merged
PRs whose ``Ref``'d task is still open; ``epic_drift`` finds open, non-standing
epics whose children are all closed (should have rolled up). ``render`` formats
both sections for a human to act on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import github, linkage, roadmap


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
        state = github.read_output(
            "issue", "view", str(task), "--repo", repo, "--json", "state", "--jq", ".state"
        ).upper()
        if state == "OPEN":
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
        f"{window_days} days) — this report changes nothing; a human closes "
        "anything it lists._"
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
