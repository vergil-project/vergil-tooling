"""Generate the project activity log — recently closed work across the org.

The backward-looking companion to the roadmap ("where we've been"). ``gather``
queries closed issues across the org via ``gh search``; ``render`` groups them by
close-date, most recent first. This is best-effort reporting (search-based) — a
roughly-right ledger is the goal, unlike the exact epic rollup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import github, release


@dataclass(frozen=True)
class ActivityItem:
    """One closed issue in the ledger."""

    repo: str  # owner/name
    number: int
    title: str
    url: str
    closed_date: str  # YYYY-MM-DD


def gather(since: str, *, org: str | None = None) -> list[ActivityItem]:
    """Closed issues across *org* with ``closedAt`` on or after *since* (YYYY-MM-DD).

    *org* defaults to the owner of the current repo's git remote.
    """
    if org is None:
        org = github.current_org()
    raw: Any = github.read_json(
        "search",
        "issues",
        "--owner",
        org,
        "--state",
        "closed",
        "--closed",
        f">={since}",
        "--limit",
        "100",
        "--json",
        "number,title,repository,url,closedAt,body",
    )
    items: list[ActivityItem] = []
    for entry in raw if isinstance(raw, list) else []:
        repo = str((entry.get("repository") or {}).get("nameWithOwner", ""))
        closed = str(entry.get("closedAt") or "")
        if not repo or not closed:
            continue
        # Release tracking issues (release: X.Y.Z) are vrg-release bookkeeping,
        # not epic/task work — keep them out of the ledger. See issue #1984.
        if release.is_release_tracking_issue(
            title=str(entry.get("title") or ""), body=str(entry.get("body") or "")
        ):
            continue
        items.append(
            ActivityItem(
                repo=repo,
                number=int(entry["number"]),
                title=str(entry["title"]),
                url=str(entry["url"]),
                closed_date=closed[:10],
            )
        )
    return items


def render(items: list[ActivityItem]) -> str:
    """Render the ledger markdown, grouped by close-date (most recent first)."""
    if not items:
        return "# Activity log\n\n_No recently closed issues._\n"
    by_date: dict[str, list[ActivityItem]] = {}
    for item in items:
        by_date.setdefault(item.closed_date, []).append(item)
    lines = [
        "# Activity log",
        "",
        f"_{len(items)} issue(s) closed in the last 30 days. Generated; do not edit._",
        "",
    ]
    for date in sorted(by_date, reverse=True):
        lines.append(f"## {date}")
        lines.append("")
        for item in sorted(by_date[date], key=lambda i: (i.repo, i.number)):
            lines.append(f"- [{item.repo}#{item.number}]({item.url}) {item.title}")
        lines.append("")
    return "\n".join(lines)
