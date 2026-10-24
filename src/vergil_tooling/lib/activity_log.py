"""Generate the project activity log — recently closed work across the org.

The backward-looking companion to the roadmap ("where we've been"). ``gather``
queries closed issues across the org via ``gh search``; ``render`` groups them by
close-date, most recent first. This is best-effort reporting (search-based) — a
roughly-right ledger is the goal, unlike the exact epic rollup.

``render_pages`` is the multi-page form for the org docs site: an ``index.md``
summary table (one row per day, one column per repo, each cell that repo's count
of closed issues that day), a detail page per day grouped by repo, and a flat
per-repo page that each summary column header links to.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import github, release

# gh search returns at most 1000 results, however many pages it follows.
_SEARCH_LIMIT = 1000

# Subdirectories of the multi-page output; render_pages owns their contents.
DAYS_DIR = "days"
REPOS_DIR = "repos"


@dataclass(frozen=True)
class ActivityItem:
    """One closed issue in the ledger."""

    repo: str  # owner/name
    number: int
    title: str
    url: str
    closed_date: str  # YYYY-MM-DD


def gather(since: str, *, org: str | None = None, repo: str | None = None) -> list[ActivityItem]:
    """Closed issues across *org* with ``closedAt`` on or after *since* (YYYY-MM-DD).

    *org* defaults to the owner of the current repo's git remote. *repo* (a bare
    name, or ``owner/name``) narrows the search to that one repo.
    """
    if org is None:
        org = github.current_org()
    scope = ["--owner", org]
    if repo is not None:
        scope = ["--repo", repo if "/" in repo else f"{org}/{repo}"]
    raw: Any = github.read_json(
        "search",
        "issues",
        *scope,
        "--state",
        "closed",
        "--closed",
        f">={since}",
        "--limit",
        str(_SEARCH_LIMIT),
        "--json",
        "number,title,repository,url,closedAt,body",
    )
    if isinstance(raw, list) and len(raw) >= _SEARCH_LIMIT:
        # gh stopped at its cap, so the oldest closures in the window are missing.
        print(
            f"vrg-activity-log: warning: the search hit its {_SEARCH_LIMIT}-result cap; "
            "the oldest days in the window are incomplete.",
            file=sys.stderr,
        )
    items: list[ActivityItem] = []
    for entry in raw if isinstance(raw, list) else []:
        repo_name = str((entry.get("repository") or {}).get("nameWithOwner", ""))
        closed = str(entry.get("closedAt") or "")
        if not repo_name or not closed:
            continue
        # Release tracking issues (release: X.Y.Z) are vrg-release bookkeeping,
        # not epic/task work — keep them out of the ledger. See issue #1984.
        if release.is_release_tracking_issue(
            title=str(entry.get("title") or ""), body=str(entry.get("body") or "")
        ):
            continue
        items.append(
            ActivityItem(
                repo=repo_name,
                number=int(entry["number"]),
                title=str(entry["title"]),
                url=str(entry["url"]),
                closed_date=closed[:10],
            )
        )
    return items


def _by_date(items: list[ActivityItem]) -> dict[str, list[ActivityItem]]:
    by_date: dict[str, list[ActivityItem]] = {}
    for item in items:
        by_date.setdefault(item.closed_date, []).append(item)
    return by_date


def _line(item: ActivityItem) -> str:
    return f"- [{item.repo}#{item.number}]({item.url}) {item.title}"


def _short(repo: str) -> str:
    return repo.split("/", 1)[-1]


def repo_page(repo: str) -> str:
    """File name of *repo*'s (``owner/name``) flat page under :data:`REPOS_DIR`.

    MkDocs skips dot-files, so a leading dot (``.github``) becomes ``dot-``.
    """
    name = _short(repo)
    if name.startswith("."):
        name = "dot-" + name[1:]
    return f"{name}.md"


def render(items: list[ActivityItem], *, title: str = "Activity log") -> str:
    """Render the ledger markdown, grouped by close-date (most recent first)."""
    if not items:
        return f"# {title}\n\n_No recently closed issues._\n"
    by_date = _by_date(items)
    lines = [
        f"# {title}",
        "",
        f"_{len(items)} issue(s) closed in the last 30 days. Generated; do not edit._",
        "",
    ]
    for date in sorted(by_date, reverse=True):
        lines.append(f"## {date}")
        lines.append("")
        for item in sorted(by_date[date], key=lambda i: (i.repo, i.number)):
            lines.append(_line(item))
        lines.append("")
    return "\n".join(lines)


def render_summary(items: list[ActivityItem]) -> str:
    """Render the org summary: a day x repo table of closed-issue counts.

    One row per day (most recent first), linking to its :func:`render_day`
    page; one column per repo with activity in the window, its header linking
    to the repo's flat :func:`render` page. A repo with nothing closed that day
    gets an empty cell, so the busy cells stand out.
    """
    if not items:
        return "# Activity log\n\n_No recently closed issues._\n"
    by_date = _by_date(items)
    repos = sorted({item.repo for item in items})
    header = ["Day", "Total"] + [f"[{_short(r)}]({REPOS_DIR}/{repo_page(r)})" for r in repos]
    lines = [
        "# Activity log",
        "",
        f"_{len(items)} issue(s) closed in the last 30 days. Generated; do not edit._",
        "",
        "| " + " | ".join(header) + " |",
        "| --- |" + " ---: |" * (len(header) - 1),
    ]
    for date in sorted(by_date, reverse=True):
        counts = dict.fromkeys(repos, 0)
        for item in by_date[date]:
            counts[item.repo] += 1
        cells = [f"[{date}]({DAYS_DIR}/{date}.md)", str(len(by_date[date]))]
        cells += [str(counts[r]) if counts[r] else "" for r in repos]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return "\n".join(lines)


def render_day(date: str, items: list[ActivityItem]) -> str:
    """Render one day's detail page: its closed issues grouped by repo."""
    lines = [
        f"# {date}",
        "",
        f"_{len(items)} issue(s) closed. Generated; do not edit._",
        "",
        "[Back to the activity log](../index.md)",
        "",
    ]
    by_repo: dict[str, list[ActivityItem]] = {}
    for item in items:
        by_repo.setdefault(item.repo, []).append(item)
    for repo in sorted(by_repo):
        lines.append(f"## {repo}")
        lines.append("")
        for item in sorted(by_repo[repo], key=lambda i: i.number):
            lines.append(_line(item))
        lines.append("")
    return "\n".join(lines)


def render_pages(items: list[ActivityItem]) -> dict[str, str]:
    """Render the multi-page activity log, keyed by path relative to its root.

    ``index.md`` is the :func:`render_summary` table; ``days/<date>.md`` holds
    each day's detail and ``repos/<name>.md`` each repo's flat ledger.
    """
    pages = {"index.md": render_summary(items)}
    for date, day_items in _by_date(items).items():
        pages[f"{DAYS_DIR}/{date}.md"] = render_day(date, day_items)
    for repo in sorted({item.repo for item in items}):
        repo_items = [item for item in items if item.repo == repo]
        pages[f"{REPOS_DIR}/{repo_page(repo)}"] = render(
            repo_items, title=f"Activity log: {_short(repo)}"
        )
    return pages
