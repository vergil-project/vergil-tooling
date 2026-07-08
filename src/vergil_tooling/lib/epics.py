"""Epic <-> task umbrella relationship, mechanism-agnostic.

An *epic* is an umbrella over *task* issues that may live in other repos within
the same org. The link is GitHub **native sub-issues** where available, with a
portable **cross-repo reference fallback** — a ``Parent: <owner>/<repo>#<N>``
line in the task body — for forges (Forgejo/Codeberg) that lack sub-issues. All
consumers (the finalize close+rollup, the roadmap generator) speak this module's
``IssueRef`` vocabulary, never the underlying mechanism.

Node-id resolution and issue state use REST (``gh api``); the parent/children
traversal and the link mutation use GraphQL (``github.graphql``). The unit tests
mock the ``github`` boundary; real GraphQL/REST correctness is exercised in use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from vergil_tooling.lib import github


@dataclass(frozen=True)
class IssueRef:
    """A cross-repo issue coordinate."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}#{self.number}"


@dataclass(frozen=True)
class ChildState:
    """A child task and whether it is open or closed."""

    ref: IssueRef
    state: str  # "OPEN" | "CLOSED"


_PARENT_RE = re.compile(
    r"^\s*Parent:\s*([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)\s*$",
    re.MULTILINE,
)

_BLOCKED_BY_RE = re.compile(
    r"^\s*Blocked-by:\s*([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)#(\d+)\s*$",
    re.MULTILINE,
)

_REF_RE = re.compile(r"^(?:([A-Za-z0-9._-]+/[A-Za-z0-9._-]+))?#([0-9]+)$")


def parse_issue_ref(ref: str, *, default_repo: str) -> IssueRef:
    """Parse a linkage ref (``"#42"`` or ``"owner/repo#42"``) into an ``IssueRef``.

    A bare ``#N`` uses *default_repo* (``"owner/name"``). Raises ``ValueError`` if
    *ref* is malformed or the resolved repo lacks an ``owner/name`` form.
    """
    match = _REF_RE.match(ref.strip())
    if match is None:
        raise ValueError(f"not an issue ref: {ref!r}")
    repo_part, number = match.groups()
    full = repo_part or default_repo
    if "/" not in full:
        raise ValueError(f"cannot resolve repo for {ref!r} (default_repo={default_repo!r})")
    owner, name = full.split("/", 1)
    return IssueRef(owner=owner, repo=name, number=int(number))


def single_target_org(*refs: IssueRef) -> str:
    """Return the single owner shared by *refs*, or raise on a cross-org span.

    Commands that mint a GitHub App token for an explicit target select the
    installation by owner, and one token cannot reach two owners. When an
    operation names refs under different owners (e.g. an epic and a task in
    different orgs) that is out of scope: fail clearly here rather than mint a
    token for one owner and hit a cryptic ``403`` on the other (issue #2070).
    """
    owners = {ref.owner for ref in refs}
    if len(owners) != 1:
        joined = ", ".join(sorted(owners))
        raise ValueError(
            f"cross-org operation is out of scope: refs span multiple owners ({joined})"
        )
    return next(iter(owners))


_SUBISSUES_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      subIssues(first: 100) {
        nodes { number state repository { name owner { login } } }
      }
    }
  }
}
"""

_PARENT_QUERY = """
query($id: ID!) {
  node(id: $id) {
    ... on Issue {
      parent { number repository { name owner { login } } }
    }
  }
}
"""

_ADD_SUBISSUE = """
mutation($parent: ID!, $child: ID!) {
  addSubIssue(input: {issueId: $parent, subIssueId: $child}) {
    subIssue { number }
  }
}
"""

_REMOVE_SUBISSUE = """
mutation($parent: ID!, $child: ID!) {
  removeSubIssue(input: {issueId: $parent, subIssueId: $child}) {
    subIssue { number }
  }
}
"""


def _issue_endpoint(ref: IssueRef) -> str:
    return f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}"


def _node_id(ref: IssueRef) -> str:
    """Resolve an issue's GraphQL global node id via REST."""
    return github.read_output("api", _issue_endpoint(ref), "--jq", ".node_id")


