"""The planning issue an epic's documentation-task PR also closes.

``epic-create`` tracks each epic's planning in a **planning issue**, an open
child of the planning queue epic, and hands the epic's spec and plan off as
the PR of its **documentation task**. The docs PR's merge is the moment the
planning is done, so that PR also closes the planning issue
(vergil-project/.github#950). Nothing has to come back later to close it.

:func:`find` decides, for the one issue a PR closes, whether it is an epic's
documentation task and, if so, which planning issue the PR also closes:

1. The task's epic is its parent (:func:`epics.parent_of`). The epic's body
   names its planning issue on a ``Planning: <owner>/<repo>#<M>`` line, which
   ``epic-create`` writes when it creates the epic.
2. The task is that epic's documentation task when the planning issue's latest
   ``Docs task:`` marker names it. Only comments by trusted authors count
   (:func:`epic_size.trusted_logins`, a leading ``app/`` stripped), since a
   planning issue is public and anyone can comment on it.
3. The planning issue is closed only when it is in the task's own repo (a PR
   closes only same-repo issues), is open, its native parent is the planning
   queue epic, and no security advisory still tracks it
   (:func:`queue_view.tracking_advisories`, the planning view's hold): closing
   it then would close the advisory with its fix unwritten. Otherwise the
   result says which condition failed, and the caller reports it.

Any other task (no ``Planning:`` line in its epic, or a planning issue whose
``Docs task:`` marker names another task) gets None, and nothing is printed for
it. A failed GitHub or trusted-author read propagates: it is never read as
"not a documentation task".

The close is rendered as a separate ``Planning issue: Closes <ref>`` line in
the PR body (:func:`vergil_tooling.lib.pr_body.build_pr_body`). It does not
begin with the linkage keyword, so the task-linkage parsers
(:func:`vergil_tooling.lib.linkage.extract_tracking_refs`) do not count it, and
a docs PR is never read as a batch PR. GitHub still closes the issue on merge
(its closing keywords match anywhere in the body), and ``vrg-finalize-pr``
closes any closing reference GitHub left open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import epic_size, epics, github, queue_view
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.queue_view import PLANNING_MARKER, parse_planning_markers

_PLANNING_LINE_RE = re.compile(
    rf"^\s*{re.escape(PLANNING_MARKER)}\s*([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#([0-9]+)\s*$",
    re.MULTILINE,
)
_APP_PREFIX = "app/"


@dataclass(frozen=True)
class PlanningClose:
    """The planning issue of a documentation task, and whether its PR closes it.

    ``skipped`` is None when the PR closes ``planning``; otherwise it says why
    it does not, for the caller to report.
    """

    task: IssueRef
    planning: IssueRef
    skipped: str | None

    @property
    def closes(self) -> bool:
        return self.skipped is None


def _same(a: IssueRef, b: IssueRef) -> bool:
    return a.slug.lower() == b.slug.lower()


def _same_repo(a: IssueRef, b: IssueRef) -> bool:
    return f"{a.owner}/{a.repo}".lower() == f"{b.owner}/{b.repo}".lower()


def planning_refs(body: str) -> list[IssueRef]:
    """Every distinct planning issue *body*'s ``Planning:`` lines name, in body order."""
    found: dict[str, IssueRef] = {}
    for m in _PLANNING_LINE_RE.finditer(body):
        ref = IssueRef(owner=m.group(1), repo=m.group(2), number=int(m.group(3)))
        found.setdefault(ref.slug.lower(), ref)
    return list(found.values())


def _view(ref: IssueRef) -> dict[str, Any]:
    raw = github.read_json(
        "issue",
        "view",
        str(ref.number),
        "--repo",
        f"{ref.owner}/{ref.repo}",
        "--json",
        "state,body,comments",
    )
    if not isinstance(raw, dict):
        raise RuntimeError(f"unexpected 'gh issue view' response for {ref.slug}: {raw!r}")
    return raw


def _trusted_bodies(view: dict[str, Any], trusted: frozenset[str]) -> list[str]:
    bodies: list[str] = []
    for comment in view.get("comments") or []:
        login = str((comment.get("author") or {}).get("login") or "")
        if login.removeprefix(_APP_PREFIX) in trusted:
            bodies.append(str(comment.get("body") or ""))
    return bodies


def find(task: IssueRef) -> PlanningClose | None:
    """The planning issue *task*'s PR also closes, or None when *task* is not a docs task.

    See the module docstring for the rules. Raises on any failed read.
    """
    epic = epics.parent_of(task)
    if epic is None:
        return None
    body = github.read_output(
        "api", f"repos/{epic.owner}/{epic.repo}/issues/{epic.number}", "--jq", ".body"
    )
    candidates = planning_refs(body or "")
    if not candidates:
        return None
    trusted = epic_size.trusted_logins()
    matches: list[tuple[IssueRef, dict[str, Any]]] = []
    for planning in candidates:
        view = _view(planning)
        docs_task = parse_planning_markers(_trusted_bodies(view, trusted)).docs_task
        if docs_task is not None and _same(docs_task, task):
            matches.append((planning, view))
    if not matches:
        return None
    planning, view = matches[0]
    if len(matches) > 1:
        names = ", ".join(ref.slug for ref, _ in matches)
        return PlanningClose(
            task, planning, f"more than one planning issue names it as their docs task ({names})"
        )
    if not _same_repo(planning, task):
        return PlanningClose(
            task, planning, "it is in another repo, and a PR closes only issues in its own repo"
        )
    state = str(view.get("state") or "").upper()
    if state != "OPEN":
        return PlanningClose(task, planning, f"it is already {state.lower() or 'in no state'}")
    parent = epics.native_parent_of(planning)
    if parent is None or epics.queue_kind(parent) != epics.PLANNING.key:
        where = "no parent" if parent is None else f"parent {parent.slug}"
        return PlanningClose(task, planning, f"it is not in the planning queue ({where})")
    held = queue_view.tracking_advisories(planning, str(view.get("body") or ""))
    if held:
        tracks = "advisory still tracks" if len(held) == 1 else "advisories still track"
        return PlanningClose(
            task,
            planning,
            f"{len(held)} {tracks} it ({', '.join(held)}); retarget each to the task or "
            "tasks that fix it with vrg-security-record retarget, then close the planning "
            "issue by hand",
        )
    return PlanningClose(task, planning, None)


def describe(result: PlanningClose) -> str:
    """One line saying what the PR does with *result*'s planning issue."""
    if result.closes:
        return (
            f"{result.task.slug} is the documentation task of planning issue "
            f"{result.planning.slug}; the PR also closes it on merge."
        )
    return (
        f"{result.task.slug} is the documentation task of planning issue "
        f"{result.planning.slug}, but the PR does not close it: {result.skipped}."
    )
