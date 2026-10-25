"""Deterministic routing of an intake item (epic vergil-project/.github#176).

Intake review decides, per item, one of five routes; this module owns the
mechanics so the skill never hand-rolls them:

- **drop** — comment the reason and close (not planned).
- **defer** — add a dated comment; nothing else changes, so the item keeps its
  age and comes up first next review.
- **one-off** — move the item into its target repo and link it under that
  repo's ad-hoc epic (or a named finite epic). A move *transfers* the issue,
  keeping its history; when GitHub refuses the transfer it falls back to
  recreating the item in the target repo with a link back and closing the
  original with a pointer — and says so on stderr. A transfer carries the
  item's intake parent across (sometimes only after a delay), so the moved
  issue is *reparented* with ``replaceParent`` and its final parent verified
  after a settle delay, replaced again if the old parent came back late
  (#3556). A recreated issue starts unparented and is simply linked. Either
  way, a final parent other than the target epic fails loudly. A one-off whose
  work lands in the item's own repo is just reparented.
- **planning** — reparent from the intake epic to the planning epic of the
  same queue home.
- **split** — the replacement tasks already exist (made with
  ``vrg-issue-create``); comment the links and the reason, then close.

Only an intake child may be routed (``planning`` also accepts an unparented
issue: the ``epic-create`` free-text path). A private repo's item is never
routed to a public repo or epic. The whole route runs under the item owner's
App token (:func:`github.target_org`).
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.labels import RETIRED_LABELS, ensure_labels, load_labels
from vergil_tooling.lib.process_errors import error_text

DROP_COMMENT = "Dropped in intake review: {reason}"
DEFER_COMMENT = "Deferred {day}: {reason}"
SPLIT_COMMENT = "Split into: {refs}. {reason}"
MOVED_TO_COMMENT = "Moved to {url}"
MOVED_FROM_LINE = "Moved from {url}"

# A transfer carries the intake parent over eventually, not atomically: a link
# made right after it can be overwritten seconds later (#3556). Each settle
# round replaces the parent, waits, then re-reads it.
_SETTLE_ROUNDS = 4
_SETTLE_SECONDS = 3.0

_ISSUE_URL_RE = re.compile(r"https?://[^/\s]+/([^/\s]+)/([^/\s]+)/issues/([0-9]+)")


class RouteRefusedError(ValueError):
    """A route that must not run: wrong queue, cross-org, or private → public."""


@dataclass(frozen=True)
class Drop:
    reason: str


@dataclass(frozen=True)
class Defer:
    reason: str


@dataclass(frozen=True)
class OneOff:
    repo: str  # target "owner/repo"
    epic: str = "adhoc"  # 'adhoc', '#N' (target repo's epic home), or 'owner/repo#N'


@dataclass(frozen=True)
class ToPlanning:
    pass


@dataclass(frozen=True)
class Split:
    into: tuple[IssueRef, ...]
    reason: str


RouteAction = Drop | Defer | OneOff | ToPlanning | Split


@dataclass(frozen=True)
class RouteResult:
    """Where the item ended up and how it got there."""

    final: IssueRef
    moved_by: Literal["none", "transfer", "recreate"]


def route(ref: IssueRef, action: RouteAction, *, today: date | None = None) -> RouteResult:
    """Apply *action* to the intake item *ref*; raise :class:`RouteRefusedError` to refuse."""
    with github.target_org(ref.owner):
        return _route(ref, action, today or date.today())


def _repo_of(ref: IssueRef) -> str:
    return f"{ref.owner}/{ref.repo}"


def _route(ref: IssueRef, action: RouteAction, today: date) -> RouteResult:
    parent = epics.parent_of(ref)
    _check_eligible(ref, action, parent)
    if isinstance(action, Drop):
        _close(ref, DROP_COMMENT.format(reason=action.reason), reason="not planned")
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, Defer):
        _comment(ref, DEFER_COMMENT.format(day=today.isoformat(), reason=action.reason))
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, Split):
        if not action.into:
            raise RouteRefusedError("split needs at least one --into task")
        refs = ", ".join(task.slug for task in action.into)
        _close(ref, SPLIT_COMMENT.format(refs=refs, reason=action.reason), reason="completed")
        return RouteResult(final=ref, moved_by="none")
    if isinstance(action, ToPlanning):
        planning = epics.ensure_planning_epic(_repo_of(ref))
        _guard_visibility(ref, _repo_of(planning))
        _link(planning, ref)
        return RouteResult(final=ref, moved_by="none")
    return _one_off(ref, action)


def _check_eligible(ref: IssueRef, action: RouteAction, parent: IssueRef | None) -> None:
    """Refuse unless *ref* is an intake child (or unparented, for ``planning``)."""
    if parent is None:
        if isinstance(action, ToPlanning):
            return
        raise RouteRefusedError(
            f"{ref.slug} has no parent; only intake items can be routed "
            "(the planning route also accepts an unparented issue)"
        )
    if epics.queue_kind(parent) == "intake":
        return
    raise RouteRefusedError(
        f"{ref.slug} is under {parent.slug}, not an intake epic; only intake items can be routed"
    )


def _guard_visibility(ref: IssueRef, *targets: str) -> None:
    """Refuse routing a private repo's item into any public *targets* repo."""
    source = _repo_of(ref)
    others = [target for target in dict.fromkeys(targets) if target != source]
    if not others or github.is_public(source):
        return
    public = [target for target in others if github.is_public(target)]
    if public:
        raise RouteRefusedError(
            f"{ref.slug} is in private repo {source}; refusing to route it to public "
            f"{', '.join(public)}"
        )


