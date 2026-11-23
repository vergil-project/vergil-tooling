"""Generate the project activity log — recently closed work across the org.

The backward-looking companion to the roadmap ("where we've been"). ``gather``
reads closed issues across the org through the trust layer's windowed search
(:func:`vergil_tooling.lib.trust.search_window`), which judges each result's
author, editors, renames and latest close in the same paged calls: an issue an
outsider opened or closed is left out and reported on stderr, metadata only.
``render`` groups the rest by close-date, most recent first. This is
search-based reporting, unlike the exact epic rollup.

``render_pages`` is the multi-page form for the org docs site: an ``index.md``
summary table (one row per day, one column per repo, each cell that repo's count
of closed issues that day), a detail page per day grouped by repo, and a flat
per-repo page that each summary column header links to.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from vergil_tooling.lib import github, release, trust
from vergil_tooling.lib.markdown_escape import escape_inline

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
    """Closed issues across *org* closed on or after *since* (YYYY-MM-DD).

    *org* defaults to the owner of the current repo's git remote. *repo* (a bare
    name, or ``owner/name``) narrows the search to that one repo. An issue
    opened, retitled or last closed by an identity outside the trusted roles
    (or whose body one edited) is left out and reported on stderr, metadata
    only (counts by cause under GitHub Actions). A read failure, or a single
    day with more closures than GitHub's search returns, raises
    :class:`~vergil_tooling.lib.trust.TrustReadError`.
    """
    if org is None:
        org = github.current_org()
    scope = f"user:{org}"
    if repo is not None:
        scope = f"repo:{repo if '/' in repo else f'{org}/{repo}'}"
    hits, skipped = trust.search_window(
        f"{scope} is:closed", kind="issue", field="closed", since=since, judge_close=True
    )
    trust.report_skipped(skipped, stream=sys.stderr)
    items: list[ActivityItem] = []
    for hit in hits:
        # Release tracking issues (release: X.Y.Z) are vrg-release bookkeeping,
        # not epic/task work — keep them out of the ledger. See issue #1984.
        if release.is_release_tracking_issue(title=hit.title, body=hit.body):
            continue
        if hit.closed_at is None:
            # judge_close always sets it; never date an issue by a guess.
            raise trust.TrustReadError("activity_log.gather", f"{hit.url} has no close time")
        items.append(
            ActivityItem(
                repo=hit.repo,
                number=hit.number,
                title=hit.title,
                url=hit.url,
                closed_date=hit.closed_at[:10],
            )
        )
    return items


def _by_date(items: list[ActivityItem]) -> dict[str, list[ActivityItem]]:
    by_date: dict[str, list[ActivityItem]] = {}
    for item in items:
        by_date.setdefault(item.closed_date, []).append(item)
    return by_date


def _line(item: ActivityItem) -> str:
    return f"- [{item.repo}#{item.number}]({item.url}) {escape_inline(item.title)}"


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
