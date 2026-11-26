"""An in-memory GitHub sub-issue graph, for the tests of the chain layer and its callers.

:class:`FakeSubIssueGraph` holds issues (title, labels, body, state) and their
native parent links, and :meth:`FakeSubIssueGraph.install` patches the
``github`` boundary with it: ``github.graphql`` answers ``epics``' sub-issue,
container, parent, open-issue listing and mutation queries, and passes the trust layer's queries
to a :class:`~tests.vergil_tooling.trust_stubs.FakeItems` that serves each
issue's body (trusted unless added with an untrusted *author*);
``github.read_output`` and ``github.read_json`` answer the REST reads of a node
id, state, title, close time and labels, and the listing of a repo's issues
by label (open, or open and closed). The writes the chain layer makes besides links are faked
too: ``github.create_issue`` adds an issue, and ``github.run`` answers
``issue edit`` (title and body), ``issue comment``, ``issue close`` and
``label create``.
Every write, links included, is recorded in order in
:attr:`FakeSubIssueGraph.events`.

``addSubIssue`` behaves as GitHub's does at its limits (epic
vergil-project/.github#1609, Task 4's probe, vergil-project/.github#1632): a
parent already holding :data:`SUBISSUE_LIMIT` children refuses with
:data:`LIMIT_ERROR`, and a link that would make a tree deeper than
:data:`MAX_DEPTH` levels (the top issue included) refuses with
:data:`DEPTH_ERROR`. Both raise :class:`~vergil_tooling.lib.github.GitHubAPIError`,
as a failed ``gh api graphql`` does.

GitHub's parent field and its sub-issue listings can disagree for a while
after a transfer (vergil-project/vergil-tooling#3839, #4004 and #4291), and
the fake models that too: each issue's ``parent`` is its parent field and each
container's ``children`` its listing, which agree unless a test makes them
disagree with :meth:`FakeSubIssueGraph.unlist` (the field names a container
whose listing lacks the child) or :meth:`FakeSubIssueGraph.list_stale` (a
listing holds a child whose field names another container, or none). Then
``addSubIssue`` into a container that lists the child under another parent
field refuses with :data:`DUPLICATE_ERROR` (#3839), a ``replaceParent`` link
into the container the field already names puts the child back in its
listing (#4291), and ``removeSubIssue`` of the field's link leaves the child
under the container that still lists it, if any (#4004).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import epics, github, trust
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from collections.abc import Iterable

    import pytest

#: GitHub's sub-issue limit per parent.
SUBISSUE_LIMIT = 100

#: GitHub's nesting limit in levels, the top issue included (Task 4's ``Max depth``).
MAX_DEPTH = 8

#: The issues one page of a repo's open-issue listing holds, as GitHub's ``first: 100``.
OPEN_ISSUES_PAGE = 100

#: GitHub's depth error, as Task 4's probe recorded it.
DEPTH_ERROR = (
    "Failed to add sub-issue #{child} to parent #{parent}. You can’t add more than 7 layers"
    " of sub-issues. To add a sub-issue, remove a parent issue at any level."
)

#: GitHub's limit error. The probe never observed it (``Limit error: unobserved``),
#: so this text is the fake's own; the chain layer treats any such text as a hint only.
LIMIT_ERROR = (
    "Failed to add sub-issue #{child} to parent #{parent}."
    " Parent cannot have more than 100 sub-issues."
)

#: GitHub's refusal of a link into a parent that already lists the child (#3839).
DUPLICATE_ERROR = (
    "Failed to add sub-issue #{child} to parent #{parent}."
    " Issue may not contain duplicate sub-issues"
)


@dataclass
class _Issue:
    ref: IssueRef
    title: str
    labels: list[str]
    body: str
    state: str
    parent: IssueRef | None = None
    children: list[IssueRef] = field(default_factory=list)


def _repo_node(ref: IssueRef) -> dict[str, Any]:
    return {"name": ref.repo, "owner": {"login": ref.owner}}


def _graphql_error(text: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ["gh", "api", "graphql"], output="", stderr=f"gh: {text}\n")


class FakeSubIssueGraph:
    """GitHub issues and their native sub-issue links, in memory.

    ``mutations`` records each ``addSubIssue``/``removeSubIssue`` call that
    succeeded, in order, as ``(op, parent, child)`` with *op* ``"add"``,
    ``"reparent"`` or ``"remove"``. ``events`` records every write in order:
    each mutation as in ``mutations``, ``("create", ref)``, ``("edit", ref)``,
    ``("comment", ref, body)``, ``("close", ref, reason)`` and
    ``("label", repo, name)``. ``comments``
    holds each issue's comment bodies.
    """

    def __init__(self) -> None:
        self.issues: dict[IssueRef, _Issue] = {}
        self.layer = trust_stubs.FakeItems()
        self.mutations: list[tuple[str, IssueRef, IssueRef]] = []
        self.events: list[tuple[Any, ...]] = []
        self.comments: dict[IssueRef, list[str]] = {}

    # -- building the graph ----------------------------------------------------

    def add_issue(
        self,
        ref: IssueRef,
        *,
        title: str,
        labels: Iterable[str] = (),
        body: str = "",
        state: str = "OPEN",
        author: Any = trust_stubs.HUMAN,
    ) -> IssueRef:
        """Add *ref*; its *body* is served by *author* (trusted by default)."""
        assert ref not in self.issues, f"{ref.slug} already exists"
        self.issues[ref] = _Issue(
            ref=ref, title=title, labels=list(labels), body=body, state=state.upper()
        )
        self.layer.items[(f"{ref.owner}/{ref.repo}", ref.number)] = trust_stubs.item(
            body=body, author=author
        )
        return ref

    def add_continuation(
        self,
        ref: IssueRef,
        head: IssueRef,
        under: IssueRef | None,
        *,
        k: int = 2,
        author: Any = trust_stubs.HUMAN,
    ) -> IssueRef:
        """Add *ref*, container *k* of *head*'s chain, linked under *under* (None: an orphan).

        Its title, labels and ``Continues:``/``Container:`` lines are as the
        chain layer writes them; its body is served by *author*.
        """
        self.add_issue(
            ref,
            title=f"{self.issues[head].title} (continuation {k})",
            labels=["epic", epics.CONTINUATION_LABEL],
            body=(
                f"Holds the overflow of {head.slug}; managed by the tooling.\n\n"
                f"Continues: {head.slug}\nContainer: {k}\n"
            ),
            author=author,
        )
        if under is not None:
            self.link(under, ref)
        return ref

    def link(self, parent: IssueRef, child: IssueRef) -> None:
        """Link *child* under *parent*, under GitHub's rules (a refusal raises)."""
        self._add(parent, child, replace=False)

    def unlist(self, parent: IssueRef, child: IssueRef) -> None:
        """Drop *child* from *parent*'s listing, its parent field still naming *parent* (#4291)."""
        assert self.issues[child].parent == parent, f"{child.slug} is not under {parent.slug}"
        self.issues[parent].children.remove(child)

    def list_stale(self, parent: IssueRef, child: IssueRef) -> None:
        """List *child* under *parent* too, its parent field unchanged (#3839, #4004)."""
        assert self.issues[child].parent != parent, f"{child.slug} is under {parent.slug}"
        assert child not in self.issues[parent].children, f"{parent.slug} lists {child.slug}"
        self.issues[parent].children.append(child)

    def close(self, ref: IssueRef) -> None:
        self.issues[ref].state = "CLOSED"

    def children_of(self, ref: IssueRef) -> list[IssueRef]:
        return list(self.issues[ref].children)

    def parent_of(self, ref: IssueRef) -> IssueRef | None:
        return self.issues[ref].parent

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
        """Answer the ``github`` boundary and the trust layer's author list from this graph."""
        monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
        monkeypatch.setattr(github, "graphql", self.graphql)
        monkeypatch.setattr(github, "read_output", self.read_output)
        monkeypatch.setattr(github, "read_json", self.read_json)
        monkeypatch.setattr(github, "create_issue", self.create_issue)
        monkeypatch.setattr(github, "run", self.run)
        return self

    def _set_body(self, ref: IssueRef, body: str) -> None:
        """Serve *ref*'s new *body*, keeping its author: an edit by a trusted writer."""
        issue = self.issues[ref]
        issue.body = body
        key = (f"{ref.owner}/{ref.repo}", ref.number)
        node = self.layer.items[key]["repository"]["issueOrPullRequest"]
        node["body"] = body

    # -- GitHub's rules ----------------------------------------------------------

    def _level(self, ref: IssueRef) -> int:
        """*ref*'s level: 1 for a top issue, one more for each ancestor."""
        level = 1
        parent = self.issues[ref].parent
        while parent is not None:
            level += 1
            parent = self.issues[parent].parent
        return level

    def _height(self, ref: IssueRef) -> int:
        """Levels in *ref*'s own subtree, *ref* included."""
        return 1 + max((self._height(c) for c in self.issues[ref].children), default=0)

    def _add(self, parent: IssueRef, child: IssueRef, *, replace: bool) -> None:
        node, sub = self.issues[parent], self.issues[child]
        numbers = {"child": child.number, "parent": parent.number}
        op = "reparent" if replace else "add"
        if child in node.children and sub.parent != parent:
            raise _graphql_error(DUPLICATE_ERROR.format(**numbers))
        if sub.parent is not None and not replace:
            raise _graphql_error("Sub issue may only have one parent")
        if sub.parent == parent and child in node.children:
            self._record((op, parent, child))
            return
        if len(node.children) >= SUBISSUE_LIMIT:
            raise _graphql_error(LIMIT_ERROR.format(**numbers))
        if self._level(parent) + self._height(child) > MAX_DEPTH:
            raise _graphql_error(DEPTH_ERROR.format(**numbers))
        if sub.parent is not None and child in self.issues[sub.parent].children:
            self.issues[sub.parent].children.remove(child)
        node.children.append(child)
        sub.parent = parent
        self._record((op, parent, child))

    def clear_log(self) -> None:
        """Forget the writes so far, so a test sees only those of the call under test."""
        self.mutations.clear()
        self.events.clear()
        self.comments.clear()

    def _record(self, event: tuple[Any, ...]) -> None:
        if event[0] in ("add", "reparent", "remove"):
            self.mutations.append((event[0], event[1], event[2]))
        self.events.append(event)

    def _remove(self, parent: IssueRef, child: IssueRef) -> None:
        node, sub = self.issues[parent], self.issues[child]
        if sub.parent == parent:
            if child in node.children:
                node.children.remove(child)
            # A listing left elsewhere (#4004) takes over the parent field.
            sub.parent = next((r for r, i in self.issues.items() if child in i.children), None)
        elif child in node.children:
            node.children.remove(child)
        else:
            raise _graphql_error(f"#{child.number} is not a sub-issue of #{parent.number}")
        self._record(("remove", parent, child))

    # -- the github boundary ----------------------------------------------------

    def _by_node_id(self, node_id: object) -> IssueRef:
        for ref in self.issues:
            if node_id == f"I_{ref.slug}":
                return ref
        raise AssertionError(f"unknown node id {node_id!r}")

    def _ref_from_endpoint(self, endpoint: str) -> IssueRef:
        parts = endpoint.split("/")
        assert len(parts) == 5 and parts[0] == "repos" and parts[3] == "issues", endpoint
        ref = IssueRef(owner=parts[1], repo=parts[2], number=int(parts[4]))
        assert ref in self.issues, f"unknown issue {ref.slug}"
        return ref

    def _child_node(self, ref: IssueRef, *, labels: bool, blockers: bool) -> dict[str, Any]:
        issue = self.issues[ref]
        node: dict[str, Any] = {
            "number": ref.number,
            "state": issue.state,
            "title": issue.title,
            "closedAt": "2026-11-02T10:00:00Z" if issue.state == "CLOSED" else None,
            "repository": _repo_node(ref),
        }
        if labels:
            node["labels"] = {
                "totalCount": len(issue.labels),
                "nodes": [{"name": name} for name in issue.labels],
            }
        if blockers:
            node["blockedBy"] = {"totalCount": 0, "nodes": []}
        return node

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        """``github.graphql``: the trust layer's queries go to :attr:`layer`."""
        if query in (trust._ITEM_QUERY, trust._EVENTS_QUERY) or trust_stubs.bodies_numbers(
            query, variables
        ):
            return self.layer(query, **variables)
        if query in (epics._ADD_SUBISSUE, epics._REPARENT_SUBISSUE, epics._REMOVE_SUBISSUE):
            parent = self._by_node_id(variables["parent"])
            child = self._by_node_id(variables["child"])
            if query == epics._REMOVE_SUBISSUE:
                self._remove(parent, child)
                return {"removeSubIssue": {"subIssue": {"number": child.number}}}
            self._add(parent, child, replace=query == epics._REPARENT_SUBISSUE)
            return {"addSubIssue": {"subIssue": {"number": child.number}}}
        if query == epics._OPEN_ISSUES_QUERY:
            return self._open_issues_page(variables)
        ref = self._by_node_id(variables["id"])
        issue = self.issues[ref]
        if query in (epics._PARENT_QUERY, epics._PARENT_WITH_LABELS_QUERY):
            parent = issue.parent
            if parent is None:
                return {"node": {"parent": None}}
            node: dict[str, Any] = {"number": parent.number, "repository": _repo_node(parent)}
            if query == epics._PARENT_WITH_LABELS_QUERY:
                names = self.issues[parent].labels
                node["labels"] = {
                    "totalCount": len(names),
                    "nodes": [{"name": name} for name in names],
                }
                grandparent = self.issues[parent].parent
                node["parent"] = None if grandparent is None else {"number": grandparent.number}
            return {"node": {"parent": node}}
        if query == epics._CONTAINER_QUERY:
            nodes = [self._child_node(c, labels=True, blockers=False) for c in issue.children]
            return {
                "node": {
                    "state": issue.state,
                    "subIssues": {"totalCount": len(nodes), "nodes": nodes},
                }
            }
        if query in (epics._SUBISSUES_QUERY, epics._SUBISSUES_WITH_BLOCKERS_QUERY):
            blockers = query == epics._SUBISSUES_WITH_BLOCKERS_QUERY
            nodes = [self._child_node(c, labels=False, blockers=blockers) for c in issue.children]
            return {"node": {"subIssues": {"nodes": nodes}}}
        raise AssertionError(f"unfaked GraphQL query: {query.strip()[:60]!r}")

    def _open_issues_page(self, variables: dict[str, Any]) -> dict[str, Any]:
        """One page of a repo's open issues, each with its parent, as ``epics`` lists them.

        A page holds :data:`OPEN_ISSUES_PAGE` issues; ``after`` is the number
        of issues already listed.
        """
        owner, name = str(variables["owner"]), str(variables["name"])
        listed = sorted(
            (ref for ref, issue in self.issues.items() if issue.state == "OPEN"),
            key=lambda r: r.number,
        )
        listed = [ref for ref in listed if (ref.owner, ref.repo) == (owner, name)]
        start = int(variables.get("after") or 0)
        page = listed[start : start + OPEN_ISSUES_PAGE]
        nodes: list[dict[str, Any]] = []
        for ref in page:
            issue = self.issues[ref]
            node: dict[str, Any] = {
                "number": ref.number,
                "title": issue.title,
                "body": issue.body,
                "labels": {
                    "totalCount": len(issue.labels),
                    "nodes": [{"name": label} for label in issue.labels],
                },
                "parent": None,
            }
            if issue.parent is not None:
                parent = self.issues[issue.parent]
                node["parent"] = {
                    "number": parent.ref.number,
                    "repository": _repo_node(parent.ref),
                    "labels": {
                        "totalCount": len(parent.labels),
                        "nodes": [{"name": label} for label in parent.labels],
                    },
                    "parent": None if parent.parent is None else {"number": parent.parent.number},
                }
            nodes.append(node)
        end = start + len(page)
        return {
            "repository": {
                "issues": {
                    "pageInfo": {"hasNextPage": end < len(listed), "endCursor": str(end)},
                    "nodes": nodes,
                }
            }
        }

    def read_output(self, *args: str) -> str:
        """``github.read_output``: an issue's REST node id, state, title or close time."""
        assert len(args) == 4 and args[0] == "api" and args[2] == "--jq", args
        issue = self.issues[self._ref_from_endpoint(args[1])]
        jq = args[3]
        if jq == ".node_id":
            return f"I_{issue.ref.slug}"
        if jq == ".state":
            return issue.state.lower()
        if jq == ".title":
            return issue.title
        if jq == '.closed_at // ""':
            return "2026-11-02T10:00:00Z" if issue.state == "CLOSED" else ""
        raise AssertionError(f"unfaked jq read: {jq!r}")

    def read_json(self, *args: str) -> dict[str, object] | list[object]:
        """``github.read_json``: one issue's labels, title and state, or a repo's issues by label.

        ``issue view <N> --repo <o/r> --json <fields>`` (of ``labels``,
        ``title`` and ``state``), and ``issue list`` as :meth:`_list` takes it.
        """
        if args[:2] == ("issue", "list"):
            return self._list(args)
        assert args[:2] == ("issue", "view") and args[3] == "--repo" and args[5] == "--json", args
        owner, repo = args[4].split("/", 1)
        issue = self.issues[IssueRef(owner=owner, repo=repo, number=int(args[2]))]
        answer: dict[str, object] = {}
        for name in args[6].split(","):
            if name == "labels":
                answer["labels"] = [{"name": label} for label in issue.labels]
            elif name == "title":
                answer["title"] = issue.title
            elif name == "state":
                answer["state"] = issue.state
            else:
                raise AssertionError(f"unfaked issue view field: {name!r}")
        return answer

    def _list(self, args: tuple[str, ...]) -> list[object]:
        """``issue list --repo <o/r> (--label <name>)… --state <open|all> --limit <n> --json <f>``.

        *f* is a comma-separated list of ``number``, ``labels``, ``title`` and
        ``state``; every ``--label`` must be on a listed issue.
        """
        pairs = list(zip(args[2::2], args[3::2], strict=True))
        wanted = [value for flag, value in pairs if flag == "--label"]
        flags = {flag: value for flag, value in pairs if flag != "--label"}
        assert set(flags) == {"--repo", "--state", "--limit", "--json"} and wanted, args
        assert flags["--state"] in ("open", "all"), args
        fields = flags["--json"].split(",")
        assert set(fields) <= {"number", "labels", "title", "state"}, args
        owner, repo = flags["--repo"].split("/", 1)
        rows: list[object] = []
        for ref, issue in self.issues.items():
            if (ref.owner, ref.repo) != (owner, repo):
                continue
            if flags["--state"] == "open" and issue.state != "OPEN":
                continue
            if not all(label in issue.labels for label in wanted):
                continue
            row: dict[str, object] = {
                "number": ref.number,
                "labels": [{"name": label} for label in issue.labels],
                "title": issue.title,
                "state": issue.state,
            }
            rows.append({name: row[name] for name in fields})
        return rows[: int(flags["--limit"])]

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str = "",
        body_file: str | None = None,
        labels: list[str] | None = None,
        assignees: list[str] | None = None,
    ) -> str:
        """``github.create_issue``: a new open issue, numbered past every issue in *repo*.

        A *body_file* is read for the body, as ``gh`` prefers it over *body*;
        *assignees* are accepted and not modelled.
        """
        del assignees
        if body_file is not None:
            body = Path(body_file).read_text(encoding="utf-8")
        owner, name = repo.split("/", 1)
        number = 1 + max(
            (r.number for r in self.issues if (r.owner, r.repo) == (owner, name)), default=0
        )
        ref = self.add_issue(
            IssueRef(owner=owner, repo=name, number=number),
            title=title,
            labels=labels or (),
            body=body,
            author=trust_stubs.AGENT,
        )
        self._record(("create", ref))
        return f"https://github.com/{repo}/issues/{number}"

    def run(self, *args: str) -> None:
        """``github.run``: ``issue edit``, ``comment`` and ``close``, and ``label create``."""
        if args[:2] == ("label", "create"):
            self._record(("label", args[args.index("--repo") + 1], args[2]))
            return
        assert args[0] == "issue" and args[3] == "--repo", args
        owner, repo = args[4].split("/", 1)
        ref = IssueRef(owner=owner, repo=repo, number=int(args[2]))
        flags = dict(zip(args[5::2], args[6::2], strict=True))
        if args[1] == "comment":
            assert set(flags) == {"--body"}, args
            self.comments.setdefault(ref, []).append(flags["--body"])
            self._record(("comment", ref, flags["--body"]))
            return
        if args[1] == "close":
            assert set(flags) <= {"--reason", "--comment"}, args
            self.issues[ref].state = "CLOSED"
            if "--comment" in flags:
                self.comments.setdefault(ref, []).append(flags["--comment"])
            self._record(("close", ref, flags.get("--reason", "completed")))
            return
        assert args[1] == "edit" and set(flags) <= {"--title", "--body"}, args
        if "--title" in flags:
            self.issues[ref].title = flags["--title"]
        if "--body" in flags:
            self._set_body(ref, flags["--body"])
        self._record(("edit", ref))