def _one_off(ref: IssueRef, action: OneOff) -> RouteResult:
    source = _repo_of(ref)
    dest = action.repo
    if dest.count("/") != 1 or not all(dest.split("/")):
        raise RouteRefusedError(f"one-off --repo must be owner/repo, got {dest!r}")
    if dest.split("/", 1)[0] != ref.owner:
        raise RouteRefusedError(f"cross-org operation is out of scope: {ref.slug} -> {dest}")
    if action.epic != "adhoc" and "/" in action.epic:
        epic_owner = epics.parse_issue_ref(action.epic, default_repo=dest).owner
        if epic_owner != ref.owner:
            raise RouteRefusedError(
                f"cross-org operation is out of scope: {ref.slug} -> epic {action.epic}"
            )
    # Check the target repo before resolving the epic: resolving 'adhoc' may
    # create the target's ad-hoc epic, which must not happen for a refused route.
    _guard_visibility(ref, dest)
    epic = epics.resolve_epic_ref(action.epic, repo=dest)
    _guard_visibility(ref, _repo_of(epic))
    if epics.queue_kind(epic) is not None:
        raise RouteRefusedError(
            f"{epic.slug} is a queue epic; a one-off goes under an ad-hoc or finite epic "
            "(use the planning route for planning)"
        )
    if dest == source:
        _link(epic, ref)
        return RouteResult(final=ref, moved_by="none")
    try:
        output = github.read_output("issue", "transfer", str(ref.number), dest, "--repo", source)
    except github.MissingGitHubTokenError:
        raise
    except subprocess.CalledProcessError as exc:
        new = _recreate(ref, dest, exc)
        # A recreated issue has no parent to carry over: link it, then check.
        _link(epic, new)
        current = epics.native_parent_of(new)
        if current != epic:
            raise _parent_mismatch(epic, new, current) from None
        return RouteResult(final=new, moved_by="recreate")
    final = _transferred_ref(ref, dest, epic, output)
    _settle_parent(epic, final)
    return RouteResult(final=final, moved_by="transfer")


def _settle_parent(epic: IssueRef, task: IssueRef) -> None:
    """Make *epic* the transferred *task*'s parent, robust to a late carry-over.

    The transfer keeps the intake parent, but it may surface only after a first
    read, so every round replaces (never plainly adds) the parent, waits, and
    verifies; a parent that reverts is replaced again. Raise if it never holds.
    """
    current: IssueRef | None = None
    for attempt in range(1, _SETTLE_ROUNDS + 1):
        _link(epic, task, replace=True)
        time.sleep(_SETTLE_SECONDS)
        current = epics.native_parent_of(task)
        if current == epic:
            return
        print(
            f"intake-route: {task.slug} is {_placement(current)} after linking it "
            f"under {epic.slug} (round {attempt}/{_SETTLE_ROUNDS}); replacing the parent",
            file=sys.stderr,
        )
    raise _parent_mismatch(epic, task, current)