def _issue_state(ref: IssueRef) -> str:
    """Return ``"OPEN"`` or ``"CLOSED"`` for an issue."""
    return github.read_output("api", _issue_endpoint(ref), "--jq", ".state").upper()


def _ref_from_node(node: Any) -> IssueRef:
    """Build an ``IssueRef`` from a GraphQL issue node (number + repository)."""
    repo = node["repository"]
    return IssueRef(
        owner=str(repo["owner"]["login"]), repo=str(repo["name"]), number=int(node["number"])
    )


def _native_child_states(epic: IssueRef) -> list[ChildState]:
    data: Any = github.graphql(_SUBISSUES_QUERY, id=_node_id(epic))
    nodes = (((data or {}).get("node") or {}).get("subIssues") or {}).get("nodes") or []
    return [ChildState(ref=_ref_from_node(n), state=str(n["state"]).upper()) for n in nodes]


def _reflink_child_states(epic: IssueRef) -> list[ChildState]:
    """Portable fallback: issues whose body references this epic as ``Parent:``."""
    results: Any = github.read_json(
        "search", "issues", f"Parent: {epic.slug}", "--json", "number,state,repository"
    )
    states: list[ChildState] = []
    for item in results if isinstance(results, list) else []:
        name_with_owner = str((item.get("repository") or {}).get("nameWithOwner", ""))
        if "/" not in name_with_owner:
            continue
        owner, name = name_with_owner.split("/", 1)
        states.append(
            ChildState(
                ref=IssueRef(owner=owner, repo=name, number=int(item["number"])),
                state=str(item["state"]).upper(),
            )
        )
    return states


def child_states(epic: IssueRef) -> list[ChildState]:
    """All child tasks of *epic*: native sub-issues preferred, reflink fallback."""
    native = _native_child_states(epic)
    return native if native else _reflink_child_states(epic)


def render_blocked_by(deps: list[IssueRef]) -> str:
    """Render the ``Blocked-by:`` reflink lines for a validation task body.

    One ``Blocked-by: owner/repo#N`` line per dependency — the portable mirror of
    the ``Parent:`` sub-issue reflink. Empty *deps* yields the empty string.
    """
    return "".join(f"Blocked-by: {dep.slug}\n" for dep in deps)


def blockers_of(task: IssueRef) -> list[IssueRef]:
    """Deps *task* is blocked by, parsed from its ``Blocked-by:`` body reflinks.

    Storage is the portable body reflink — the mirror of the ``Parent:``
    sub-issue fallback — chosen by the storage spike (#2184) because GitHub's
    native issue dependencies are REST-only and out of the sanctioned tooling's
    reach. If native dependencies become reachable later, this is the one place
    that would prefer them over the reflink.
    """
    body = github.read_output("api", _issue_endpoint(task), "--jq", ".body") or ""
    return [
        IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))
        for match in _BLOCKED_BY_RE.finditer(body)
    ]


def all_blockers_closed(task: IssueRef) -> bool:
    """True iff every blocker of *task* is CLOSED.

    No blockers means nothing holds *task*, so it is runnable — the empty case is
    vacuously True. Used by the validation-aware rollup to classify an open
    validation task as runnable (blockers closed) vs blocked.
    """
    return all(_issue_state(dep) == "CLOSED" for dep in blockers_of(task))


def parent_of(task: IssueRef) -> IssueRef | None:
    """The epic *task* belongs to: native parent preferred, reflink fallback."""
    data: Any = github.graphql(_PARENT_QUERY, id=_node_id(task))
    parent = ((data or {}).get("node") or {}).get("parent")
    if isinstance(parent, dict):
        return _ref_from_node(parent)
    body = github.read_output("api", _issue_endpoint(task), "--jq", ".body")
    match = _PARENT_RE.search(body or "")
    if match:
        return IssueRef(owner=match.group(1), repo=match.group(2), number=int(match.group(3)))
    return None


