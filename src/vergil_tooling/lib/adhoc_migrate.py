"""One-shot migration: relocate per-repo standing epics into the org ``.github``.

Standing epics used to live per-repo as ``Epic (standing): Ad-hoc maintenance``.
The ad-hoc model (epic vergil-project/.github#85) centralizes them: one
``Epic (ad hoc): <repo>`` per repo, in ``<org>/.github``. This module finds the
old standing epics, re-links their **open** child tasks under the new ``.github``
ad-hoc epic, and closes the old standing epic. Closed children stay with the
retired epic for history.

Idempotent and safe to re-run: a closed standing epic drops out of the
open-epic search, so a second pass reprocesses nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import epics, github


@dataclass(frozen=True)
class Relocation:
    """A standing epic and the open children to move under its ad-hoc replacement."""

    standing: epics.IssueRef
    open_children: tuple[epics.IssueRef, ...]

    @property
    def target_repo(self) -> str:
        """The repo whose ad-hoc epic (in ``.github``) replaces this standing epic."""
        return f"{self.standing.owner}/{self.standing.repo}"


def find_standing_epics(org: str) -> list[epics.IssueRef]:
    """Open ``epic``+``standing`` issues across *org* — the epics to relocate."""
    raw: Any = github.read_json(
        "search",
        "issues",
        "--owner",
        org,
        "--label",
        "epic",
        "--label",
        "standing",
        "--state",
        "open",
        "--json",
        "number,repository",
    )
    refs: list[epics.IssueRef] = []
    for item in raw if isinstance(raw, list) else []:
        name_with_owner = str((item.get("repository") or {}).get("nameWithOwner", ""))
        if "/" not in name_with_owner:
            continue
        owner, name = name_with_owner.split("/", 1)
        refs.append(epics.IssueRef(owner=owner, repo=name, number=int(item["number"])))
    return refs


def plan(org: str) -> list[Relocation]:
    """Build the relocation plan: each standing epic paired with its OPEN children."""
    relocations: list[Relocation] = []
    for standing in find_standing_epics(org):
        open_children = tuple(
            child.ref for child in epics.child_states(standing) if child.state == "OPEN"
        )
        relocations.append(Relocation(standing=standing, open_children=open_children))
    return relocations


_CLOSE_COMMENT = (
    "Migrated to {adhoc} — ad-hoc epics now live in the org `.github` (epic "
    "vergil-project/.github#85). Open children were re-linked there; closed "
    "children stay here for history."
)


def apply_one(reloc: Relocation) -> str:
    """Execute one relocation and return a summary line.

    Ensures the target ad-hoc epic in ``.github``, reparents each open child under
    it (unlink from the standing epic, link to the ad-hoc epic), then closes the
    standing epic with a pointer comment.
    """
    adhoc = epics.ensure_adhoc_epic(reloc.target_repo)
    for child in reloc.open_children:
        epics.remove_child(reloc.standing, child)
        epics.add_child(adhoc, child)
    github.run(
        "issue",
        "close",
        str(reloc.standing.number),
        "--repo",
        reloc.target_repo,
        "--comment",
        _CLOSE_COMMENT.format(adhoc=adhoc.slug),
    )
    moved = len(reloc.open_children)
    return f"{reloc.standing.slug} → {adhoc.slug} ({moved} open child(ren) moved)"


def render_plan(relocations: list[Relocation], *, org: str) -> str:
    """Dry-run report: what ``--apply`` would do, changing nothing."""
    header = [
        f"# Ad-hoc migration — dry run (**{org}**)",
        "",
        "_Preview only; nothing changed. Run `--apply` (as a human) to execute._",
        "",
    ]
    if not relocations:
        return "\n".join([*header, "_No standing epics found — nothing to migrate._", ""])
    lines = [*header, "## Planned relocations", ""]
    for reloc in sorted(relocations, key=lambda r: r.standing.slug):
        adhoc_title = f"Epic (ad hoc): {reloc.standing.repo}"
        lines.append(
            f"- {reloc.standing.slug} → `{reloc.standing.owner}/.github` "
            f"«{adhoc_title}» — move {len(reloc.open_children)} open child(ren), "
            "then close the standing epic"
        )
        lines += [f"    - {child.slug}" for child in reloc.open_children]
    lines.append("")
    return "\n".join(lines)


def render_applied(summaries: list[str], *, org: str) -> str:
    """Summary of an ``--apply`` run: what was actually relocated."""
    header = [f"# Ad-hoc migration — applied (**{org}**)", ""]
    if not summaries:
        return "\n".join([*header, "_No standing epics found — nothing migrated._", ""])
    lines = [*header, "## Relocated", "", *(f"- {summary}" for summary in summaries), ""]
    return "\n".join(lines)
