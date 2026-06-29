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


def all_children_closed(epic: IssueRef) -> bool:
    """True iff *epic* has at least one child and all children are closed."""
    children = child_states(epic)
    return bool(children) and all(child.state == "CLOSED" for child in children)