def add_child(epic: IssueRef, task: IssueRef) -> None:
    """Link *task* under *epic*. Reopen the epic first if it is closed.

    Adding a task to an already-closed finite epic must reopen it (the
    reopen-on-late-child rule); the later finalize rollup closes it again.
    """
    if _issue_state(epic) == "CLOSED":
        github.run("issue", "reopen", str(epic.number), "--repo", f"{epic.owner}/{epic.repo}")
    github.graphql(_ADD_SUBISSUE, parent=_node_id(epic), child=_node_id(task))


def remove_child(epic: IssueRef, task: IssueRef) -> None:
    """Unlink *task* from *epic* (remove the native sub-issue relationship)."""
    github.graphql(_REMOVE_SUBISSUE, parent=_node_id(epic), child=_node_id(task))


def all_children_closed(epic: IssueRef) -> bool:
    """True iff *epic* has at least one child and all children are closed."""
    children = child_states(epic)
    return bool(children) and all(child.state == "CLOSED" for child in children)


def _labels(ref: IssueRef) -> set[str]:
    raw: Any = github.read_json(
        "issue", "view", str(ref.number), "--repo", f"{ref.owner}/{ref.repo}", "--json", "labels"
    )
    labels = (raw or {}).get("labels") or [] if isinstance(raw, dict) else []
    return {str(label.get("name", "")) for label in labels}


def is_epic(ref: IssueRef) -> bool:
    """True if *ref* carries the ``epic`` label (i.e. it is in the model)."""
    return "epic" in _labels(ref)


def resolve_epic_ref(ref: str, *, repo: str) -> IssueRef:
    """Resolve an epic ref, accepting the ``"adhoc"`` sentinel.

    ``"adhoc"`` (and the deprecated alias ``"standing"``) ensures *repo*'s ad-hoc
    epic exists in ``<org>/.github`` — creating it if absent and reusing it
    otherwise — via :func:`ensure_adhoc_epic`. Any other ref is parsed with
    :func:`parse_issue_ref` and validated to carry the ``epic`` label.
    """
    if ref in ("adhoc", "standing"):
        return ensure_adhoc_epic(repo)
    epic = parse_issue_ref(ref, default_repo=repo)
    if not is_epic(epic):
        raise ValueError(f"{epic.slug} is not an epic (missing the 'epic' label)")
    return epic


_ADHOC_EPIC_TITLE_PREFIX = "Epic (ad hoc): "
_ADHOC_EPIC_LABELS = ("epic", "ad-hoc")
_ADHOC_EPIC_BODY = (
    "Perpetual umbrella for ad-hoc work in {repo}. Created and reused "
    "idempotently; tasks routed to the ad-hoc epic are linked here.\n"
)


def ensure_adhoc_epic(target_repo: str) -> IssueRef:
    """Return *target_repo*'s ad-hoc epic in ``<org>/.github``, creating it if absent.

    All ad-hoc epics live in the org's ``.github`` repo, one per repo, each
    disambiguated by the title ``Epic (ad hoc): <bare repo name>`` and labelled
    ``epic`` + ``ad-hoc``. Idempotent: an existing epic with that title is
    reused; none means create it; two sharing the title is ambiguous and an
    error names an explicit ref instead of guessing. Applies to member repos and
    ``.github`` itself alike.
    """
    if "/" not in target_repo:
        raise ValueError(f"cannot resolve repo for ad-hoc epic (repo={target_repo!r})")
    owner, bare = target_repo.split("/", 1)
    dotgithub = f"{owner}/.github"
    title = f"{_ADHOC_EPIC_TITLE_PREFIX}{bare}"
    raw: Any = github.read_json(
        "issue",
        "list",
        "--repo",
        dotgithub,
        "--label",
        "epic",
        "--label",
        "ad-hoc",
        "--state",
        "open",
        "--json",
        "number,title",
    )
    rows = (
        [r for r in raw if isinstance(r, dict) and r.get("title") == title]
        if isinstance(raw, list)
        else []
    )
    if len(rows) > 1:
        nums = ", ".join(f"#{r['number']}" for r in rows)
        raise ValueError(
            f"multiple ad-hoc epics titled {title!r} in {dotgithub} ({nums}) — "
            "pass an explicit --epic"
        )
    if rows:
        return IssueRef(owner=owner, repo=".github", number=int(rows[0]["number"]))
    url = github.create_issue(
        repo=dotgithub,
        title=title,
        body=_ADHOC_EPIC_BODY.format(repo=target_repo),
        labels=list(_ADHOC_EPIC_LABELS),
    )
    number = int(url.rstrip("/").rsplit("/", 1)[-1])
    return IssueRef(owner=owner, repo=".github", number=number)