def _placement(parent: IssueRef | None) -> str:
    return f"under {parent.slug}" if parent is not None else "unparented"


def _parent_mismatch(epic: IssueRef, task: IssueRef, current: IssueRef | None) -> RuntimeError:
    """The loud failure for a move whose final parent is not *epic*."""
    return RuntimeError(
        f"moved the item to {task.slug}, but it is {_placement(current)}, "
        f"not under {epic.slug}; the route is half-applied. Fix it with: "
        f"vrg-epic-move --task {task.slug} --epic {epic.slug}"
    )


def _issue_ref_from_url(output: str, *, failure: str) -> IssueRef:
    """The issue a ``gh`` command's *output* names by URL; raise with *failure* if none."""
    match = _ISSUE_URL_RE.search(output)
    if match is None:
        raise RuntimeError(f"{failure}: could not read the new issue URL from {output!r}")
    return IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))


def _transferred_ref(ref: IssueRef, dest: str, epic: IssueRef, output: str) -> IssueRef:
    return _issue_ref_from_url(
        output,
        failure=f"transferred {ref.slug} to {dest} (link it under {epic.slug} by hand)",
    )


def _recreate(ref: IssueRef, dest: str, exc: subprocess.CalledProcessError) -> IssueRef:
    """Copy *ref* into *dest* with a link back, then close the original with a pointer."""
    source = _repo_of(ref)
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", source, "--json", "title,body,labels,url"
    )
    names = [str(label.get("name", "")) for label in raw.get("labels") or []]
    registry = {entry["name"] for entry in load_labels()["labels"]}
    kept = [name for name in names if name not in RETIRED_LABELS]
    carried = [name for name in kept if name in registry]
    uncarried = [name for name in kept if name not in registry]
    if carried:
        ensure_labels(dest, carried)
    original_url = str(raw["url"])
    moved_from = MOVED_FROM_LINE.format(url=original_url)
    body = (raw.get("body") or "").rstrip()
    new_url = github.create_issue(
        repo=dest,
        title=str(raw["title"]),
        body=f"{body}\n\n{moved_from}\n" if body else f"{moved_from}\n",
        labels=carried,
    )
    new = _issue_ref_from_url(
        new_url, failure=f"recreated {ref.slug} in {dest} (original left open)"
    )
    _close(ref, MOVED_TO_COMMENT.format(url=new_url), reason="not planned")
    detail = " ".join(error_text(exc).split())
    print(
        f"intake-route: {ref.slug} -> {dest}: transfer failed ({detail}); "
        f"recreated as {new_url}, original closed with a pointer",
        file=sys.stderr,
    )
    if uncarried:
        print(
            f"intake-route: label(s) not carried to {new.slug} (not in the label registry): "
            f"{', '.join(uncarried)}",
            file=sys.stderr,
        )
    return new


def _link(epic: IssueRef, task: IssueRef, *, replace: bool = False) -> None:
    """Link *task* under *epic*, replacing any current parent in one atomic call.

    With *replace*, an apparently unparented *task* is still linked with
    ``replaceParent``, so a parent that surfaces between the read and the link
    is replaced rather than refusing the link ("Sub issue may only have one
    parent").
    """
    current = epics.native_parent_of(task)
    if current == epic:
        return
    if current is None and not replace:
        epics.add_child(epic, task)  # reopens a closed epic itself
        return
    state = github.read_output(
        "api", f"repos/{epic.owner}/{epic.repo}/issues/{epic.number}", "--jq", ".state"
    )
    if state.lower() == "closed":
        github.run("issue", "reopen", str(epic.number), "--repo", _repo_of(epic))
    epics.reparent_child(epic, task)


def _comment(ref: IssueRef, body: str) -> None:
    github.run("issue", "comment", str(ref.number), "--repo", _repo_of(ref), "--body", body)


def _close(ref: IssueRef, comment: str, *, reason: str) -> None:
    github.run(
        "issue",
        "close",
        str(ref.number),
        "--repo",
        _repo_of(ref),
        "--reason",
        reason,
        "--comment",
        comment,
    )
