"""Generate the project roadmap from the finite epics in an org's ``.github``.

"Where we're going and where we've been", derived mechanically from epic
metadata — no hand editing. ``gather`` reads the open ``epic``-labelled issues
(skipping the perpetual standing epics and proxy epics) and rolls up each one's
children via :mod:`vergil_tooling.lib.epics`. ``gather_completed`` reads the
epics closed as completed through the trust layer's windowed search
(:func:`vergil_tooling.lib.trust.search_window`), with each one's close date,
task count, repos and Documents links (:mod:`vergil_tooling.lib.epic_docs`).
``render`` turns both into three sections: In flight (an open epic with at
least one closed task), Planned (an open epic with none closed yet) and
Completed (most recently closed first).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import epic_docs, epics, github, release, trust
from vergil_tooling.lib.markdown_escape import escape_inline


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


@dataclass(frozen=True)
class CompletedEpic:
    """A Completed row: one epic closed as completed, and its documents.

    *documents* holds a (doc, URL) pair for each of :data:`epic_docs.DOCS`
    that the epic's Documents section links, in that order; a document the
    section notes as not written, or every document of an epic with no
    section, is absent.
    """

    number: int
    title: str
    closed: str  # YYYY-MM-DD
    repos: tuple[str, ...]
    total: int
    url: str
    documents: tuple[tuple[str, str], ...]


# The close date the Completed search reads from: earlier than any epic.
COMPLETED_SINCE = "2008-01-01"


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
        str(epics.LISTING_LIMIT),
        "--json",
        "number,title,createdAt,milestone,labels,url,body",
    )
    epics.refuse_truncated(raw, epics.LISTING_LIMIT, f"the open epics of {home}")
    return raw if isinstance(raw, list) else []


def _is_perpetual(epic: Any) -> bool:
    """True if the epic is not a development epic of its own: a perpetual
    standing epic (ad-hoc or a queue kind) or a proxy, which lives as long as
    its primary, or a continuation, whose items its chain's head summarizes
    (epic vergil-project/.github#1609). Excluded from the strategic roadmap
    and so from epic drift."""
    names = {str((label or {}).get("name", "")) for label in (epic.get("labels") or [])}
    return epics.is_standing(names) or epics.is_proxy(names) or epics.is_continuation(names)


def gather(org: str | None = None) -> list[EpicSummary]:
    """Summarize every open finite (non-perpetual) epic in *org*'s ``.github``.

    *org* defaults to the current repo's owner. Its ``.github`` (via
    :func:`epics.resolve_epic_home`) is the home of every epic in the org.
    """
    if org is None:
        org = github.current_org()
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
        number = int(epic["number"])
        if release.is_release_tracking_issue(
            home, number, title=str(epic.get("title") or ""), body=str(epic.get("body") or "")
        ):
            continue
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


def _documents(number: int, body: str) -> tuple[tuple[str, str], ...]:
    """The (doc, URL) pairs *body*'s Documents section links, in DOCS order.

    A malformed section raises :class:`epic_docs.EpicDocsFormatError` naming
    the epic: a broken section is never shown as no documents.
    """
    try:
        entries = epic_docs.parse(body, ".github")
    except epic_docs.EpicDocsFormatError as exc:
        msg = f"epic #{number}: {exc}"
        raise epic_docs.EpicDocsFormatError(msg) from exc
    if entries is None:
        return ()
    pairs: list[tuple[str, str]] = []
    for doc in epic_docs.DOCS:
        entry = entries[doc]
        if isinstance(entry, epic_docs.DocLink):
            pairs.append((doc, entry.url))
    return tuple(pairs)


def gather_completed(org: str | None = None) -> list[CompletedEpic]:
    """Every finite epic in *org*'s ``.github`` closed as completed.

    Read through the trust layer's windowed search, each epic's latest close
    judged too: an epic an outsider opened, edited, retitled or closed is left
    out and reported on stderr, metadata only (counts by cause under GitHub
    Actions). An epic closed as not planned was not completed and is not read.
    Container, proxy and archived epics (:func:`epic_docs.is_skipped`),
    continuations (closed with their chain's head, which carries the
    summary) and release tracking issues are left out. A read failure raises
    :class:`trust.TrustReadError`; a malformed Documents section raises
    :class:`epic_docs.EpicDocsFormatError`.
    """
    if org is None:
        org = github.current_org()
    home = epics.resolve_epic_home(org, ".github")
    home_owner, home_repo = home.split("/", 1)
    hits, skipped = trust.search_window(
        f"repo:{home} label:epic is:closed reason:completed",
        kind="issue",
        field="closed",
        since=COMPLETED_SINCE,
        judge_close=True,
    )
    trust.report_skipped(skipped, stream=sys.stderr)
    completed: list[CompletedEpic] = []
    for hit in hits:
        if epic_docs.is_skipped(hit.title, hit.labels) or epics.is_continuation(hit.labels):
            continue
        if release.is_release_tracking_issue(home, hit.number, title=hit.title, body=hit.body):
            continue
        if hit.closed_at is None:
            # judge_close always sets it; never date an epic by a guess.
            raise trust.TrustReadError("roadmap.gather_completed", f"{hit.url} has no close time")
        children = epics.child_states(epics.IssueRef(home_owner, home_repo, hit.number))
        completed.append(
            CompletedEpic(
                number=hit.number,
                title=hit.title,
                closed=hit.closed_at[:10],
                repos=tuple(sorted({f"{c.ref.owner}/{c.ref.repo}" for c in children})),
                total=len(children),
                url=hit.url,
                documents=_documents(hit.number, hit.body),
            )
        )
    return completed


def _repos_cell(repos: tuple[str, ...]) -> str:
    """The repos stacked in one cell by short name, or an em dash for none."""
    return "<br>".join(r.split("/")[-1] for r in repos) if repos else "—"


def _row(epic: EpicSummary) -> str:
    """One markdown table row for an open epic (repos stacked in-cell)."""
    title = escape_inline(epic.title)
    return (
        f"| [#{epic.number}]({epic.url}) {title} "
        f"| {epic.closed}/{epic.total} | {_repos_cell(epic.repos)} | {epic.created} |"
    )


_DOC_LABELS = {"spec": "Spec", "plan": "Plan", "retrospective": "Retrospective"}


def _completed_row(epic: CompletedEpic) -> str:
    """One Completed row: the epic, its close date, tasks, repos and documents."""
    title = escape_inline(epic.title)
    docs = " · ".join(f"[{_DOC_LABELS[doc]}]({url})" for doc, url in epic.documents) or "—"
    return (
        f"| [#{epic.number}]({epic.url}) {title} "
        f"| {epic.closed} | {epic.total} | {_repos_cell(epic.repos)} | {docs} |"
    )


def _open_section(heading: str, summaries: list[EpicSummary]) -> list[str]:
    """An open-epics section: one table, or one per milestone when any has one."""
    lines = [f"## {heading}", ""]
    if not summaries:
        return [*lines, "_None._", ""]
    by_milestone: dict[str | None, list[EpicSummary]] = {}
    for epic in summaries:
        by_milestone.setdefault(epic.milestone, []).append(epic)
    grouped = any(milestone is not None for milestone in by_milestone)
    for milestone in sorted(by_milestone, key=lambda m: (m is None, m or "")):
        if grouped:
            lines += [f"### {escape_inline(milestone or 'No milestone')}", ""]
        lines.append("| Epic | Done | Repos | Created |")
        lines.append("| --- | --- | --- | --- |")
        lines += [_row(epic) for epic in sorted(by_milestone[milestone], key=lambda e: e.number)]
        lines.append("")
    return lines


def _completed_section(completed: list[CompletedEpic]) -> list[str]:
    """The Completed section: one table, most recently closed first."""
    lines = ["## Completed", ""]
    if not completed:
        return [*lines, "_None._", ""]
    lines.append("| Epic | Closed | Tasks | Repos | Documents |")
    lines.append("| --- | --- | --- | --- | --- |")
    newest = sorted(completed, key=lambda e: (e.closed, e.number), reverse=True)
    lines += [_completed_row(epic) for epic in newest]
    lines.append("")
    return lines


def render(
    summaries: list[EpicSummary], completed: list[CompletedEpic], org: str | None = None
) -> str:
    """Render the roadmap: In flight, Planned, then Completed (newest first).

    An open epic is in flight once any of its tasks has closed, and planned
    until then. An empty section says so.
    """
    source = f"{org}/.github" if org else "the org .github repo"
    lines = [
        "# Roadmap",
        "",
        f"_Generated from the epics in {source}. Do not edit by hand._",
        "",
    ]
    lines += _open_section("In flight", [e for e in summaries if e.closed > 0])
    lines += _open_section("Planned", [e for e in summaries if e.closed == 0])
    lines += _completed_section(completed)
    return "\n".join(lines)