# Deprecated alias kept for the ad-hoc rollout window (epic #85); callers still
# importing ``ensure_standing_epic`` keep working. Removed in the retire-standing
# task once the migration is complete.
ensure_standing_epic = ensure_adhoc_epic


def is_epic_linkage(ref: str, *, default_repo: str) -> bool:
    """True if *ref* points at an epic, so it must not be linked as a PR's task.

    Single source of truth for "is this linkage an epic?", shared by
    ``vrg-submit-pr`` and ``vrg-pr-workflow report-ready``. Self-scoping: an
    unparseable ref (e.g. a legacy issue with no resolvable repo) is never an
    epic and returns False.
    """
    try:
        issue = parse_issue_ref(ref, default_repo=default_repo)
    except ValueError:
        return False
    return is_epic(issue)


# Labels marking a not-PR-workable *operational task* — one that is run and whose
# acceptance is a recorded ``Outcome:`` comment, not a merged PR. Extended as new
# operational kinds are added (e.g. ``deployment``).
_OPERATIONAL_LABELS: set[str] = {"validation"}


def is_operational(ref: IssueRef) -> bool:
    """True if *ref* carries any operational label (validation, deployment, …)."""
    return bool(_labels(ref) & _OPERATIONAL_LABELS)


def operational_kind(ref: IssueRef) -> str | None:
    """The operational label on *ref* (``"validation"`` / ``"deployment"``), or None."""
    kinds = _labels(ref) & _OPERATIONAL_LABELS
    return next(iter(kinds)) if kinds else None


def is_operational_task(ref: str, *, default_repo: str) -> bool:
    """True if *ref* is an operational task, so PR tooling must refuse it.

    Single source of truth for "is this an operational task?", shared by
    ``vrg-submit-pr`` and ``vrg-pr-workflow report-ready``. An operational task
    (validation, deployment, …) is proven by *running* it and recording an
    ``Outcome:`` comment — it has no code PR — so the PR path is refused before
    any work begins. Self-scoping: an unparseable ref (e.g. a legacy issue with
    no resolvable repo) is never operational and returns False.
    """
    try:
        issue = parse_issue_ref(ref, default_repo=default_repo)
    except ValueError:
        return False
    return is_operational(issue)


def rollup(task: IssueRef) -> None:
    """Close *task*'s parent epic if the epic is finite and all children closed.

    A no-op unless the task has an ``epic``-labeled parent (the transition gate):
    legacy issues have no epic parent, so finalize never rolls them up. An
    ``ad-hoc`` epic (or its deprecated ``standing`` alias) is perpetual and never
    auto-closes.
    """
    parent = parent_of(task)
    if parent is None or not is_epic(parent):
        return
    if _labels(parent) & {"ad-hoc", "standing"}:
        return
    if all_children_closed(parent):
        print(f"Rolling up epic {parent.slug} — all child tasks closed.")
        github.run("issue", "close", str(parent.number), "--repo", f"{parent.owner}/{parent.repo}")
