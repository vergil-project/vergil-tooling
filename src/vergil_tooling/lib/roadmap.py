"""Generate the project roadmap from open finite epics in a repo's resolved epic home.

"Where we're going", derived mechanically from epic metadata — no hand editing.
``gather`` reads the open ``epic``-labelled issues (skipping perpetual ``ad-hoc`` buckets)
and rolls up each one's children via :mod:`vergil_tooling.lib.epics`; ``render``
turns the summaries into markdown grouped by milestone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import epics, github, release


@dataclass(frozen=True)
class EpicSummary:
    """A roadmap row: one open finite epic and its rolled-up progress."""

    number: int
    title: str
    created: str  # YYYY-MM-DD
    milestone: str | None
    repos: tuple[str, ...]
    total: int
    closed: int
    url: str


def _open_epics(home: str) -> list[Any]:
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        home,
        "--label",
        "epic",
        "--state",
        "open",
        "--limit",
        "200",
        "--json",
        "number,title,createdAt,milestone,labels,url,body",
    )
    return raw if isinstance(raw, list) else []


def _is_perpetual(epic: Any) -> bool:
    """True if the epic is a perpetual ad-hoc bucket (``ad-hoc``, or its
    deprecated ``standing`` alias) — excluded from the strategic roadmap."""
    names = {(label or {}).get("name") for label in (epic.get("labels") or [])}
    return bool(names & {"ad-hoc", "standing"})


def gather(org: str | None = None, *, home: str | None = None) -> list[EpicSummary]:
    """Summarize every open finite (non-perpetual) epic in the resolved epic *home*.

    *org* defaults to the current repo's owner. *home* defaults to that org's
    ``.github`` (via :func:`epics.resolve_epic_home`), so the default is the
    org-level roadmap; pass an explicit *home* (a private repo that self-homes
    its epics) to scope the roadmap to that repo.
    """
    if org is None:
        org = github.current_org()
    if home is None:
        home = epics.resolve_epic_home(org, ".github")
    home_owner, home_repo = home.split("/", 1)
    summaries: list[EpicSummary] = []
    for epic in _open_epics(home):
        if _is_perpetual(epic):
            continue
        # Defense in depth: a release tracking issue is never epic-labelled, so
        # it should not reach here — but skip it explicitly so a stray label can
        # never leak release bookkeeping into the roadmap (or epic-drift, which
        # reads this same gather). See issue #1984.
        if release.is_release_tracking_issue(
            title=str(epic.get("title") or ""), body=str(epic.get("body") or "")
        ):
            continue
        number = int(epic["number"])
        children = epics.child_states(epics.IssueRef(home_owner, home_repo, number))
        repos = tuple(sorted({f"{c.ref.owner}/{c.ref.repo}" for c in children}))
        closed = sum(1 for c in children if c.state == "CLOSED")
        milestone = epic.get("milestone")
        summaries.append(
            EpicSummary(
                number=number,
                title=str(epic["title"]),
                created=str(epic["createdAt"])[:10],
                milestone=milestone.get("title") if isinstance(milestone, dict) else None,
                repos=repos,
                total=len(children),
                closed=closed,
                url=str(epic["url"]),
            )
        )
    return summaries


def _row(epic: EpicSummary) -> str:
    """One markdown table row for an epic (repos stacked in-cell)."""
    repos = "<br>".join(r.split("/")[-1] for r in epic.repos) if epic.repos else "—"
    title = epic.title.replace("|", "\\|")
    return (
        f"| [#{epic.number}]({epic.url}) {title} "
        f"| {epic.closed}/{epic.total} | {repos} | {epic.created} |"
    )


def render(summaries: list[EpicSummary], org: str | None = None, *, home: str | None = None) -> str:
    """Render the roadmap markdown as a table per milestone."""
    if not summaries:
        return "# Roadmap\n\n_No active epics._\n"
    source = home or (f"{org}/.github" if org else "the org .github repo")
    by_milestone: dict[str, list[EpicSummary]] = {}
    for epic in summaries:
        by_milestone.setdefault(epic.milestone or "No milestone", []).append(epic)
    lines = [
        "# Roadmap",
        "",
        f"_Generated from open epics in {source}. Do not edit by hand._",
        "",
    ]
    for milestone in sorted(by_milestone):
        lines.append(f"## {milestone}")
        lines.append("")
        lines.append("| Epic | Done | Repos | Created |")
        lines.append("| --- | --- | --- | --- |")
        for epic in sorted(by_milestone[milestone], key=lambda e: e.number):
            lines.append(_row(epic))
        lines.append("")
    return "\n".join(lines)
