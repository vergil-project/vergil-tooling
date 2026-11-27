"""Tests for vergil_tooling.lib.intake_route (deterministic intake routing)."""

from __future__ import annotations

import contextlib
from datetime import date
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, call, patch

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.lib import epic_chain, epics, github, intake_route, security_records, trust
from vergil_tooling.lib.epic_size import Sizing
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.intake_route import (
    Defer,
    Drop,
    OneOff,
    RouteRefusedError,
    RouteResult,
    SizeItem,
    Split,
    ToIntake,
    ToPlanning,
    route,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vergil_tooling.lib.intake_route import RouteAction

_MOD = "vergil_tooling.lib.intake_route"
# Captured before any patching, to run the real planning ensure under Env.
_REAL_ENSURE_PLANNING_EPIC = epics.ensure_planning_epic
_REAL_PARENT_OF = epics.parent_of

ITEM = IssueRef("org", ".github", 352)
INTAKE = IssueRef("org", ".github", 7)
PLANNING = IssueRef("org", ".github", 8)
ADHOC_DOTGITHUB = IssueRef("org", ".github", 9)
DEFERRED = IssueRef("org", ".github", 10)
TOOL_ADHOC = IssueRef("org", ".github", 11)
OPERATIONS = IssueRef("org", ".github", 12)
DEV_EPIC = IssueRef("org", ".github", 400)
TODAY = date(2026, 10, 23)
ORIG_URL = "https://github.com/org/.github/issues/352"
GHSA = "GHSA-abcd-2345-wxyz"
ADVISORY_LINE = f"Security advisory: org/.github {GHSA}"
# What gh prints when addSubIssue names a child the parent already lists (#3839).
DUPLICATE_REFUSAL = (
    "gh: Failed to add sub-issue #900 to parent org/.github#11. "
    "Issue may not contain duplicate sub-issues"
)


# The queries epics and epic_chain make of the sub-issue graph, reads and writes.
_GRAPH_QUERIES = frozenset(
    {
        epics._ADD_SUBISSUE,
        epics._REPARENT_SUBISSUE,
        epics._REMOVE_SUBISSUE,
        epics._PARENT_QUERY,
        epics._PARENT_WITH_LABELS_QUERY,
        epics._CONTAINER_QUERY,
        epics._SUBISSUES_QUERY,
        epics._SUBISSUES_WITH_BLOCKERS_QUERY,
        epics._OPEN_ISSUES_QUERY,
    }
)


def _applied(_parent: IssueRef, _child: IssueRef, apply: Any) -> None:
    """A write that takes effect under the fake's GitHub rules."""
    apply()


def _no_effect(_parent: IssueRef, _child: IssueRef, _apply: Any) -> None:
    """A write GitHub accepts that never takes effect."""


def _raising(exc: BaseException) -> Any:
    """A write that fails with *exc*, changing nothing."""

    def effect(_parent: IssueRef, _child: IssueRef, _apply: Any) -> None:
        raise exc

    return effect


class _RouteGraph(FakeSubIssueGraph):
    """The sub-issue fake, each write logged on *env* and passed through its effects."""

    def __init__(self, env: Env) -> None:
        super().__init__()
        self.env = env

    def _add(self, parent: IssueRef, child: IssueRef, *, replace: bool) -> None:
        op = "reparent" if replace else "add"
        self.env.writes.append((op, parent, child))
        self.env.effects[op](
            parent, child, lambda: FakeSubIssueGraph._add(self, parent, child, replace=replace)
        )

    def _remove(self, parent: IssueRef, child: IssueRef) -> None:
        self.env.writes.append(("remove", parent, child))
        self.env.effects["remove"](
            parent, child, lambda: FakeSubIssueGraph._remove(self, parent, child)
        )


class Env:
    """The patched GitHub and epics boundary one test drives."""

    def __init__(self) -> None:
        self.parent: IssueRef | None = INTAKE
        # The container_kind map; queue_kind is derived from it.
        self.kinds: dict[IssueRef, str | None] = {
            INTAKE: "intake",
            PLANNING: "planning",
            DEFERRED: "deferred",
            ADHOC_DOTGITHUB: "ad-hoc",
            TOOL_ADHOC: "ad-hoc",
            OPERATIONS: "operations",
        }
        # The issues carrying the 'epic' label (epics.is_epic).
        self.epic_refs: set[IssueRef] = {INTAKE, PLANNING, DEFERRED, ADHOC_DOTGITHUB, DEV_EPIC}
        # The item's own labels, as a 'labels' read of it gives them; None is
        # the labels of *issue_view*, which every other issue's read gives.
        self.labels: list[str] | None = None
        self.comments: list[str] = []
        # The item's comment nodes as the trust layer reads them; by default
        # one per entry of *comments*, by a trusted human.
        self.comment_nodes: list[dict[str, Any]] | None = None
        # Who wrote and last edited the item's body, as the trust layer reads it.
        self.body_author: Any = trust_stubs.HUMAN
        self.body_editor: Any = None
        # Closing-PR URL -> state (OPEN, CLOSED, MERGED); each one's trusted
        # body closes the item, unless *closing_pr_answers* holds its answer.
        self.closing_prs: dict[str, str] = {}
        # Closing-PR URL -> the trust layer's read of it (trust_stubs.closing_pr).
        self.closing_pr_answers: dict[str, dict[str, Any]] = {}
        self.closing_pr_reads: list[str] = []
        # The sub-issue graph the real epics and epic_chain code reads and
        # writes (a FakeSubIssueGraph). The three maps below describe its
        # parent fields and listings: each is loaded into the graph before a
        # sub-issue query and read back from it after one, so a test sets or
        # checks a state through them, even mid-route (a late carry-over).
        self.graph = _RouteGraph(self)
        # Each issue's parent field: task -> the container it names.
        self.native_parent: dict[IssueRef, IssueRef | None] = {}
        # Sub-issue listings GitHub still shows although the child's parent
        # field says otherwise: epic -> stale children (#4004).
        self.stale_listing: dict[IssueRef, set[IssueRef]] = {}
        # Children an epic's sub-issue list lacks although their parent field
        # names it, until a link lands (#4291): epic -> missing children.
        self.unlisted: dict[IssueRef, set[IssueRef]] = {}
        self.epic_state = "open"
        # Bare repo name -> its live ad-hoc epic.
        self.adhoc_epics: dict[str, IssueRef] = {"tool": TOOL_ADHOC, ".github": ADHOC_DOTGITHUB}
        # REST issue endpoint -> state, for the epics an Epic: marker names.
        self.api_states: dict[str, str] = {}
        # The 'gh search issues' rows for a planning item's Planning: line.
        self.search_results: Any = []
        # The search hits whose body an outsider wrote, as the trust layer reads it.
        self.untrusted_hits: set[int] = set()
        self.transfer: Any = "https://github.com/org/tool/issues/900"
        self.issue_view: dict[str, object] = {
            "title": "Make the thing faster",
            "body": "It is slow.",
            "labels": [{"name": "triage"}, {"name": "bug"}, {"name": "off-registry"}],
            "url": ORIG_URL,
        }
        self.created_url = "https://github.com/org/tool/issues/901"
        self.run = MagicMock()
        self.read_output = MagicMock(side_effect=self._read_output)
        self.read_json = MagicMock(side_effect=self._read_json)
        self.create_issue = MagicMock(side_effect=lambda **kw: self.created_url)
        self.ensure_labels = MagicMock()
        # Every sub-issue write the route attempts, in order, as (op, parent,
        # child) with op "add", "reparent" (replaceParent) or "remove".
        self.writes: list[tuple[str, IssueRef, IssueRef]] = []
        # What each kind of write does: called with (parent, child, apply),
        # where apply() makes the write under the fake's GitHub rules. A test
        # swaps one to record an order, fail, or have no effect.
        self.effects: dict[str, Any] = dict.fromkeys(("add", "reparent", "remove"), _applied)
        # Once set, every read of a container's sub-issue listing fails with it,
        # after the first *listing_reads_left* of them.
        self.listing_error: BaseException | None = None
        self.listing_reads_left = 0
        self.sleep = MagicMock()
        self.ensure_planning_epic = MagicMock(return_value=PLANNING)
        self.ensure_intake_epic = MagicMock(return_value=INTAKE)
        self.ensure_deferred_epic = MagicMock(return_value=DEFERRED)
        self.resolve_epic_ref = MagicMock(return_value=TOOL_ADHOC)
        self.advisory_home = MagicMock(side_effect=lambda org: f"{org}/.github")
        self.retarget = MagicMock()
        # Each advisory tracks the routed item alone unless a test says otherwise.
        self.tracked: dict[str, tuple[IssueRef, ...]] = {}
        self.tracked_issues = MagicMock(
            side_effect=lambda _home, ghsa: self.tracked.get(ghsa, (ITEM,))
        )

    def make_security_item(self) -> None:
        """Turn the item into a security item: the label and the advisory line."""
        self.issue_view = {
            **self.issue_view,
            "body": f"It is slow.\n\n{ADVISORY_LINE}",
            "labels": [{"name": "bug"}, {"name": "security"}],
        }

    def calls(self, op: str) -> list[tuple[IssueRef, IssueRef]]:
        """The (parent, child) of every *op* write attempted, in order."""
        return [(parent, child) for kind, parent, child in self.writes if kind == op]

    def listing(self, epic: IssueRef) -> list[IssueRef]:
        """*epic*'s sub-issue list: its children by parent field, less unlisted, plus stale."""
        listed = {task for task, parent in self.native_parent.items() if parent == epic}
        listed -= self.unlisted.get(epic, set())
        return sorted(listed | self.stale_listing.get(epic, set()), key=lambda ref: ref.slug)

    def add_continuation(self, ref: IssueRef, head: IssueRef, under: IssueRef, k: int = 2) -> None:
        """Add continuation *k* of *head*'s chain, linked under *under*."""
        self._ensure(head)
        self.graph.add_continuation(ref, head, None, k=k)
        self.native_parent[ref] = under

    def fill(self, container: IssueRef, count: int, *, start: int) -> list[IssueRef]:
        """Link *count* items numbered from *start* under *container*."""
        items = [IssueRef("org", "tool", number) for number in range(start, start + count)]
        for item in items:
            self.native_parent[item] = container
        return items

    def _ensure(self, ref: IssueRef) -> None:
        if ref not in self.graph.issues:
            kind = self.kinds.get(ref)
            if kind is not None:
                labels = ["epic", kind]
            else:
                labels = ["epic"] if ref in self.epic_refs else []
            self.graph.add_issue(ref, title=f"issue {ref.slug}", labels=labels)

    def _state(self, ref: IssueRef) -> str:
        endpoint = f"repos/{ref.owner}/{ref.repo}/issues/{ref.number}"
        return self.api_states.get(endpoint, self.epic_state).upper()

    def _load(self) -> None:
        """Put the parent fields and listings the maps describe into the graph."""
        refs = {*self.kinds, *self.epic_refs, *self.native_parent}
        refs |= {parent for parent in self.native_parent.values() if parent is not None}
        for epic, tasks in (*self.stale_listing.items(), *self.unlisted.items()):
            refs |= {epic, *tasks}
        for ref in refs:
            self._ensure(ref)
        for ref, issue in self.graph.issues.items():
            issue.parent = self.native_parent.get(ref)
            issue.children = self.listing(ref)
            issue.state = self._state(ref)

    def _store(self) -> None:
        """Read the graph's parent fields and listings back into the maps."""
        issues = self.graph.issues
        fields = {ref: issue.parent for ref, issue in issues.items()}
        stale = {
            ref: {c for c in issue.children if issues[c].parent != ref}
            for ref, issue in issues.items()
        }
        unlisted = {
            ref: {c for c, parent in fields.items() if parent == ref} - set(issue.children)
            for ref, issue in issues.items()
        }
        known = set(self.native_parent)
        self.native_parent.clear()
        self.native_parent.update(
            {ref: parent for ref, parent in fields.items() if parent is not None or ref in known}
        )
        self.stale_listing.clear()
        self.stale_listing.update({ref: kids for ref, kids in stale.items() if kids})
        self.unlisted.clear()
        self.unlisted.update({ref: kids for ref, kids in unlisted.items() if kids})

    def find_standing_epic(self, kind: epics.StandingKind, home: str, name: str) -> IssueRef | None:
        """The live standing epics: one queue of each kind, an ad-hoc epic per repo."""
        assert home == "org/.github"
        if kind is epics.ADHOC:
            return self.adhoc_epics.get(name)
        assert name == "org"
        return {
            "intake": INTAKE,
            "planning": PLANNING,
            "deferred": DEFERRED,
            "operations": OPERATIONS,
        }[kind.key]

    def queue_kind(self, ref: IssueRef) -> str | None:
        kind = self.kinds.get(ref)
        return kind if kind in ("intake", "planning", "deferred") else None

    def _read_json(self, *args: str) -> object:
        fields = args[-1]
        if args[:2] == ("issue", "list"):
            # The chain layer's listing of a repo's continuations.
            self._load()
            return self.graph.read_json(*args)
        if args[:2] == ("search", "issues"):
            if isinstance(self.search_results, BaseException):
                raise self.search_results
            return self.search_results
        if fields == "labels" and int(args[2]) == ITEM.number and self.labels is not None:
            return {"labels": [{"name": name} for name in self.labels]}
        if fields == "labels" and args[:2] == ("issue", "view"):
            owner, repo = args[4].split("/", 1)
            issue = self.graph.issues.get(IssueRef(owner, repo, int(args[2])))
            if issue is not None and "epic" in issue.labels:
                return self.graph.read_json(*args)
        if fields == "closedByPullRequestsReferences":
            return {
                "closedByPullRequestsReferences": [
                    {"number": int(url.rsplit("/", 1)[1]), "url": url}
                    for url in {**self.closing_prs, **self.closing_pr_answers}
                ]
            }
        return self.issue_view

    def graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        """The trust layer's item read: the item's body and comments, any other issue's none.

        A closing PR's read answers from *closing_pr_answers*, or as a PR in
        *closing_prs* whose trusted body closes the item. A sub-issue query
        or write goes to :attr:`graph`, loaded from the maps first and read
        back into them after.
        """
        if query in _GRAPH_QUERIES:
            if self.listing_error is not None and query == epics._CONTAINER_QUERY:
                if self.listing_reads_left == 0:
                    raise self.listing_error
                self.listing_reads_left -= 1
            self._load()
            try:
                return self.graph.graphql(query, **variables)
            finally:
                self._store()
        if query == trust._PR_CLOSING_QUERY:
            repo = f"{variables['owner']}/{variables['name']}"
            url = f"https://github.com/{repo}/pull/{variables['number']}"
            self.closing_pr_reads.append(url)
            if url in self.closing_pr_answers:
                return self.closing_pr_answers[url]
            item = (f"{ITEM.owner}/{ITEM.repo}", ITEM.number, "OPEN")
            return trust_stubs.closing_pr(
                repo, int(variables["number"]), item, state=self.closing_prs[url]
            )
        nodes = self.comment_nodes
        if nodes is None:
            nodes = trust_stubs.comments(*self.comments)
        item = trust_stubs.item(
            body=str(self.issue_view.get("body") or ""),
            author=self.body_author,
            editor=self.body_editor,
            nodes=nodes,
        )
        items = {(f"{ITEM.owner}/{ITEM.repo}", ITEM.number): item}
        # Each search hit's body, as the trust layer reads it again: by the
        # trusted human, or by an outsider for the numbers in *untrusted_hits*.
        for hit in self.search_results if isinstance(self.search_results, list) else []:
            repo = str((hit.get("repository") or {}).get("nameWithOwner") or "")
            outsider = hit["number"] in self.untrusted_hits
            author = trust_stubs.OUTSIDER if outsider else trust_stubs.HUMAN
            items.setdefault(
                (repo, int(hit["number"])),
                trust_stubs.item(body=str(hit.get("body") or ""), author=author),
            )
        # Every other issue's body (a continuation's, say) as the graph serves it.
        for key, answer in self.graph.layer.items.items():
            items.setdefault(key, answer)
        fake = trust_stubs.FakeItems(items)
        return fake(query, **variables)

    def _read_output(self, *args: str) -> str:
        if args[:2] == ("issue", "transfer"):
            if isinstance(self.transfer, BaseException):
                raise self.transfer
            return str(self.transfer)
        assert args[0] == "api"
        if args[-1] == ".node_id":
            _, owner, repo, _, number = args[1].split("/")
            ref = IssueRef(owner, repo, int(number))
            self._ensure(ref)
            return f"I_{ref.slug}"
        if args[-1] == ".title":
            # The chain layer's read of a container's title.
            self._load()
            return self.graph.read_output(*args)
        assert args[-1] == ".state"
        if args[1] in self.api_states:
            return self.api_states[args[1]]
        return self.epic_state


@pytest.fixture
def env() -> Iterator[Env]:
    state = Env()
    # Python allows only 20 statically nested blocks, so one ExitStack holds them.
    patches = (
        patch(f"{_MOD}.epics.parent_of", side_effect=lambda ref: state.parent),
        patch(f"{_MOD}.epics.queue_kind", side_effect=state.queue_kind),
        patch(f"{_MOD}.epics.container_kind", side_effect=lambda ref: state.kinds.get(ref)),
        patch(f"{_MOD}.epics.is_epic", side_effect=lambda ref: ref in state.epic_refs),
        patch(f"{_MOD}.epics.ensure_planning_epic", state.ensure_planning_epic),
        patch(f"{_MOD}.epics.ensure_intake_epic", state.ensure_intake_epic),
        patch(f"{_MOD}.epics.ensure_deferred_epic", state.ensure_deferred_epic),
        patch(f"{_MOD}.epics.resolve_epic_ref", state.resolve_epic_ref),
        # The never-creating lookups a one-off re-run uses (#4291): all of them
        # home in org/.github.
        patch(f"{_MOD}.epics.resolve_epic_home", side_effect=lambda owner, bare: "org/.github"),
        patch(f"{_MOD}.epics.queue_home", side_effect=lambda repo: ("org/.github", "org")),
        patch(f"{_MOD}.epics.operations_home", side_effect=lambda repo: ("org/.github", "org")),
        patch(f"{_MOD}.epics.find_standing_epic", side_effect=state.find_standing_epic),
        patch("vergil_tooling.lib.github.graphql", state.graphql),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        patch(f"{_MOD}.load_trusted_authors", return_value=trust_stubs.AUTHORS),
        patch(f"{_MOD}.github.run", state.run),
        patch(f"{_MOD}.github.read_output", state.read_output),
        patch(f"{_MOD}.github.read_json", state.read_json),
        patch(f"{_MOD}.github.create_issue", state.create_issue),
        patch(f"{_MOD}.ensure_labels", state.ensure_labels),
        patch(f"{_MOD}.time.sleep", state.sleep),
        patch(f"{_MOD}.security_records.advisory_home", state.advisory_home),
        patch(f"{_MOD}.security_records.retarget", state.retarget),
        patch(f"{_MOD}.security_records.tracked_issues", state.tracked_issues),
    )
    with contextlib.ExitStack() as stack:
        for patcher in patches:
            stack.enter_context(patcher)
        yield state


def _transfer_calls(env: Env) -> list[Any]:
    return [c for c in env.read_output.call_args_list if c.args[:2] == ("issue", "transfer")]


# -- drop / defer / split ----------------------------------------------------


def test_route_drop_comments_and_closes(env: Env) -> None:
    result = route(ITEM, Drop(reason="not feasible for years"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "not planned",
        "--comment",
        "Dropped in intake review: not feasible for years",
    )


def _comment_calls(env: Env) -> list[Any]:
    return [c for c in env.run.call_args_list if c.args[:2] == ("issue", "comment")]


def test_route_defer_moves_intake_item_and_records(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    result = route(ITEM, Defer(reason="waits on X", revisit_when="X ships"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.ensure_deferred_epic.assert_called_once_with("org/.github")
    # The intake parent is replaced atomically, never unlinked first.
    assert env.calls("reparent") == [(DEFERRED, ITEM)]
    assert env.native_parent[ITEM] == DEFERRED
    env.run.assert_called_once_with(
        "issue",
        "comment",
        "352",
        "--repo",
        "org/.github",
        "--body",
        "Deferred 2026-10-23: waits on X\nRevisit when: X ships\nDeferred from: org/.github#7",
    )


def test_route_defer_links_before_commenting(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    order: list[str] = []
    env.effects["reparent"] = lambda *a: order.append("link") or a[2]()
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert order == ["link", "issue:comment"]


def test_route_defer_date_override(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    route(ITEM, Defer(reason="r", revisit_when="w", day=date(2026, 10, 24)), today=TODAY)
    assert _comment_calls(env)[0].args[-1].startswith("Deferred 2026-10-24: r\n")


def test_route_defer_defaults_to_today(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    with patch(f"{_MOD}.date") as mock_date:
        mock_date.today.return_value = date(2027, 1, 2)
        route(ITEM, Defer(reason="later", revisit_when="soon"))
    assert _comment_calls(env)[0].args[-1].startswith("Deferred 2027-01-02: later\n")


def test_route_defer_refresh_without_an_earlier_origin_says_so(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refresh with no earlier record to carry its origin from says so (#5224)."""
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    env.comments = [
        # Not a deferral record, so its line is not taken.
        "Deferred from: org/.github#99 is what the old record said",
        "Deferred 2026-01-02: older\nRevisit when: later",
    ]
    result = route(ITEM, Defer(reason="still waiting", revisit_when="Y ships"), today=TODAY)
    assert capsys.readouterr().err.endswith(
        "org/.github#352: no earlier deferral record of it carries a 'Deferred from:' "
        "line, so the refreshed record has none\n"
    )
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.run.assert_called_once_with(
        "issue",
        "comment",
        "352",
        "--repo",
        "org/.github",
        "--body",
        "Deferred 2026-10-23: still waiting\nRevisit when: Y ships",
    )
    env.ensure_deferred_epic.assert_not_called()
    assert env.calls("add") == []
    assert env.calls("reparent") == []


def test_route_defer_refresh_carries_the_origin_forward(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refresh keeps the latest record's ``Deferred from:`` line, past one without (#5224)."""
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    env.comments = [
        "Deferred 2026-01-02: first\nRevisit when: later\nDeferred from: org/.github#5",
        "Deferred 2026-03-04: moved\nRevisit when: later\nDeferred from: org/.github#7",
        # A refresh written before #5224, with no origin of its own.
        "Deferred 2026-05-06: refreshed\nRevisit when: later",
        "A note that is not a record.",
    ]
    route(ITEM, Defer(reason="still waiting", revisit_when="Y ships"), today=TODAY)
    assert _comment_calls(env)[0].args[-1] == (
        "Deferred 2026-10-23: still waiting\nRevisit when: Y ships\nDeferred from: org/.github#7"
    )
    assert "Deferred from:" not in capsys.readouterr().err


def test_route_defer_from_planning_removes_in_progress(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.labels = ["in-progress", "idea"]
    env.comments = ["Some progress note."]
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]
    env.run.assert_any_call(
        "issue", "edit", "352", "--repo", "org/.github", "--remove-label", "in-progress"
    )
    body = _comment_calls(env)[0].args[-1]
    assert body.endswith("\nDeferred from: org/.github#8")


def test_route_defer_from_planning_without_in_progress_leaves_labels(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert [c.args[1] for c in env.run.call_args_list] == ["comment"]


def test_route_defer_from_adhoc_moves(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]
    assert _comment_calls(env)[0].args[-1].endswith("\nDeferred from: org/.github#9")


def test_route_defer_refuses_development_epic_task(env: Env) -> None:
    env.parent = DEV_EPIC
    with pytest.raises(RouteRefusedError, match=r"org/\.github#400"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.run.assert_not_called()
    assert env.calls("add") == []
    assert env.calls("reparent") == []
    env.ensure_deferred_epic.assert_not_called()


def test_route_defer_refuses_unparented(env: Env) -> None:
    env.parent = None
    with pytest.raises(RouteRefusedError, match="has no parent"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.run.assert_not_called()
    env.ensure_deferred_epic.assert_not_called()


def _assert_untouched(env: Env) -> None:
    env.run.assert_not_called()
    assert env.calls("reparent") == []
    env.ensure_deferred_epic.assert_not_called()


def test_route_parent_fallback_trusted_only(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    # With no native parent, the item's container comes from its body's
    # Parent: line, which counts only from a trusted body. Outside Actions,
    # so the skip is reported per item, not counted by cause.
    env.issue_view = {**env.issue_view, "body": f"It is slow.\n\nParent: {INTAKE.slug}\n"}
    with patch(f"{_MOD}.epics.parent_of", _REAL_PARENT_OF):
        route(ITEM, Drop(reason="not feasible"), today=TODAY)
    assert [c.args[:2] for c in env.run.call_args_list] == [("issue", "close")]
    assert capsys.readouterr().err == ""

    # The same line in a body an outsider edited gives no parent: a refusal.
    env.run.reset_mock()
    env.body_editor = trust_stubs.OUTSIDER
    with (
        patch(f"{_MOD}.epics.parent_of", _REAL_PARENT_OF),
        pytest.raises(RouteRefusedError, match="has no parent"),
    ):
        route(ITEM, Drop(reason="not feasible"), today=TODAY)
    _assert_untouched(env)
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: {ITEM.slug} edit drive-by at {trust_stubs.EDITED}"
        " on body (untrusted)\n"
    )


_EVERY_ROUTE: list[RouteAction] = [
    Drop(reason="not feasible"),
    Defer(reason="r", revisit_when="w"),
    OneOff(repo="org/tool"),
    ToPlanning(),
    ToPlanning(sizing=Sizing(14, "med", "One sentence.")),
    ToIntake(),
    Split(into=(IssueRef("org", "tool", 10), IssueRef("org", "lib", 11)), reason="two fixes"),
    SizeItem(Sizing(14, "med", "One sentence.")),
]


def _assert_nothing_resolved_or_written(env: Env) -> None:
    for mock in (
        env.run,
        env.read_output,
        env.read_json,
        env.create_issue,
        env.ensure_labels,
        env.ensure_planning_epic,
        env.ensure_intake_epic,
        env.ensure_deferred_epic,
        env.resolve_epic_ref,
        env.retarget,
    ):
        mock.assert_not_called()
    assert env.writes == []


@pytest.mark.parametrize(
    ("author", "login", "cause"),
    [
        (trust_stubs.OUTSIDER, "drive-by", "untrusted"),
        (trust_stubs.IMPOSTOR, "vergil-user", "wrong-type"),
    ],
    ids=["outsider", "impostor"],
)
@pytest.mark.parametrize("action", _EVERY_ROUTE, ids=lambda a: type(a).__name__)
def test_route_outsider_issue_stops(
    env: Env, action: RouteAction, author: Any, login: str, cause: str
) -> None:
    # A tool pointed at an outsider-opened item acts on nothing: every route
    # stops before anything resolves or is written, naming only metadata.
    env.issue_view = {**env.issue_view, "body": f"Parent: {INTAKE.slug}\n{trust_stubs.SPAM_TEXT}"}
    env.body_author = author
    with (
        patch(f"{_MOD}.epics.parent_of") as parent_of,
        pytest.raises(trust.UntrustedContentError) as excinfo,
    ):
        route(ITEM, action, today=TODAY)
    parent_of.assert_not_called()
    _assert_nothing_resolved_or_written(env)
    item = excinfo.value.item
    assert (item.repo, item.number, item.kind, item.login, item.field, item.cause) == (
        f"{ITEM.owner}/{ITEM.repo}",
        ITEM.number,
        "issue",
        login,
        "body",
        cause,
    )
    assert excinfo.value.reason == "the item was opened by an untrusted author"
    assert "SPAM-MARKER-TEXT" not in str(excinfo.value)
    assert str(excinfo.value).startswith("stopped before any write: ")


@pytest.mark.parametrize(
    "action",
    [SizeItem(Sizing(14, "med", "One sentence.")), ToPlanning(sizing=Sizing(3, "high", "Small."))],
    ids=["size", "to-planning-sized"],
)
def test_size_outsider_issue_stops(env: Env, action: RouteAction) -> None:
    # Sizing runs only through routing (epic_size reads nothing from GitHub),
    # so sizing an outsider-opened item is the routing stop: no Sized comment
    # and no epic-size label.
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.body_author = trust_stubs.OUTSIDER
    with pytest.raises(trust.UntrustedContentError):
        route(ITEM, action, today=TODAY)
    _assert_nothing_resolved_or_written(env)


def test_route_trusted_item_with_outsider_comment_is_routed(env: Env) -> None:
    # Only the author decides the stop: an outsider's comment on a trusted
    # item does not stop the route.
    env.comment_nodes = [trust_stubs.comment(11, trust_stubs.SPAM_TEXT, trust_stubs.OUTSIDER)]
    result = route(ITEM, Drop(reason="not feasible"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert [c.args[:2] for c in env.run.call_args_list] == [("issue", "close")]


def _planning_item(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.labels = ["in-progress"]


def _epic_hit(number: int, state: str, body: str = "Planning: org/.github#352") -> dict[str, Any]:
    return {
        "number": number,
        "state": state,
        "repository": {"nameWithOwner": "org/.github"},
        "body": f"An epic.\n\n{body}\n",
    }


def test_route_drop_planning_refuses_open_marker_epic(env: Env) -> None:
    _planning_item(env)
    env.comments = ["Epic: org/.github#600\nDocs task: org/.github#601"]
    with pytest.raises(
        RouteRefusedError,
        match=r"has an open development epic \(org/\.github#600\); dropping it would orphan",
    ):
        route(ITEM, Drop(reason="no longer needed"), today=TODAY)
    _assert_untouched(env)


def test_forged_epic_marker_does_not_block_drop(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    # An outsider's comment, a trusted one an outsider edited and a User named
    # like the agent App each name an open epic: none is a marker, so the drop
    # goes ahead and each is reported, metadata only.
    _planning_item(env)
    forged = f"Epic: org/.github#600\n{trust_stubs.SPAM_TEXT}"
    env.comment_nodes = [
        trust_stubs.comment(11, forged, trust_stubs.OUTSIDER),
        trust_stubs.comment(12, forged, trust_stubs.HUMAN, editor=trust_stubs.OUTSIDER),
        trust_stubs.comment(13, forged, trust_stubs.IMPOSTOR),
    ]
    env.api_states = {"repos/org/.github/issues/600": "open"}
    result = route(ITEM, Drop(reason="no longer needed"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert env.run.call_args.args[:3] == ("issue", "close", "352")
    # The named epic's state is never read: no trusted marker names it.
    assert not [c for c in env.read_output.call_args_list if c.args[0] == "api"]
    err = capsys.readouterr().err
    assert err.splitlines() == [
        f"skipped untrusted content: org/.github#352 comment drive-by at "
        f"{trust_stubs.CREATED} on comment 11 (untrusted)",
        f"skipped untrusted content: org/.github#352 edit drive-by at "
        f"{trust_stubs.EDITED} on comment 12 (untrusted)",
        f"skipped untrusted content: org/.github#352 comment vergil-user at "
        f"{trust_stubs.CREATED} on comment 13 (wrong-type)",
    ]
    assert "SPAM-MARKER-TEXT" not in err
    assert "example.invalid" not in err


def test_route_drop_planning_refuses_open_epic_found_by_planning_line(env: Env) -> None:
    _planning_item(env)
    env.comments = ["Epic: org/.github#600"]
    env.api_states = {"repos/org/.github/issues/600": "closed"}
    env.search_results = [_epic_hit(600, "CLOSED"), _epic_hit(648, "OPEN")]
    with pytest.raises(
        RouteRefusedError, match=r"has an open development epic \(org/\.github#648\); dropping"
    ):
        route(ITEM, Drop(reason="no longer needed"), today=TODAY)
    _assert_untouched(env)


def test_route_drop_planning_with_all_epics_closed_closes(env: Env) -> None:
    # #4170: a planning item whose development epic is closed can be dropped.
    _planning_item(env)
    env.comments = ["Epic: org/.github#600\nDocs task: org/.github#601"]
    env.api_states = {"repos/org/.github/issues/600": "closed"}
    env.search_results = [_epic_hit(600, "CLOSED")]
    result = route(ITEM, Drop(reason="no longer needed"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert env.calls("reparent") == []
    env.ensure_deferred_epic.assert_not_called()
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "not planned",
        "--comment",
        "Dropped in planning review: no longer needed",
    )


def test_route_defer_planning_refuses_open_marker_epic(env: Env) -> None:
    _planning_item(env)
    env.comments = ["Epic: org/.github#600\nDocs task: org/.github#601"]
    with pytest.raises(
        RouteRefusedError, match=r"has an open development epic \(org/\.github#600\)"
    ):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    _assert_untouched(env)


def test_route_defer_planning_refuses_an_earlier_open_marker_epic(env: Env) -> None:
    # Only the latest marker counted before #4001; every epic named must be closed.
    _planning_item(env)
    env.comments = ["Epic: org/.github#600", "Epic: org/.github#610"]
    env.api_states = {
        "repos/org/.github/issues/600": "open",
        "repos/org/.github/issues/610": "closed",
    }
    with pytest.raises(RouteRefusedError, match=r"\(org/\.github#600\)"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    _assert_untouched(env)


def test_route_defer_planning_with_all_epics_closed_moves(env: Env) -> None:
    _planning_item(env)
    env.comments = ["Epic: org/.github#600\nDocs task: org/.github#601"]
    env.api_states = {"repos/org/.github/issues/600": "closed"}
    env.search_results = [_epic_hit(600, "CLOSED"), _epic_hit(648, "CLOSED")]
    route(ITEM, Defer(reason="on hold", revisit_when="the design settles"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]
    assert _comment_calls(env)[0].args[-1] == (
        "Deferred 2026-10-23: on hold\nRevisit when: the design settles\n"
        "Deferred from: org/.github#8"
    )
    [search] = [c for c in env.read_json.call_args_list if c.args[:2] == ("search", "issues")]
    assert search.args == (
        "search",
        "issues",
        "Planning: org/.github#352",
        "--owner",
        "org",
        "--label",
        "epic",
        "--limit",
        str(epics.LISTING_LIMIT),
        "--json",
        "number,state,repository,body",
    )


def test_route_defer_planning_refuses_open_epic_found_by_planning_line(env: Env) -> None:
    # #848 in the #4001 report: an epic with no Epic: marker, only its Planning: line.
    _planning_item(env)
    env.comments = ["Epic: org/.github#600"]
    env.api_states = {"repos/org/.github/issues/600": "closed"}
    env.search_results = [_epic_hit(600, "CLOSED"), _epic_hit(648, "OPEN")]
    with pytest.raises(
        RouteRefusedError, match=r"has an open development epic \(org/\.github#648\)"
    ):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    _assert_untouched(env)


def test_route_defer_planning_ignores_an_untrusted_planning_line(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """A Planning: line counts only in a body the trust layer trusts, read again per hit."""
    _planning_item(env)
    env.search_results = [_epic_hit(648, "OPEN")]
    env.untrusted_hits = {648}
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]
    err = capsys.readouterr().err
    assert "skipped untrusted content:" in err
    assert "648" in err
    assert "Planning: org/.github#352" not in err


def test_route_defer_planning_ignores_hit_without_the_exact_planning_line(env: Env) -> None:
    _planning_item(env)
    env.search_results = [
        _epic_hit(700, "OPEN", body="Planning: org/.github#3520"),
        _epic_hit(701, "OPEN", body="See Planning: org/.github#352 for context"),
    ]
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]


def test_route_defer_planning_refuses_a_truncated_search(env: Env) -> None:
    _planning_item(env)
    env.search_results = [_epic_hit(600, "CLOSED")] * epics.LISTING_LIMIT
    with pytest.raises(epics.EpicListTruncatedError, match="possibly truncated"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    _assert_untouched(env)


def test_route_defer_planning_search_failure_raises(env: Env) -> None:
    _planning_item(env)
    env.search_results = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(github.GitHubAPIError):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    _assert_untouched(env)


def test_route_defer_planning_state_read_failure_raises(env: Env) -> None:
    _planning_item(env)
    env.comments = ["Epic: org/.github#600"]
    env.read_output.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 404")
    with pytest.raises(github.GitHubAPIError):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    _assert_untouched(env)


@pytest.mark.parametrize(
    ("results", "match"),
    [
        ({"items": []}, "unexpected 'gh search issues' response"),
        ([_epic_hit(600, "MERGED")], r"unexpected state 'MERGED' for org/\.github#600"),
        ([{**_epic_hit(600, "OPEN"), "repository": {}}], "has no repository"),
    ],
)
def test_route_defer_planning_refuses_malformed_search(env: Env, results: Any, match: str) -> None:
    _planning_item(env)
    env.search_results = results
    with pytest.raises(RuntimeError, match=match):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    _assert_untouched(env)


def _assert_no_one_off(env: Env) -> None:
    """A refused one-off: nothing moved, and no ad-hoc epic was resolved (or created)."""
    _assert_untouched(env)
    env.resolve_epic_ref.assert_not_called()
    assert _transfer_calls(env) == []
    env.create_issue.assert_not_called()


def _label_removals(env: Env) -> list[Any]:
    return [c for c in env.run.call_args_list if c.args[:2] == ("issue", "edit")]


def test_route_one_off_from_planning_moves_under_adhoc(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    new = IssueRef("org", "tool", 900)
    assert result == RouteResult(final=new, moved_by="transfer")
    env.resolve_epic_ref.assert_called_once_with("adhoc", repo="org/tool")
    assert env.native_parent[new] == TOOL_ADHOC
    assert new not in env.listing(PLANNING)
    assert _label_removals(env) == []


def test_route_one_off_planning_refuses_open_marker_epic(env: Env) -> None:
    _planning_item(env)
    env.comments = ["Epic: org/.github#600\nDocs task: org/.github#601"]
    with pytest.raises(
        RouteRefusedError,
        match=r"has an open development epic \(org/\.github#600\); routing one-off it",
    ):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    _assert_no_one_off(env)


def test_route_one_off_planning_refuses_open_epic_found_by_planning_line(env: Env) -> None:
    _planning_item(env)
    env.comments = ["Epic: org/.github#600"]
    env.api_states = {"repos/org/.github/issues/600": "closed"}
    env.search_results = [_epic_hit(600, "CLOSED"), _epic_hit(648, "OPEN")]
    with pytest.raises(
        RouteRefusedError,
        match=r"has an open development epic \(org/\.github#648\); routing one-off",
    ):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    _assert_no_one_off(env)


def test_route_one_off_planning_refuses_a_truncated_search(env: Env) -> None:
    _planning_item(env)
    env.search_results = [_epic_hit(600, "CLOSED")] * epics.LISTING_LIMIT
    with pytest.raises(epics.EpicListTruncatedError, match="possibly truncated"):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    _assert_no_one_off(env)


def _closed_planning_item(env: Env) -> None:
    """A planning item labelled in-progress whose one development epic is closed."""
    _planning_item(env)
    env.comments = ["Epic: org/.github#600"]
    env.api_states = {"repos/org/.github/issues/600": "closed"}
    env.search_results = [_epic_hit(600, "CLOSED")]
    env.issue_view = {
        **env.issue_view,
        "labels": [{"name": "in-progress"}, {"name": "bug"}],
    }


def test_route_one_off_from_planning_removes_in_progress_after_transfer(env: Env) -> None:
    _closed_planning_item(env)
    order: list[str] = []
    env.effects["reparent"] = lambda *a: order.append("link") or a[2]()
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    new = IssueRef("org", "tool", 900)
    assert result == RouteResult(final=new, moved_by="transfer")
    # Removed from the transferred issue, never the old ref, once the move settled.
    env.run.assert_called_once_with(
        "issue", "edit", "900", "--repo", "org/tool", "--remove-label", "in-progress"
    )
    assert order == ["link", "issue:edit"]


def test_route_one_off_from_planning_transfer_that_dropped_the_label_removes_nothing(
    env: Env,
) -> None:
    # A transfer drops a label the target repo lacks: nothing is left to remove.
    _closed_planning_item(env)
    env.issue_view = {**env.issue_view, "labels": [{"name": "bug"}]}
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result.moved_by == "transfer"
    assert _label_removals(env) == []


def test_route_one_off_from_planning_removes_in_progress_after_recreate(env: Env) -> None:
    _closed_planning_item(env)
    _transfer_fails(env)
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=IssueRef("org", "tool", 901), moved_by="recreate")
    # The recreated issue never gets the label, so there is nothing to remove.
    assert env.create_issue.call_args.kwargs["labels"] == ["bug"]
    env.ensure_labels.assert_called_once_with("org/tool", ["bug"])
    assert _label_removals(env) == []


def test_route_one_off_from_planning_removes_in_progress_same_repo(env: Env) -> None:
    _closed_planning_item(env)
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    result = route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert env.calls("reparent") == [(ADHOC_DOTGITHUB, ITEM)]
    env.run.assert_called_once_with(
        "issue", "edit", "352", "--repo", "org/.github", "--remove-label", "in-progress"
    )


def test_route_one_off_from_planning_keeps_in_progress_when_move_fails(env: Env) -> None:
    _closed_planning_item(env)
    _transfer_fails(env)
    env.create_issue.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(github.GitHubAPIError):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    # The original is neither closed nor stripped of its label.
    env.run.assert_not_called()


def test_route_one_off_from_planning_label_failure_fails_loudly(env: Env) -> None:
    _closed_planning_item(env)
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("moved org/.github#352 to org/tool#900, but ")
    assert "routing one-off left the 'in-progress' label on org/tool#900" in message
    assert "HTTP 502" in message
    assert "gh issue edit 900 --repo org/tool --remove-label in-progress" in message
    assert env.native_parent[IssueRef("org", "tool", 900)] == TOOL_ADHOC


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (github.GitHubAPIError(1, ["gh"], "", "HTTP 502"), "HTTP 502"),
        (["not", "a", "dict"], "unexpected 'gh issue view' response"),
    ],
)
def test_route_one_off_from_planning_label_read_failure_after_transfer_is_half_applied(
    env: Env, failure: Any, detail: str
) -> None:
    _closed_planning_item(env)
    plain_read = env.read_json.side_effect

    def read_json(*args: str) -> object:
        if args[-1] == "labels" and args[2] == "900":
            if isinstance(failure, BaseException):
                raise failure
            return failure
        return plain_read(*args)

    env.read_json.side_effect = read_json
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("moved org/.github#352 to org/tool#900, but ")
    assert "could not read the labels of org/tool#900" in message
    assert detail in message
    assert "gh issue edit 900 --repo org/tool --remove-label in-progress" in message
    assert env.native_parent[IssueRef("org", "tool", 900)] == TOOL_ADHOC
    assert _label_removals(env) == []


def test_route_one_off_from_planning_label_read_missing_token_is_left_as_it_came(
    env: Env,
) -> None:
    _closed_planning_item(env)
    plain_read = env.read_json.side_effect

    def read_json(*args: str) -> object:
        if args[-1] == "labels" and args[2] == "900":
            raise github.MissingGitHubTokenError(1, ["gh"], "", "GH_TOKEN environment variable")
        return plain_read(*args)

    env.read_json.side_effect = read_json
    with pytest.raises(github.MissingGitHubTokenError):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)


def test_route_recreate_never_carries_in_progress(env: Env) -> None:
    _transfer_fails(env)
    env.issue_view = {**env.issue_view, "labels": [{"name": "in-progress"}, {"name": "bug"}]}
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert env.create_issue.call_args.kwargs["labels"] == ["bug"]


def test_route_drop_planning_without_marker_closes(env: Env) -> None:
    env.parent = PLANNING
    env.comments = ["Just a note."]
    route(ITEM, Drop(reason="superseded"), today=TODAY)
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "not planned",
        "--comment",
        "Dropped in planning review: superseded",
    )


def test_route_defer_adhoc_refuses_open_closing_pr(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.closing_prs = {
        "https://github.com/org/.github/pull/41": "MERGED",
        "https://github.com/org/.github/pull/42": "OPEN",
    }
    with pytest.raises(RouteRefusedError, match=r"https://github\.com/org/\.github/pull/42"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.run.assert_not_called()
    assert env.calls("reparent") == []
    env.ensure_deferred_epic.assert_not_called()


def test_route_defer_adhoc_ignores_closed_pr(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    env.closing_prs = {
        "https://github.com/org/.github/pull/41": "MERGED",
        "https://github.com/org/.github/pull/43": "CLOSED",
    }
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]


def test_route_defer_comment_failure_after_move_fails_loudly(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, Defer(reason="waits on X", revisit_when="X ships"), today=TODAY)
    message = str(excinfo.value)
    assert env.native_parent[ITEM] == DEFERRED
    assert "org/.github#352 is under the deferred epic org/.github#10" in message
    assert "without a deferral record" in message
    assert "HTTP 502" in message
    # The full intended comment, including the origin a refresh would not write.
    assert (
        "Deferred 2026-10-23: waits on X\nRevisit when: X ships\nDeferred from: org/.github#7"
        in message
    )


def test_route_defer_label_failure_after_move_fails_loudly(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.labels = ["in-progress"]
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    message = str(excinfo.value)
    assert "without a deferral record" in message
    assert "--remove-label in-progress" in message
    assert "Deferred 2026-10-23: r\nRevisit when: w\nDeferred from: org/.github#8" in message


def test_route_defer_uses_items_repo_queue_home(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    secret_deferred = IssueRef("org", "secret", 3)
    env.ensure_deferred_epic.return_value = secret_deferred
    route(secret_item, Defer(reason="r", revisit_when="w"), today=TODAY)
    env.ensure_deferred_epic.assert_called_once_with("org/secret")
    assert env.calls("add") == [(secret_deferred, secret_item)]


# -- revive (out of deferred) ------------------------------------------------


def test_route_to_intake_from_deferred(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    result = route(ITEM, ToIntake(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.ensure_intake_epic.assert_called_once_with("org/.github")
    assert env.calls("reparent") == [(INTAKE, ITEM)]
    env.run.assert_not_called()


def test_route_to_intake_refused_from_intake(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="the intake route is not allowed"):
        route(ITEM, ToIntake(), today=TODAY)
    env.ensure_intake_epic.assert_not_called()
    assert env.calls("reparent") == []


def test_route_planning_from_deferred(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    route(ITEM, ToPlanning(), today=TODAY)
    assert env.calls("reparent") == [(PLANNING, ITEM)]


def test_route_planning_from_adhoc_reparents(env: Env) -> None:
    # #4598: an ad-hoc task found too big for one PR goes straight to planning.
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    result = route(ITEM, ToPlanning(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.ensure_planning_epic.assert_called_once_with("org/.github")
    assert env.calls("reparent") == [(PLANNING, ITEM)]
    assert env.calls("add") == []
    env.run.assert_not_called()


def test_route_planning_from_adhoc_refuses_open_closing_pr(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    env.closing_prs = {
        "https://github.com/org/.github/pull/41": "MERGED",
        "https://github.com/org/.github/pull/42": "OPEN",
    }
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, ToPlanning(), today=TODAY)
    message = str(excinfo.value)
    assert "https://github.com/org/.github/pull/42" in message
    assert "routing to planning it would strand that work" in message
    env.ensure_planning_epic.assert_not_called()
    assert env.calls("reparent") == []
    env.run.assert_not_called()


def test_route_planning_from_adhoc_ignores_closed_pr(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    env.closing_prs = {
        "https://github.com/org/.github/pull/41": "MERGED",
        "https://github.com/org/.github/pull/43": "CLOSED",
    }
    route(ITEM, ToPlanning(), today=TODAY)
    assert env.calls("reparent") == [(PLANNING, ITEM)]


def test_route_planning_from_intake_does_not_read_closing_prs(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    route(ITEM, ToPlanning(), today=TODAY)
    reads = [c.args[-1] for c in env.read_json.call_args_list]
    assert "closedByPullRequestsReferences" not in reads


def test_route_defer_adhoc_open_closing_pr_names_deferring(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.closing_prs = {"https://github.com/org/.github/pull/42": "OPEN"}
    with pytest.raises(RouteRefusedError, match="deferring it would strand that work"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)


# An open PR closing the item, as the trust layer reads it.
_OPEN_PR_URL = "https://github.com/org/.github/pull/42"
_ITEM_REF = ("org/.github", ITEM.number, "OPEN")


def _open_pr(env: Env, **kwargs: Any) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    env.closing_pr_answers[_OPEN_PR_URL] = trust_stubs.closing_pr(
        "org/.github", 42, _ITEM_REF, state="OPEN", **kwargs
    )


def test_route_defer_adhoc_untrusted_closing_pr_does_not_hold_it(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    """An outsider's open PR naming the item never blocks routing (GHSA-g68j-q2mw-jqf9)."""
    _open_pr(env, author=trust_stubs.OUTSIDER)
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]
    err = capsys.readouterr().err
    assert "skipped untrusted content: org/.github#42 pr drive-by" in err
    assert "open PR org/.github#42 would close it by a reference the trust layer" in err
    assert "Summary." not in err


def test_route_planning_adhoc_untrusted_edit_of_closing_pr_does_not_hold_it(env: Env) -> None:
    _open_pr(env, editor=trust_stubs.OUTSIDER)
    route(ITEM, ToPlanning(), today=TODAY)
    assert env.calls("reparent") == [(PLANNING, ITEM)]


def test_route_defer_adhoc_untrusted_link_does_not_hold_it(env: Env) -> None:
    _open_pr(env, body="No closing line.", links=((trust_stubs.OUTSIDER, "org/.github", 352),))
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]


def test_route_defer_adhoc_trusted_link_holds_it(env: Env) -> None:
    _open_pr(env, body="No closing line.", links=((trust_stubs.AGENT, "org/.github", 352),))
    with pytest.raises(RouteRefusedError, match="deferring it would strand that work"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == []


def test_route_defer_adhoc_pr_of_another_repo_does_not_hold_it(env: Env) -> None:
    """A reference from another repo's PR is not counted, so it never holds the route."""
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    url = "https://github.com/org/tool/pull/7"
    env.closing_pr_answers[url] = trust_stubs.closing_pr("org/tool", 7, _ITEM_REF, state="OPEN")
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == [(DEFERRED, ITEM)]
    assert env.closing_pr_reads == [url]


def test_route_defer_adhoc_closing_pr_read_failure_moves_nothing(env: Env) -> None:
    """A closing PR the layer cannot read in full is a read failure, never 'not open'."""
    _open_pr(env)
    answer = env.closing_pr_answers[_OPEN_PR_URL]["repository"]["pullRequest"]
    answer["closingIssuesReferences"]["totalCount"] = 30
    with pytest.raises(trust.TrustReadError, match="truncated"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == []
    env.ensure_deferred_epic.assert_not_called()


def test_route_defer_adhoc_closing_pr_without_a_pr_url_fails(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    env.closing_prs = {"https://github.com/org/.github/issues/42": "OPEN"}
    with pytest.raises(RuntimeError, match="has no PR URL"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == []
    assert env.closing_pr_reads == []


def test_route_defer_adhoc_closing_pr_reported_as_counts_under_actions(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _open_pr(env, author=trust_stubs.OUTSIDER)
    route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    err = capsys.readouterr().err
    assert "skipped 1 untrusted item(s) (untrusted: 1)" in err
    assert "drive-by" not in err


def test_route_one_off_from_deferred_same_repo_reparents(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    result = route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert env.calls("reparent") == [(ADHOC_DOTGITHUB, ITEM)]
    assert _transfer_calls(env) == []


def test_route_split_from_deferred_closes(env: Env) -> None:
    env.parent = DEFERRED
    route(ITEM, Split(into=(IssueRef("org", "tool", 10),), reason="r"), today=TODAY)
    assert env.run.call_args.args[:2] == ("issue", "close")


@pytest.mark.parametrize(
    ("parent", "queue"), [(INTAKE, "intake"), (PLANNING, "planning"), (DEFERRED, "deferred")]
)
def test_route_drop_names_source_queue(env: Env, parent: IssueRef, queue: str) -> None:
    env.parent = parent
    route(ITEM, Drop(reason="obsolete"), today=TODAY)
    assert env.run.call_args.args[-1] == f"Dropped in {queue} review: obsolete"


def test_routes_table_matches_spec() -> None:
    expected: dict[str | None, frozenset[type]] = {
        "intake": frozenset({Drop, Defer, OneOff, ToPlanning, Split}),
        "planning": frozenset({Defer, Drop, OneOff, SizeItem}),
        "ad-hoc": frozenset({Defer, ToPlanning}),
        "deferred": frozenset({Defer, ToIntake, ToPlanning, OneOff, Drop, Split, SizeItem}),
        None: frozenset({ToPlanning}),
    }
    assert expected == intake_route.ROUTES


def test_route_defer_unexpected_issue_view_fails_loudly(env: Env) -> None:
    env.read_json.side_effect = lambda *a: ["not", "a", "dict"]
    with pytest.raises(RuntimeError, match="unexpected 'gh issue view' response"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == []
    env.run.assert_not_called()


def test_route_defer_adhoc_closing_pr_without_url_fails_loudly(env: Env) -> None:
    env.parent = ADHOC_DOTGITHUB
    view = {"labels": [], "comments": [], "closedByPullRequestsReferences": [{"number": 5}]}
    env.read_json.side_effect = lambda *a: view
    with pytest.raises(RuntimeError, match="has no URL"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    assert env.calls("reparent") == []
    env.run.assert_not_called()


@pytest.mark.parametrize(
    ("parent", "action", "name"),
    [
        (PLANNING, ToPlanning(), "planning"),
        (PLANNING, Split(into=(IssueRef("org", "tool", 10),), reason="r"), "split"),
        (ADHOC_DOTGITHUB, Drop(reason="x"), "drop"),
        (ADHOC_DOTGITHUB, ToIntake(), "intake"),
    ],
)
def test_route_refuses_routes_outside_the_table(
    env: Env, parent: IssueRef, action: Any, name: str
) -> None:
    env.parent = parent
    with pytest.raises(RouteRefusedError, match=f"the {name} route is not allowed"):
        route(ITEM, action, today=TODAY)
    env.run.assert_not_called()
    assert env.calls("reparent") == []
    env.resolve_epic_ref.assert_not_called()


def test_route_split_closes_with_links(env: Env) -> None:
    into = (IssueRef("org", "tool", 10), IssueRef("org", "lib", 11))
    result = route(ITEM, Split(into=into, reason="two separate fixes"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "completed",
        "--comment",
        "Split into: org/tool#10, org/lib#11. two separate fixes",
    )


def test_route_split_refuses_empty_into(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="at least one"):
        route(ITEM, Split(into=(), reason="x"), today=TODAY)
    env.run.assert_not_called()


# -- eligibility -------------------------------------------------------------


def test_route_refuses_development_epic_task(env: Env) -> None:
    env.parent = DEV_EPIC
    with pytest.raises(RouteRefusedError, match=r"under org/\.github#400, not a container epic"):
        route(ITEM, Drop(reason="x"), today=TODAY)
    env.run.assert_not_called()


def test_route_refuses_unparented_item_for_non_planning(env: Env) -> None:
    env.parent = None
    with pytest.raises(RouteRefusedError, match="has no parent"):
        route(ITEM, Drop(reason="x"), today=TODAY)
    env.run.assert_not_called()


def test_route_refuses_planning_item_for_planning(env: Env) -> None:
    env.parent = PLANNING
    with pytest.raises(RouteRefusedError, match="the planning route is not allowed"):
        route(ITEM, ToPlanning(), today=TODAY)
    assert env.calls("reparent") == []


def test_route_runs_under_item_owner_token(env: Env) -> None:
    seen: list[str | None] = []

    def fake_close(*args: str) -> None:
        seen.append(github._target_org.get())

    env.run.side_effect = fake_close
    route(ITEM, Drop(reason="x"), today=TODAY)
    assert seen == ["org"]


# -- planning ----------------------------------------------------------------


def test_route_planning_reparents_to_planning_epic(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    result = route(ITEM, ToPlanning(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.ensure_planning_epic.assert_called_once_with("org/.github")
    # The intake parent is replaced atomically, never unlinked first.
    assert env.calls("reparent") == [(PLANNING, ITEM)]
    assert env.calls("add") == []


def test_route_planning_accepts_unparented_issue(env: Env) -> None:
    env.parent = None
    result = route(ITEM, ToPlanning(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert env.calls("add") == [(PLANNING, ITEM)]
    assert env.calls("reparent") == []


def test_route_planning_uses_items_repo_queue_home(env: Env) -> None:
    secret_item = IssueRef("org", "secret", 5)
    secret_planning = IssueRef("org", "secret", 2)
    env.ensure_planning_epic.return_value = secret_planning
    env.native_parent[secret_item] = IssueRef("org", "secret", 1)
    route(secret_item, ToPlanning(), today=TODAY)
    env.ensure_planning_epic.assert_called_once_with("org/secret")
    assert env.calls("reparent") == [(secret_planning, secret_item)]


def test_route_planning_ensures_in_progress_label_in_items_repo(env: Env) -> None:
    # Routing a member-repo item to planning goes through the real
    # ensure_planning_epic, which ensures 'in-progress' in the item's repo as
    # well as the home, so epic-create can label it (#3520).
    tool_item = IssueRef("org", "tool", 36)
    env.native_parent[tool_item] = INTAKE
    with (
        patch(f"{_MOD}.epics.ensure_planning_epic", _REAL_ENSURE_PLANNING_EPIC),
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/.github"),
        patch(f"{_MOD}.epics._find_epic_by_title", return_value=PLANNING),
        patch(f"{_MOD}.epics.ensure_labels") as mock_ensure,
    ):
        route(tool_item, ToPlanning(), today=TODAY)
    assert [c.args for c in mock_ensure.call_args_list] == [
        ("org/.github", ("in-progress",)),
        ("org/tool", ("in-progress",)),
    ]
    assert env.calls("reparent") == [(PLANNING, tool_item)]


# -- planning: a follow-on brainstorm out of a development epic (#4296) -------


def _brainstorm(env: Env, title: str = "Follow-on brainstorm: what comes next") -> None:
    """The item is a follow-on brainstorm natively under the development epic."""
    env.parent = DEV_EPIC
    env.native_parent[ITEM] = DEV_EPIC
    env.issue_view = {**env.issue_view, "title": title, "labels": []}


def test_route_planning_moves_brainstorm_from_development_epic(env: Env) -> None:
    _brainstorm(env)
    result = route(ITEM, ToPlanning(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    # The development epic is replaced atomically, never unlinked first.
    assert env.calls("reparent") == [(PLANNING, ITEM)]
    assert env.calls("remove") == []
    assert env.native_parent[ITEM] == PLANNING
    assert [c.args for c in _comment_calls(env)] == [
        (
            "issue",
            "comment",
            "400",
            "--repo",
            "org/.github",
            "--body",
            "Follow-on brainstorm handed to planning: org/.github#352 (now under org/.github#8)",
        ),
        (
            "issue",
            "comment",
            "352",
            "--repo",
            "org/.github",
            "--body",
            "Handed to planning from: org/.github#400",
        ),
    ]


def test_route_planning_records_brainstorm_only_after_the_move(env: Env) -> None:
    _brainstorm(env)
    order: list[str] = []
    env.effects["reparent"] = lambda *a: order.append("link")
    env.run.side_effect = lambda *args: order.append("comment")
    route(ITEM, ToPlanning(), today=TODAY)
    assert order == ["link", "comment", "comment"]


@pytest.mark.parametrize(
    "title",
    [
        "Make the thing faster",
        "follow-on brainstorm: lower case",
        "Brainstorm: Follow-on brainstorm: not a prefix",
    ],
)
def test_route_planning_still_refuses_other_development_epic_tasks(env: Env, title: str) -> None:
    _brainstorm(env, title)
    with pytest.raises(RouteRefusedError, match=r"under org/\.github#400, not a container epic"):
        route(ITEM, ToPlanning(), today=TODAY)
    _assert_untouched(env)


def test_route_brainstorm_only_takes_the_planning_route(env: Env) -> None:
    _brainstorm(env)
    with pytest.raises(RouteRefusedError, match="not a container epic"):
        route(ITEM, Defer(reason="r", revisit_when="w"), today=TODAY)
    _assert_untouched(env)


def test_route_planning_refuses_brainstorm_that_is_an_epic(env: Env) -> None:
    _brainstorm(env)
    env.issue_view = {**env.issue_view, "labels": [{"name": "epic"}]}
    with pytest.raises(RouteRefusedError, match="not a container epic"):
        route(ITEM, ToPlanning(), today=TODAY)
    _assert_untouched(env)


def test_route_planning_refuses_brainstorm_under_a_non_epic(env: Env) -> None:
    _brainstorm(env)
    env.epic_refs.discard(DEV_EPIC)
    with pytest.raises(RouteRefusedError, match="not a container epic"):
        route(ITEM, ToPlanning(), today=TODAY)
    _assert_untouched(env)


def test_route_planning_refuses_brainstorm_linked_only_by_body_line(env: Env) -> None:
    # parent_of falls back to a body 'Parent:' line; a reparent would leave it stale.
    _brainstorm(env)
    env.native_parent[ITEM] = None
    with pytest.raises(RouteRefusedError, match="not a container epic"):
        route(ITEM, ToPlanning(), today=TODAY)
    _assert_untouched(env)


def test_route_planning_brainstorm_epic_record_failure_names_both_comments(env: Env) -> None:
    _brainstorm(env)
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, ToPlanning(), today=TODAY)
    message = str(excinfo.value)
    assert env.native_parent[ITEM] == PLANNING
    assert (
        "org/.github#352 moved from org/.github#400 to the planning epic org/.github#8" in message
    )
    assert "HTTP 502" in message
    assert (
        "on org/.github#400: Follow-on brainstorm handed to planning: org/.github#352 "
        "(now under org/.github#8)"
    ) in message
    assert "on org/.github#352: Handed to planning from: org/.github#400" in message


def test_route_planning_brainstorm_item_record_failure_names_only_that_comment(env: Env) -> None:
    _brainstorm(env)
    env.run.side_effect = [None, github.GitHubAPIError(1, ["gh"], "", "HTTP 502")]
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, ToPlanning(), today=TODAY)
    message = str(excinfo.value)
    assert "on org/.github#400" not in message
    assert "on org/.github#352: Handed to planning from: org/.github#400" in message


# -- one-off -----------------------------------------------------------------


def test_route_one_off_same_repo_reparents_only(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    result = route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.resolve_epic_ref.assert_called_once_with("adhoc", repo="org/.github")
    assert _transfer_calls(env) == []
    assert env.calls("reparent") == [(ADHOC_DOTGITHUB, ITEM)]
    env.create_issue.assert_not_called()


def test_route_one_off_same_repo_parent_mismatch_fails_loudly(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    env.effects["reparent"] = _no_effect  # the link never takes
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert str(excinfo.value) == (
        "linked org/.github#352, but it is under org/.github#7, not under org/.github#9; "
        "the route is half-applied. Fix it with: "
        "vrg-epic-move --task 'org/.github#352' --epic 'org/.github#9'"
    )


def test_route_one_off_transfers_then_links(env: Env) -> None:
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    new = IssueRef("org", "tool", 900)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert _transfer_calls(env) == [
        call("issue", "transfer", "352", "org/tool", "--repo", "org/.github")
    ]
    env.resolve_epic_ref.assert_called_once_with("adhoc", repo="org/tool")
    # The transfer dropped the sub-issue link; it is still linked with
    # replaceParent, in case the old parent surfaces before the link lands.
    assert env.calls("reparent") == [(TOOL_ADHOC, new)]
    assert env.calls("add") == []
    assert env.native_parent[new] == TOOL_ADHOC
    # One settle wait before the first read, one more before the confirming read.
    assert env.sleep.call_args_list == [call(intake_route._SETTLE_SECONDS)] * 2
    env.create_issue.assert_not_called()
    env.run.assert_not_called()


def test_route_one_off_transfer_kept_link_is_replaced(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert env.calls("reparent") == [(TOOL_ADHOC, new)]
    assert env.calls("add") == []
    assert env.native_parent[new] == TOOL_ADHOC


def test_route_one_off_transfer_late_parent_is_replaced_again(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    new = IssueRef("org", "tool", 900)
    # The transfer carries the intake parent over only after the first link,
    # overwriting it (the silent #3556 failure); the next round replaces it.
    late = iter([INTAKE])

    def carry_over(*_: object) -> None:
        env.native_parent[new] = next(late, TOOL_ADHOC)

    env.sleep.side_effect = carry_over
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert env.calls("reparent") == [(TOOL_ADHOC, new), (TOOL_ADHOC, new)]
    assert env.native_parent[new] == TOOL_ADHOC
    err = capsys.readouterr().err
    assert "org/tool#900 is under org/.github#7 after linking it under org/.github#11" in err
    assert "(round 1/4); replacing the parent" in err


def test_route_one_off_transfer_parent_mismatch_fails_loudly(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    env.effects["reparent"] = _no_effect  # the link never takes
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert "but it is under org/.github#7, not under org/.github#11" in message
    assert "half-applied" in message
    assert "vrg-epic-move --task 'org/tool#900' --epic 'org/.github#11'" in message
    assert len(env.calls("reparent")) == intake_route._SETTLE_ROUNDS
    assert capsys.readouterr().err.count("replacing the parent") == intake_route._SETTLE_ROUNDS


def test_route_one_off_transfer_parent_reverting_after_a_matching_read_is_replaced(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    new = IssueRef("org", "tool", 900)
    # The first read after the link sees the target, but the carry-over lands
    # just after it (#4004): the confirming read catches it and the next round
    # replaces the parent again.
    sleeps = iter(range(1, 100))

    def carry_over_after_first_read(*_: object) -> None:
        if next(sleeps) == 2:
            env.native_parent[new] = INTAKE

    env.sleep.side_effect = carry_over_after_first_read
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert env.calls("reparent") == [(TOOL_ADHOC, new), (TOOL_ADHOC, new)]
    assert env.native_parent[new] == TOOL_ADHOC
    err = capsys.readouterr().err
    assert "org/tool#900 is under org/.github#7 after linking it under org/.github#11" in err
    assert "(round 1/4); replacing the parent" in err


def test_route_one_off_transfer_parent_that_never_holds_fails_loudly(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    # The silent #4004 failure: every read right after a link sees the target
    # epic, and every time the intake parent comes back a moment later. A
    # single matching read must not report the route done.
    sleeps = iter(range(1, 100))

    def carry_over_after_each_read(*_: object) -> None:
        if next(sleeps) % 2 == 0:
            env.native_parent[new] = INTAKE

    env.sleep.side_effect = carry_over_after_each_read
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith(
        "moved org/.github#352 to org/tool#900, but it is under org/.github#7"
    )
    assert "not under org/.github#11; the route is half-applied" in message
    assert "vrg-epic-move --task 'org/tool#900' --epic 'org/.github#11'" in message
    assert len(env.calls("reparent")) == intake_route._SETTLE_ROUNDS


def test_route_one_off_transfer_still_listed_under_intake_fails_loudly(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    new = IssueRef("org", "tool", 900)
    # The parent field reads the target, but the intake epic's sub-issue list
    # (what vrg-queue intake reads) still holds the moved item.
    env.stale_listing[INTAKE] = {new}
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert (
        "moved org/.github#352 to org/tool#900, but it is still listed as a sub-issue of "
        "org/.github#7 (its parent reads org/.github#11), not settled under org/.github#11"
    ) in message
    # vrg-epic-move would read the target parent and do nothing: name a real recovery.
    assert "vrg-epic-move" not in message
    assert "Re-check with vrg-queue in a minute" in message
    assert (
        "clear it with: vrg-intake-route 'org/tool#900' one-off --repo org/tool "
        "--epic 'org/.github#11'"
    ) in message
    err = capsys.readouterr().err
    assert err.count("still listed as a sub-issue of org/.github#7") == intake_route._SETTLE_ROUNDS
    assert "(round 1/4); checking again" in err


def test_route_one_off_transfer_stale_listing_that_clears_settles(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.stale_listing[INTAKE] = {new}
    sleeps = iter(range(1, 100))

    def listing_catches_up(*_: object) -> None:
        if next(sleeps) == 3:
            env.stale_listing.clear()

    env.sleep.side_effect = listing_catches_up
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert env.calls("reparent") == [(TOOL_ADHOC, new)]


def test_route_one_off_transfer_duplicate_refusal_clears_the_old_link(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    new = IssueRef("org", "tool", 900)
    # After the transfer GitHub lists the item under the target epic while its
    # parent field still reads the intake epic, so replaceParent is refused
    # with "Issue may not contain duplicate sub-issues" (#3839).
    env.native_parent[new] = INTAKE
    env.stale_listing[TOOL_ADHOC] = {new}
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert env.calls("remove") == [(INTAKE, new)]
    assert env.native_parent[new] == TOOL_ADHOC
    # The recovery is followed by the settle loop's two-read check.
    assert env.sleep.call_args_list == [call(intake_route._SETTLE_SECONDS)] * 2
    err = capsys.readouterr().err
    assert (
        "intake-route: org/.github#11 already lists org/tool#900 as a sub-issue, but its "
        "parent reads org/.github#7; removing the stale link from org/.github#7"
    ) in err


def test_route_one_off_same_repo_rerun_duplicate_refusal_clears_and_settles(env: Env) -> None:
    # The reported re-run (vrg-intake-route <new ref> one-off --repo <same repo>)
    # on an item left in the duplicate state.
    env.native_parent[ITEM] = INTAKE
    env.stale_listing[ADHOC_DOTGITHUB] = {ITEM}
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    result = route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert env.calls("remove") == [(INTAKE, ITEM)]
    assert env.native_parent[ITEM] == ADHOC_DOTGITHUB
    # A recovered link gets the settle loop's read-back, not a single read.
    assert env.sleep.call_args_list == [call(intake_route._SETTLE_SECONDS)] * 2


def test_route_one_off_same_repo_without_refusal_reads_once(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert env.calls("remove") == []
    env.sleep.assert_not_called()


def test_route_one_off_duplicate_refusal_while_unparented_relinks(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    # The target lists the item but its parent field reads nothing: the only
    # link to clear is the target's own, then the link is made again.
    env.stale_listing[TOOL_ADHOC] = {new}
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=new, moved_by="transfer")
    assert env.calls("remove") == [(TOOL_ADHOC, new)]
    assert env.calls("reparent") == [(TOOL_ADHOC, new)] * 2
    assert env.native_parent[new] == TOOL_ADHOC


def test_route_one_off_duplicate_refusal_not_listed_fails_cleanly(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    # GitHub refuses the link as a duplicate, but the target's list does not
    # show the item: no known state to recover from, so nothing is removed.
    env.effects["reparent"] = _raising(
        github.GitHubAPIError(1, ["gh", "api", "graphql"], "", DUPLICATE_REFUSAL)
    )
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert str(excinfo.value) == (
        "moved org/.github#352 to org/tool#900, but GitHub refused to link it under "
        "org/.github#11 (Issue may not contain duplicate sub-issues) although "
        "org/.github#11 does not list it; it is under org/.github#7; the route is "
        "half-applied. Set the parent of org/tool#900 to org/.github#11 in the GitHub UI"
    )
    assert env.calls("remove") == []


def test_route_one_off_duplicate_refusal_listing_read_failure_fails_cleanly(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    env.stale_listing[TOOL_ADHOC] = {new}

    def refuse_then_fail_reads(_parent: IssueRef, _child: IssueRef, apply: Any) -> None:
        # Every listing read after the refused link fails, but the one that
        # checks whether the refusal was a full container.
        env.listing_error = github.GitHubAPIError(1, ["gh"], "", "gh: HTTP 502")
        env.listing_reads_left = 1
        apply()

    env.effects["reparent"] = refuse_then_fail_reads
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith(
        "moved org/.github#352 to org/tool#900, but GitHub refused to link it under "
        "org/.github#11 (Issue may not contain duplicate sub-issues) and reading the "
        "sub-issues of org/.github#11 failed ("
    )
    assert message.endswith(
        "); it is under org/.github#7; the route is half-applied. "
        "Set the parent of org/tool#900 to org/.github#11 in the GitHub UI"
    )
    assert env.calls("remove") == []


def test_route_one_off_duplicate_refusal_remove_failure_fails_cleanly(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    env.stale_listing[TOOL_ADHOC] = {new}
    env.effects["remove"] = _raising(
        github.GitHubAPIError(1, ["gh", "api", "graphql"], "", "gh: HTTP 502")
    )
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith(
        "moved org/.github#352 to org/tool#900, but it is under org/.github#7 while "
        "org/.github#11 already lists it as a sub-issue, and removing the stale link "
        "from org/.github#7 failed ("
    )
    assert "gh: HTTP 502" in message
    assert message.endswith(
        "); the route is half-applied. "
        "Set the parent of org/tool#900 to org/.github#11 in the GitHub UI"
    )


def test_route_one_off_duplicate_refusal_that_persists_fails_cleanly(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = INTAKE
    env.stale_listing[TOOL_ADHOC] = {new}
    env.effects["remove"] = _no_effect  # the removal never takes
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    # vrg-epic-move would hit the same refusal: name the fix that works.
    assert str(excinfo.value) == (
        "moved org/.github#352 to org/tool#900, but it is under org/.github#7, not under "
        "org/.github#11, although org/.github#11 lists it as a sub-issue (GitHub refuses "
        "the link: Issue may not contain duplicate sub-issues); the route is half-applied. "
        "Set the parent of org/tool#900 to org/.github#11 in the GitHub UI"
    )
    assert len(env.calls("remove")) == intake_route._SETTLE_ROUNDS
    assert capsys.readouterr().err.count("replacing the parent") == intake_route._SETTLE_ROUNDS


def test_route_one_off_other_link_error_fails_cleanly(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    env.effects["reparent"] = _raising(github.GitHubAPIError(1, ["gh"], "", "gh: HTTP 502"))
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith(
        "org/.github#352: linking org/.github#352 under org/.github#9 or reading its "
        "parent back failed ("
    )
    assert "gh: HTTP 502" in message
    # The pasted command quotes its refs: zsh EXTENDED_GLOB reads an unquoted #.
    assert message.endswith(
        "); the route is half-applied. Check where it is with vrg-queue, then fix it "
        "with: vrg-epic-move --task 'org/.github#352' --epic 'org/.github#9'"
    )
    assert env.calls("remove") == []


def test_route_one_off_transfer_read_back_error_fails_cleanly(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    # The link lands, but reading the sub-issues back fails.

    def link_then_fail_reads(_parent: IssueRef, _child: IssueRef, apply: Any) -> None:
        apply()
        env.listing_error = github.GitHubAPIError(1, ["gh"], "", "gh: HTTP 502")

    env.effects["reparent"] = link_then_fail_reads
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith(
        "moved org/.github#352 to org/tool#900, but linking org/tool#900 under "
        "org/.github#11 or reading its parent back failed ("
    )
    assert message.endswith("vrg-epic-move --task 'org/tool#900' --epic 'org/.github#11'")
    assert env.native_parent[new] == TOOL_ADHOC


def test_route_one_off_missing_token_is_left_as_it_came(env: Env) -> None:
    env.effects["reparent"] = _raising(github.MissingGitHubTokenError(1, ["gh"], "", "no token"))
    with pytest.raises(github.MissingGitHubTokenError):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)


def test_route_planning_duplicate_refusal_is_not_recovered(env: Env) -> None:
    # Only a one-off reads its parent back, so only a one-off recovers.
    env.native_parent[ITEM] = INTAKE
    env.stale_listing[PLANNING] = {ITEM}
    with pytest.raises(github.GitHubAPIError):
        route(ITEM, ToPlanning(), today=TODAY)
    assert env.calls("remove") == []


def test_route_one_off_already_under_epic_is_left(env: Env) -> None:
    new = IssueRef("org", "tool", 900)
    env.native_parent[new] = TOOL_ADHOC
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert env.calls("reparent") == []
    assert env.calls("add") == []


def test_route_refuses_closed_target_epic(env: Env) -> None:
    # Closed is closed (.github#1609): the route is refused naming the epic,
    # which is never reopened, and the item is not moved.
    env.native_parent[ITEM] = INTAKE
    env.epic_state = "closed"
    finite = IssueRef("org", ".github", 50)
    env.resolve_epic_ref.return_value = finite
    with pytest.raises(epics.ClosedEpicError, match=r"epic org/\.github#50 is closed"):
        route(ITEM, OneOff(repo="org/.github", epic="#50"), today=TODAY)
    env.resolve_epic_ref.assert_called_once_with("#50", repo="org/.github")
    env.run.assert_not_called()
    assert env.calls("reparent") == []
    assert env.calls("add") == []
    assert env.native_parent[ITEM] == INTAKE


def test_route_refuses_closed_target_epic_before_transfer(env: Env) -> None:
    # A one-off into another repo is refused before the transfer, so a closed
    # epic leaves the item where it was.
    env.native_parent[ITEM] = INTAKE
    env.epic_state = "closed"
    with pytest.raises(epics.ClosedEpicError):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.run.assert_not_called()
    assert env.calls("reparent") == []
    assert env.calls("remove") == []
    env.create_issue.assert_not_called()
    assert not [c for c in env.read_output.call_args_list if c.args[:2] == ("issue", "transfer")]


def test_route_one_off_open_epic_is_not_reopened(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    env.run.assert_not_called()


def test_route_one_off_falls_back_to_recreate_when_transfer_fails(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.transfer = github.GitHubAPIError(
        1, ["gh", "issue", "transfer"], "", "GraphQL: Resource not accessible by integration"
    )
    order: list[str] = []
    env.create_issue.side_effect = lambda **kw: order.append("create") or env.created_url
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    env.effects["add"] = lambda *a: order.append("link") or a[2]()

    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)

    new = IssueRef("org", "tool", 901)
    assert result == RouteResult(final=new, moved_by="recreate")
    kwargs = env.create_issue.call_args.kwargs
    assert kwargs["repo"] == "org/tool"
    assert kwargs["title"] == "Make the thing faster"
    assert kwargs["body"] == f"It is slow.\n\nMoved from {ORIG_URL}\n"
    # Retired intake labels are not carried; registry labels are, ensured first.
    assert kwargs["labels"] == ["bug"]
    env.ensure_labels.assert_called_once_with("org/tool", ["bug"])
    env.run.assert_called_once_with(
        "issue",
        "close",
        "352",
        "--repo",
        "org/.github",
        "--reason",
        "not planned",
        "--comment",
        f"Moved to {env.created_url}",
    )
    assert env.calls("add") == [(TOOL_ADHOC, new)]
    # Never half-moved: the copy exists before the original closes, then it is linked.
    assert order == ["create", "issue:close", "link"]
    err = capsys.readouterr().err
    assert "transfer failed (" in err
    assert "Resource not accessible by integration" in err
    assert f"); recreated as {env.created_url}" in err
    assert "label(s) not carried to org/tool#901 (not in the label registry): off-registry" in err
    # A recreated issue has no parent to carry over: no settle wait.
    assert env.calls("reparent") == []
    env.sleep.assert_not_called()


def test_route_recreate_parent_mismatch_fails_loudly(env: Env) -> None:
    env.transfer = github.GitHubAPIError(1, ["gh"], "", "HTTP 422")
    env.effects["add"] = _no_effect  # the link never takes
    with pytest.raises(RuntimeError, match=r"but it is unparented, not under org/\.github#11"):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert env.calls("add") == [(TOOL_ADHOC, IssueRef("org", "tool", 901))]


def test_route_recreate_with_empty_body_and_no_labels(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    env.transfer = github.GitHubAPIError(1, ["gh"], "", "HTTP 422")
    env.issue_view = {"title": "T", "body": None, "labels": [{"name": "idea"}], "url": ORIG_URL}
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    kwargs = env.create_issue.call_args.kwargs
    assert kwargs["body"] == f"Moved from {ORIG_URL}\n"
    assert kwargs["labels"] == []
    env.ensure_labels.assert_not_called()
    assert "not carried" not in capsys.readouterr().err


def test_route_one_off_missing_token_is_not_recreated(env: Env) -> None:
    env.transfer = github.MissingGitHubTokenError(1, ["gh"], "", "GH_TOKEN environment variable")
    with pytest.raises(github.MissingGitHubTokenError):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.create_issue.assert_not_called()


def test_route_one_off_unreadable_transfer_output_fails_loudly(env: Env) -> None:
    env.transfer = "done"
    with pytest.raises(RuntimeError, match="could not read the new issue URL"):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.create_issue.assert_not_called()
    assert env.calls("add") == []


def test_route_one_off_refuses_cross_org_repo(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="cross-org"):
        route(ITEM, OneOff(repo="other/tool"), today=TODAY)
    env.resolve_epic_ref.assert_not_called()


def test_route_one_off_refuses_malformed_repo(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="owner/repo"):
        route(ITEM, OneOff(repo="tool"), today=TODAY)


def test_route_one_off_refuses_cross_org_epic(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="cross-org"):
        route(ITEM, OneOff(repo="org/tool", epic="other/.github#3"), today=TODAY)
    env.resolve_epic_ref.assert_not_called()


def test_one_off_refuses_hoshin_sentinel(env: Env) -> None:
    with pytest.raises(RouteRefusedError, match="/vergil:hoshin"):
        route(ITEM, OneOff(repo="org/tool", epic="hoshin"), today=TODAY)
    # Refused by name before anything resolves: no epic is created.
    env.resolve_epic_ref.assert_not_called()
    env.create_issue.assert_not_called()
    assert _transfer_calls(env) == []
    assert env.calls("add") == []
    assert env.calls("reparent") == []


@pytest.mark.parametrize(
    "action",
    [Defer(reason="r", revisit_when="w"), ToPlanning(), Drop(reason="r")],
    ids=["defer", "planning", "drop"],
)
def test_route_refuses_item_under_hoshin_epic(env: Env, action: RouteAction) -> None:
    hoshin = IssueRef("org", ".github", 77)
    env.kinds[hoshin] = "hoshin"
    env.parent = hoshin
    env.native_parent[ITEM] = hoshin
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, action, today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("org/.github#352 is under the hoshin epic org/.github#77")
    assert "/vergil:hoshin" in message
    # Not the development-epic refusal, which would misdescribe the hoshin epic.
    assert "not a container epic" not in message
    _assert_untouched(env)


def test_one_off_refuses_hoshin_epic_ref(env: Env) -> None:
    hoshin = IssueRef("org", ".github", 77)
    env.kinds[hoshin] = "hoshin"
    env.resolve_epic_ref.return_value = hoshin
    with pytest.raises(RouteRefusedError, match="org/.github#77 is the hoshin epic"):
        route(ITEM, OneOff(repo="org/tool", epic="org/.github#77"), today=TODAY)
    assert _transfer_calls(env) == []
    assert env.calls("add") == []
    assert env.calls("reparent") == []


@pytest.mark.parametrize(
    "action",
    [
        Defer(reason="r", revisit_when="w"),
        ToPlanning(),
        Drop(reason="r"),
        OneOff(repo="org/tool"),
        Split(into=(IssueRef("org", "tool", 10),), reason="r"),
    ],
    ids=["defer", "planning", "drop", "one-off", "split"],
)
def test_route_refuses_item_under_operations(env: Env, action: RouteAction) -> None:
    # An ops issue stays under the operations epic until it is closed by hand.
    env.parent = OPERATIONS
    env.native_parent[ITEM] = OPERATIONS
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, action, today=TODAY)
    message = str(excinfo.value)
    assert message == (
        "org/.github#352 is an ops issue under org/.github#12; it is closed by hand once "
        "dealt with, never routed"
    )
    _assert_untouched(env)
    env.resolve_epic_ref.assert_not_called()
    assert env.calls("add") == []
    assert env.calls("remove") == []
    assert _transfer_calls(env) == []


def test_route_one_off_into_operations_allowed(env: Env) -> None:
    # A hand-captured operational problem reaches the operations queue through intake.
    env.native_parent[ITEM] = INTAKE
    env.resolve_epic_ref.return_value = OPERATIONS
    result = route(ITEM, OneOff(repo="org/.github", epic="operations"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    env.resolve_epic_ref.assert_called_once_with("operations", repo="org/.github")
    assert env.calls("reparent") == [(OPERATIONS, ITEM)]
    assert env.native_parent[ITEM] == OPERATIONS


def test_route_one_off_rerun_into_operations_clears_a_stale_queue_listing(env: Env) -> None:
    # A re-run of a one-off into the operations epic finishes the move (#4291)
    # rather than being refused as a route out of it.
    moved = IssueRef("org", "tool", 900)
    env.parent = OPERATIONS
    env.native_parent[moved] = OPERATIONS
    env.stale_listing[INTAKE] = {moved}
    result = route(moved, OneOff(repo="org/tool", epic="operations"), today=TODAY)
    assert result == RouteResult(final=moved, moved_by="none")
    assert env.calls("remove") == [(INTAKE, moved)]
    assert env.native_parent[moved] == OPERATIONS
    env.resolve_epic_ref.assert_not_called()


def test_route_one_off_refuses_queue_epic(env: Env) -> None:
    env.resolve_epic_ref.return_value = INTAKE
    with pytest.raises(RouteRefusedError, match="queue epic"):
        route(ITEM, OneOff(repo="org/tool", epic="intake"), today=TODAY)
    assert _transfer_calls(env) == []


# -- security items keep the advisory link (epic vergil-project/.github#517) ----

SPLIT_INTO = (IssueRef("org", "tool", 10), IssueRef("org", "lib", 11))
RETARGET_FAILED = security_records.RetargetError(
    f"advisory {GHSA}: marking org/tool#10 failed: HTTP 500"
)


def _transfer_fails(env: Env) -> None:
    env.transfer = github.GitHubAPIError(1, ["gh"], "", "HTTP 422")


def test_split_security_item_retargets_to_all_tasks_before_close(env: Env) -> None:
    env.make_security_item()
    order: list[str] = []
    env.retarget.side_effect = lambda *a, **kw: order.append("retarget")
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    env.advisory_home.assert_called_once_with("org")
    env.retarget.assert_called_once_with("org/.github", GHSA, SPLIT_INTO, source=ITEM, renamed=())
    assert order == ["retarget", "issue:close"]


@pytest.mark.parametrize(
    ("author", "editor", "kind", "login"),
    [
        (trust_stubs.OUTSIDER, None, "issue", "drive-by"),
        (trust_stubs.HUMAN, trust_stubs.OUTSIDER, "edit", "drive-by"),
        (trust_stubs.IMPOSTOR, None, "issue", "vergil-user"),
    ],
)
@pytest.mark.parametrize(
    "action",
    [Split(into=SPLIT_INTO, reason="two fixes"), OneOff(repo="org/tool")],
    ids=["split", "one-off"],
)
def test_route_security_lines_untrusted_body_raises(
    env: Env, action: RouteAction, author: Any, editor: Any, kind: str, login: str
) -> None:
    # The advisory lines of a body the layer does not trust pick nothing: the
    # route stops before any write, naming only metadata.
    env.make_security_item()
    env.issue_view = {
        **env.issue_view,
        "body": f"{env.issue_view['body']}\n{trust_stubs.SPAM_TEXT}",
    }
    env.body_author, env.body_editor = author, editor
    with pytest.raises(trust.UntrustedContentError) as excinfo:
        route(ITEM, action, today=TODAY)
    assert (excinfo.value.item.kind, excinfo.value.item.login) == (kind, login)
    assert excinfo.value.item.field == "body"
    assert "SPAM-MARKER-TEXT" not in str(excinfo.value)
    env.retarget.assert_not_called()
    env.run.assert_not_called()
    env.read_output.assert_not_called()
    env.create_issue.assert_not_called()
    assert env.calls("remove") == []


def test_recreate_copies_the_trusted_body_read_before_the_move(env: Env) -> None:
    _transfer_fails(env)
    trusted_body = env.issue_view["body"]
    reads = 0

    def graphql(query: str, **variables: Any) -> dict[str, Any]:
        nonlocal reads
        if query in _GRAPH_QUERIES:
            return env.graphql(query, **variables)
        reads += 1
        return trust_stubs.item(body=str(trusted_body))

    # The body gh would show after the failed transfer is never copied.
    env.issue_view = {**env.issue_view, "body": "changed since the read"}
    with patch("vergil_tooling.lib.github.graphql", side_effect=graphql):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    # Every read comes before the move: the outsider-opened check, the trusted
    # comments the size is read from, then the body.
    assert reads == 3
    body = env.create_issue.call_args.kwargs["body"]
    assert body.startswith(f"{trusted_body}\n\n")
    assert "changed since the read" not in body


def test_split_security_item_retarget_failure_leaves_original_open(env: Env) -> None:
    env.make_security_item()
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    message = str(excinfo.value)
    assert f"advisory {GHSA} was not retargeted to org/tool#10, org/lib#11" in message
    assert "marking org/tool#10 failed" in message
    assert "org/.github#352 is left open" in message
    env.run.assert_not_called()


def test_split_security_item_unreadable_home_leaves_original_open(env: Env) -> None:
    env.make_security_item()
    env.tracked_issues.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(RouteRefusedError, match=f"advisory {GHSA} was not retargeted"):
        route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    env.retarget.assert_not_called()
    env.run.assert_not_called()


def test_recreate_security_item_retargets_before_closing(env: Env) -> None:
    env.make_security_item()
    _transfer_fails(env)
    order: list[str] = []
    env.create_issue.side_effect = lambda **kw: order.append("create") or env.created_url
    env.retarget.side_effect = lambda *a, **kw: order.append("retarget")
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    new = IssueRef("org", "tool", 901)
    assert result == RouteResult(final=new, moved_by="recreate")
    env.retarget.assert_called_once_with("org/.github", GHSA, (new,), source=ITEM, renamed=())
    assert order == ["create", "retarget", "issue:close"]
    # The copy carries the label and the advisory line; retarget's mark is idempotent.
    kwargs = env.create_issue.call_args.kwargs
    assert "security" in kwargs["labels"]
    assert ADVISORY_LINE in kwargs["body"]


def test_recreate_security_item_retarget_failure_leaves_original_open(env: Env) -> None:
    env.make_security_item()
    _transfer_fails(env)
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert f"recreated org/.github#352 as org/tool#901, but advisory {GHSA}" in message
    assert "org/.github#352 is left open" in message
    assert (
        f"vrg-security-record retarget {GHSA} --from 'org/.github#352' --to 'org/tool#901'"
        in message
    )
    env.run.assert_not_called()
    assert env.calls("add") == []


def test_transfer_security_item_retargets_to_final(env: Env) -> None:
    env.make_security_item()
    order: list[str] = []
    env.sleep.side_effect = lambda *_: order.append("settle")
    env.retarget.side_effect = lambda *a, **kw: order.append("retarget")
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    final = IssueRef("org", "tool", 900)
    assert result == RouteResult(final=final, moved_by="transfer")
    # The old number is the moved issue itself: rewritten away, never unmarked.
    env.retarget.assert_called_once_with(
        "org/.github", GHSA, (final,), source=ITEM, renamed=(ITEM,)
    )
    # The settle wait and its confirming wait both come before the retarget.
    assert order == ["settle", "settle", "retarget"]
    # The item was read before the move, while it still had its labels: once
    # for its size (and in-progress), once for its advisories. Its comments
    # come from the trust layer, never from gh.
    assert env.read_json.call_args_list == [
        call("issue", "view", "352", "--repo", "org/.github", "--json", "labels"),
        call("issue", "view", "352", "--repo", "org/.github", "--json", "labels"),
    ]


def test_transfer_retarget_failure_reports_moved_not_refused(env: Env) -> None:
    env.make_security_item()
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert not isinstance(excinfo.value, RouteRefusedError)
    assert str(excinfo.value) == (
        f"org/.github#352 moved to org/tool#900, but advisory {GHSA} was not retargeted; "
        f"run: vrg-security-record retarget {GHSA} --from 'org/.github#352' --to 'org/tool#900'"
    )
    assert excinfo.value.__cause__ is RETARGET_FAILED


def test_transfer_parent_mismatch_also_names_the_advisory(env: Env) -> None:
    env.make_security_item()
    env.effects["reparent"] = _no_effect  # the link never takes
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert "half-applied" in message
    assert f"also, advisory {GHSA} was not retargeted" in message
    assert (
        f"vrg-security-record retarget {GHSA} --from 'org/.github#352' --to 'org/tool#900'"
        in message
    )
    env.retarget.assert_not_called()


def test_transfer_link_error_is_half_applied_and_names_the_advisory(env: Env) -> None:
    env.make_security_item()
    env.effects["reparent"] = _raising(RuntimeError("GraphQL errors: [boom]"))
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    # The item has moved, so a failed link leaves the route half-applied
    # (#3839): it names that state, with the advisory recovery added.
    message = str(excinfo.value)
    assert message.startswith(
        "moved org/.github#352 to org/tool#900, but linking org/tool#900 under "
        "org/.github#11 or reading its parent back failed (GraphQL errors: [boom])"
    )
    assert f"; also, advisory {GHSA} was not retargeted" in message
    assert (
        f"vrg-security-record retarget {GHSA} --from 'org/.github#352' --to 'org/tool#900'"
        in message
    )
    env.retarget.assert_not_called()


def test_non_security_item_never_touches_advisories(env: Env) -> None:
    route(ITEM, Split(into=SPLIT_INTO, reason="x"), today=TODAY)
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    _transfer_fails(env)
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    env.advisory_home.assert_not_called()
    env.retarget.assert_not_called()


def test_planning_route_leaves_advisory_on_item(env: Env) -> None:
    env.make_security_item()
    route(ITEM, ToPlanning(), today=TODAY)
    env.read_json.assert_not_called()
    env.advisory_home.assert_not_called()
    env.retarget.assert_not_called()


@pytest.mark.parametrize(
    ("body", "labels", "has", "lacks"),
    [
        ("Neutral.", [{"name": "security"}], "the security label", "a Security advisory: line"),
        (ADVISORY_LINE, [], "a Security advisory: line", "the security label"),
    ],
)
def test_half_marked_item_is_warned_and_not_retargeted(
    env: Env,
    capsys: pytest.CaptureFixture[str],
    body: str,
    labels: list[dict[str, str]],
    has: str,
    lacks: str,
) -> None:
    env.issue_view = {**env.issue_view, "body": body, "labels": labels}
    route(ITEM, Split(into=SPLIT_INTO, reason="x"), today=TODAY)
    env.retarget.assert_not_called()
    env.run.assert_called_once()
    err = capsys.readouterr().err
    assert f"org/.github#352 has {has} but not {lacks}" in err
    assert "no advisory retargeted (a broken security pair)" in err


def test_item_with_no_body_or_labels_is_not_a_security_item(env: Env) -> None:
    env.issue_view = {**env.issue_view, "body": None, "labels": None}
    route(ITEM, Split(into=SPLIT_INTO, reason="x"), today=TODAY)
    env.retarget.assert_not_called()


# -- several advisories per item (epic vergil-project/.github#573) ----------------

GHSA_B = "GHSA-efgh-6789-stuv"
NEW = IssueRef("org", "tool", 901)
FINAL = IssueRef("org", "tool", 900)


def _name_two_advisories(env: Env) -> None:
    env.make_security_item()
    body = str(env.issue_view["body"])
    env.issue_view = {**env.issue_view, "body": f"{body}\nSecurity advisory: org/.github {GHSA_B}"}


def test_transfer_retargets_every_advisory(env: Env) -> None:
    _name_two_advisories(env)
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=FINAL, moved_by="transfer")
    assert env.retarget.call_args_list == [
        call("org/.github", GHSA, (FINAL,), source=ITEM, renamed=(ITEM,)),
        call("org/.github", GHSA_B, (FINAL,), source=ITEM, renamed=(ITEM,)),
    ]


def test_transfer_retarget_failure_names_every_pending_advisory(env: Env) -> None:
    _name_two_advisories(env)
    env.retarget.side_effect = [None, RETARGET_FAILED]
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert not isinstance(excinfo.value, RouteRefusedError)
    # The first advisory was retargeted; only the second is left, with its command.
    assert str(excinfo.value) == (
        f"org/.github#352 moved to org/tool#900, but advisory {GHSA_B} was not retargeted; "
        f"run: vrg-security-record retarget {GHSA_B} --from 'org/.github#352' --to 'org/tool#900'"
    )
    assert excinfo.value.__cause__ is RETARGET_FAILED


def test_transfer_first_retarget_failure_names_both_advisories(env: Env) -> None:
    _name_two_advisories(env)
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert str(excinfo.value) == (
        f"org/.github#352 moved to org/tool#900, but advisories {GHSA}, {GHSA_B} were not "
        f"retargeted; run: vrg-security-record retarget {GHSA} --from 'org/.github#352' "
        "--to 'org/tool#900', then "
        f"vrg-security-record retarget {GHSA_B} --from 'org/.github#352' --to 'org/tool#900'"
    )
    env.retarget.assert_called_once()


def test_transfer_parent_mismatch_names_every_advisory(env: Env) -> None:
    _name_two_advisories(env)
    env.effects["reparent"] = _no_effect  # the link never takes
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert f"also, advisories {GHSA}, {GHSA_B} were not retargeted" in message
    assert (
        f"vrg-security-record retarget {GHSA_B} --from 'org/.github#352' --to 'org/tool#900'"
        in message
    )
    env.retarget.assert_not_called()


def test_recreate_retargets_every_advisory_before_closing(env: Env) -> None:
    _name_two_advisories(env)
    _transfer_fails(env)
    order: list[str] = []
    env.create_issue.side_effect = lambda **kw: order.append("create") or env.created_url
    env.retarget.side_effect = lambda _home, ghsa, *a, **kw: order.append(f"retarget:{ghsa}")
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=NEW, moved_by="recreate")
    assert env.retarget.call_args_list == [
        call("org/.github", GHSA, (NEW,), source=ITEM, renamed=()),
        call("org/.github", GHSA_B, (NEW,), source=ITEM, renamed=()),
    ]
    assert order == ["create", f"retarget:{GHSA}", f"retarget:{GHSA_B}", "issue:close"]


def test_recreate_second_retarget_failure_leaves_original_open(env: Env) -> None:
    _name_two_advisories(env)
    _transfer_fails(env)
    env.retarget.side_effect = [None, RETARGET_FAILED]
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert f"recreated org/.github#352 as org/tool#901, but advisory {GHSA_B} was not" in message
    assert (
        f"vrg-security-record retarget {GHSA_B} --from 'org/.github#352' --to 'org/tool#901'"
        in message
    )
    assert f"retarget {GHSA} " not in message
    assert "org/.github#352 is left open" in message
    env.run.assert_not_called()
    assert env.calls("add") == []


def test_split_refuses_an_item_naming_several_advisories(env: Env) -> None:
    _name_two_advisories(env)
    with pytest.raises(RouteRefusedError) as excinfo:
        route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    assert str(excinfo.value) == (
        f"org/.github#352 names several advisories ({GHSA}, {GHSA_B}); retarget each by hand "
        "to the tasks that fix it, keeping every other issue it tracks (vrg-security-record "
        f"retarget {GHSA} --from 'org/.github#352' --to <task> [--to …], then "
        f"vrg-security-record retarget {GHSA_B} --from 'org/.github#352' --to <task> "
        "[--to …]), then split"
    )
    # Refused before any change: no comment, no close, no retarget.
    env.run.assert_not_called()
    env.retarget.assert_not_called()
    env.advisory_home.assert_not_called()


def test_split_one_advisory_unchanged(env: Env) -> None:
    env.make_security_item()
    route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    env.retarget.assert_called_once_with("org/.github", GHSA, SPLIT_INTO, source=ITEM, renamed=())
    env.run.assert_called_once()
    assert env.run.call_args.args[:2] == ("issue", "close")


OTHER_TASK = IssueRef("org", "lib", 77)


def test_split_keeps_other_tracked_issues(env: Env) -> None:
    """The advisory also tracks open OTHER_TASK: the split keeps it (Review Focus 2)."""
    env.make_security_item()
    env.tracked[GHSA] = (OTHER_TASK, ITEM)
    route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    env.retarget.assert_called_once_with(
        "org/.github", GHSA, (OTHER_TASK, *SPLIT_INTO), source=ITEM, renamed=()
    )
    env.advisory_home.assert_called_once_with("org")
    assert env.run.call_args.args[:2] == ("issue", "close")


def test_split_tracking_read_failure_leaves_original_open(env: Env) -> None:
    env.make_security_item()
    env.tracked_issues.side_effect = security_records.RetargetError(
        f"advisory {GHSA}: reading the tracking lines failed: HTTP 500"
    )
    with pytest.raises(RouteRefusedError, match=f"advisory {GHSA} was not retargeted"):
        route(ITEM, Split(into=SPLIT_INTO, reason="two fixes"), today=TODAY)
    env.retarget.assert_not_called()
    env.run.assert_not_called()


@pytest.mark.parametrize("transfer_fails", [False, True])
def test_transfer_and_recreate_pass_source(env: Env, transfer_fails: bool) -> None:
    env.make_security_item()
    env.tracked[GHSA] = (OTHER_TASK, ITEM)
    if transfer_fails:
        _transfer_fails(env)
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    renamed = () if transfer_fails else (ITEM,)
    env.retarget.assert_called_once_with(
        "org/.github", GHSA, (OTHER_TASK, result.final), source=ITEM, renamed=renamed
    )


def test_transfer_tracking_read_failure_moves_nothing(env: Env) -> None:
    env.make_security_item()
    env.tracked_issues.side_effect = security_records.RetargetError(
        f"advisory {GHSA}: reading the tracking lines failed: HTTP 500"
    )
    with pytest.raises(RouteRefusedError, match="nothing was moved"):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert _transfer_calls(env) == []
    env.retarget.assert_not_called()


def test_recovery_commands_carry_from() -> None:
    targets = {GHSA: (OTHER_TASK, FINAL), GHSA_B: (FINAL,)}
    assert intake_route._retarget_commands((GHSA, GHSA_B), ITEM, targets) == (
        f"vrg-security-record retarget {GHSA} --from 'org/.github#352' --to 'org/lib#77' "
        f"--to 'org/tool#900', then vrg-security-record retarget {GHSA_B} "
        "--from 'org/.github#352' --to 'org/tool#900'"
    )
    assert intake_route._retarget_commands((GHSA,), ITEM, None) == (
        f"vrg-security-record retarget {GHSA} --from 'org/.github#352' --to <task> [--to …]"
    )


def test_recovery_command_keeps_other_tracked_issues(env: Env) -> None:
    env.make_security_item()
    env.tracked[GHSA] = (OTHER_TASK, ITEM)
    env.retarget.side_effect = RETARGET_FAILED
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert str(excinfo.value) == (
        f"org/.github#352 moved to org/tool#900, but advisory {GHSA} was not retargeted; "
        f"run: vrg-security-record retarget {GHSA} --from 'org/.github#352' "
        "--to 'org/lib#77' --to 'org/tool#900'"
    )


# -- one-off: no link left to carry over, and a re-run clears a stale listing (#4291) --

MOVED = IssueRef("org", "tool", 900)


def test_route_one_off_unlinks_from_the_queue_before_the_transfer(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    order: list[str] = []
    env.effects["remove"] = lambda *a: order.append("unlink") or a[2]()
    env.read_output.side_effect = lambda *a: (
        (order.append("transfer") if a[:2] == ("issue", "transfer") else None)
        or env._read_output(*a)
    )
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="transfer")
    assert env.calls("remove") == [(INTAKE, ITEM)]
    assert order == ["unlink", "transfer"]
    assert env.calls("reparent") == [(TOOL_ADHOC, MOVED)]
    assert env.native_parent[MOVED] == TOOL_ADHOC


def test_route_one_off_body_only_parent_is_not_unlinked(env: Env) -> None:
    # parent_of reads the body Parent: line; there is no native link to remove.
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert env.calls("remove") == []


def test_route_one_off_same_repo_is_not_unlinked_first(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert env.calls("remove") == []
    assert env.calls("reparent") == [(ADHOC_DOTGITHUB, ITEM)]


def test_route_one_off_unlink_failure_moves_nothing(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.effects["remove"] = _raising(github.GitHubAPIError(1, ["gh"], "", "HTTP 502"))
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith(
        "unlinking org/.github#352 from org/.github#7 before its transfer failed ("
    )
    assert "HTTP 502" in message
    assert "nothing was moved" in message
    assert "vrg-epic-move --task 'org/.github#352' --epic 'org/.github#7'" in message
    assert _transfer_calls(env) == []


def test_route_one_off_unlink_that_does_not_take_moves_nothing(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.effects["remove"] = _no_effect  # the unlink never takes
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert str(excinfo.value) == (
        "unlinked org/.github#352 from org/.github#7 before its transfer, but it is under "
        "org/.github#7; nothing was moved. Check where it is with vrg-queue, then route it again"
    )
    assert _transfer_calls(env) == []


def test_route_one_off_refused_transfer_relinks_before_the_recreate(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.transfer = github.GitHubAPIError(1, ["gh"], "", "HTTP 422")
    order: list[str] = []
    env.effects["add"] = lambda *a: order.append(f"link {a[0].slug}") or a[2]()
    env.create_issue.side_effect = lambda **kw: order.append("create") or env.created_url
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=IssueRef("org", "tool", 901), moved_by="recreate")
    # Back under its queue epic before the copy is made, as if never unlinked.
    assert order == ["link org/.github#7", "create", "link org/.github#11"]
    assert env.native_parent[ITEM] == INTAKE


def test_route_one_off_relink_failure_after_refused_transfer_fails_loudly(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.transfer = github.GitHubAPIError(1, ["gh"], "", "HTTP 422")
    env.effects["add"] = _raising(github.GitHubAPIError(1, ["gh"], "", "HTTP 502"))
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("the transfer of org/.github#352 to org/tool failed (")
    assert "linking it back under org/.github#7 failed (" in message
    assert "it is unparented and nothing was moved" in message
    assert "vrg-epic-move --task 'org/.github#352' --epic 'org/.github#7'" in message
    env.create_issue.assert_not_called()


def _moved_but_still_listed(env: Env) -> None:
    """The #4168 state: moved and under the ad-hoc epic, the intake epic still listing it."""
    env.parent = TOOL_ADHOC
    env.native_parent[MOVED] = TOOL_ADHOC
    env.stale_listing[INTAKE] = {MOVED}


def test_route_one_off_rerun_clears_a_stale_queue_listing(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    _moved_but_still_listed(env)
    result = route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="none")
    assert env.calls("remove") == [(INTAKE, MOVED)]
    assert env.native_parent[MOVED] == TOOL_ADHOC
    assert MOVED not in env.listing(INTAKE)
    # Read back twice, a settle delay apart.
    assert env.sleep.call_args_list == [call(intake_route._SETTLE_SECONDS)] * 2
    # Never creates or links anything.
    env.resolve_epic_ref.assert_not_called()
    assert env.calls("reparent") == []
    assert _transfer_calls(env) == []
    assert (
        "intake-route: org/tool#900 is under org/.github#11, but org/.github#7 still lists "
        "it as a sub-issue; removing the stale listing"
    ) in capsys.readouterr().err


def test_route_one_off_rerun_as_the_half_applied_message_prints_clears_it(env: Env) -> None:
    # The recovery the still-listed failure prints names the epic in full.
    _moved_but_still_listed(env)
    result = route(MOVED, OneOff(repo="org/tool", epic="org/.github#11"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="none")
    assert env.calls("remove") == [(INTAKE, MOVED)]


def test_route_one_off_rerun_clears_a_stale_listing_under_a_finite_epic(env: Env) -> None:
    finite = IssueRef("org", ".github", 50)
    env.parent = finite
    env.native_parent[MOVED] = finite
    env.stale_listing[PLANNING] = {MOVED}
    env.resolve_epic_ref.return_value = finite
    result = route(MOVED, OneOff(repo="org/tool", epic="#50"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="none")
    assert env.calls("remove") == [(PLANNING, MOVED)]
    # The re-run's target goes through the shared resolution with no sentinel,
    # so a continuation's ref would mean its head and nothing is created.
    env.resolve_epic_ref.assert_called_once_with("#50", repo="org/tool", sentinels=False)


def test_route_one_off_rerun_with_no_stale_listing_is_still_refused(env: Env) -> None:
    env.parent = TOOL_ADHOC
    env.native_parent[MOVED] = TOOL_ADHOC
    with pytest.raises(RouteRefusedError, match="the one-off route is not allowed from ad-hoc"):
        route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    assert env.calls("remove") == []
    env.sleep.assert_not_called()


@pytest.mark.parametrize(
    ("action", "parent"),
    [
        # Another repo: the item has not been moved yet.
        (OneOff(repo="org/lib"), TOOL_ADHOC),
        # Under an ad-hoc epic that is not the target's.
        (OneOff(repo="org/tool"), ADHOC_DOTGITHUB),
        # A finite epic that is not the one named.
        (OneOff(repo="org/tool", epic="#51"), IssueRef("org", ".github", 50)),
    ],
)
def test_route_one_off_rerun_off_target_is_refused_untouched(
    env: Env, action: OneOff, parent: IssueRef
) -> None:
    env.parent = parent
    env.native_parent[MOVED] = parent
    env.stale_listing[INTAKE] = {MOVED}
    with pytest.raises(RouteRefusedError):
        route(MOVED, action, today=TODAY)
    assert env.calls("remove") == []
    assert MOVED in env.listing(INTAKE)


def test_route_one_off_rerun_without_a_target_adhoc_epic_is_refused(env: Env) -> None:
    _moved_but_still_listed(env)
    env.adhoc_epics.clear()  # never created by a refused route
    with pytest.raises(RouteRefusedError):
        route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    assert env.calls("remove") == []
    env.resolve_epic_ref.assert_not_called()


def test_route_one_off_rerun_listing_that_stays_fails_loudly(env: Env) -> None:
    _moved_but_still_listed(env)
    env.effects["remove"] = _no_effect  # the removal never takes
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    assert str(excinfo.value) == (
        "settled org/tool#900 under org/.github#11, but it is still listed as a sub-issue of "
        "org/.github#7 on reading it back. Re-check with vrg-queue in a minute; if it is still "
        "wrong, link org/tool#900 under org/.github#11 in the GitHub UI, and only then remove "
        "it from the sub-issues of any queue epic that still lists it"
    )


def test_route_one_off_rerun_parent_that_moves_fails_loudly(env: Env) -> None:
    _moved_but_still_listed(env)
    env.sleep.side_effect = lambda *_: env.native_parent.update({MOVED: None})
    with pytest.raises(
        intake_route.RouteHalfAppliedError, match="but it is unparented on reading it back"
    ):
        route(MOVED, OneOff(repo="org/tool"), today=TODAY)


def test_route_one_off_rerun_remove_failure_fails_loudly(env: Env) -> None:
    _moved_but_still_listed(env)
    env.effects["remove"] = _raising(github.GitHubAPIError(1, ["gh"], "", "HTTP 502"))
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith(
        "org/tool#900 reads org/.github#11 as its parent but was not settled there, and "
        "settling it or reading it back failed ("
    )
    assert "HTTP 502" in message
    assert "link org/tool#900 under org/.github#11 in the GitHub UI" in message


def test_route_one_off_rerun_missing_token_is_left_as_it_came(env: Env) -> None:
    _moved_but_still_listed(env)
    env.effects["remove"] = _raising(github.MissingGitHubTokenError(1, ["gh"], "", "GH_TOKEN"))
    with pytest.raises(github.MissingGitHubTokenError):
        route(MOVED, OneOff(repo="org/tool"), today=TODAY)


def _parent_field_only(env: Env) -> None:
    """The #4168 follow-up state: the parent field reads the ad-hoc epic, which does
    not list the item; the intake epic still holds its only real link."""
    _moved_but_still_listed(env)
    env.unlisted[TOOL_ADHOC] = {MOVED}


def test_route_one_off_rerun_relinks_before_clearing_when_the_target_lacks_it(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    _parent_field_only(env)
    order: list[str] = []
    env.effects["reparent"] = lambda *a: order.append("link") or a[2]()
    env.effects["remove"] = lambda *a: order.append("unlink") or a[2]()
    with patch.object(epic_chain, "relink", wraps=epic_chain.relink) as relink:
        result = route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="none")
    # The forced re-link goes into the container the parent field names (#4291).
    assert relink.call_args_list == [call(TOOL_ADHOC, MOVED)]
    # Linked under the target first, so removing the queue listing cannot orphan it.
    assert order == ["link", "unlink"]
    assert env.calls("reparent") == [(TOOL_ADHOC, MOVED)]
    assert env.calls("remove") == [(INTAKE, MOVED)]
    assert MOVED in env.listing(TOOL_ADHOC)
    assert MOVED not in env.listing(INTAKE)
    err = capsys.readouterr().err
    assert (
        "intake-route: org/tool#900's parent reads org/.github#11, but org/.github#11 does "
        "not list it as a sub-issue; linking it again"
    ) in err


def test_route_one_off_rerun_relink_with_no_queue_listing_settles(env: Env) -> None:
    env.parent = TOOL_ADHOC
    env.native_parent[MOVED] = TOOL_ADHOC
    env.unlisted[TOOL_ADHOC] = {MOVED}
    result = route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="none")
    assert env.calls("reparent") == [(TOOL_ADHOC, MOVED)]
    assert env.calls("remove") == []


def test_route_one_off_rerun_relink_that_does_not_list_it_removes_nothing(env: Env) -> None:
    _parent_field_only(env)
    env.effects["reparent"] = _no_effect  # the link never lands
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith(
        "linked org/tool#900 under org/.github#11 again, but org/.github#11 still does not "
        "list it; no queue listing was removed."
    )
    assert env.calls("remove") == []
    assert MOVED in env.listing(INTAKE)


def test_route_one_off_transfer_link_the_target_does_not_list_is_linked_again(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    # The parent field reads the target after the first link, but the target's
    # sub-issue list lacks the item: the next round links it again (#4291).
    sleeps = iter(range(1, 100))

    def hide_after_first_link(*_: object) -> None:
        if next(sleeps) == 1:
            env.unlisted[TOOL_ADHOC] = {MOVED}

    env.sleep.side_effect = hide_after_first_link
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="transfer")
    assert env.calls("reparent") == [(TOOL_ADHOC, MOVED)] * 2
    err = capsys.readouterr().err
    assert (
        "org/tool#900 is not listed as a sub-issue of org/.github#11 (its parent reads "
        "org/.github#11) after linking it under org/.github#11 (round 1/4); linking it again"
    ) in err


def test_route_one_off_transfer_never_listed_names_the_rerun(env: Env) -> None:
    env.effects["reparent"] = lambda *a: a[2]() or env.graph.unlist(TOOL_ADHOC, MOVED)
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert str(excinfo.value) == (
        "moved org/.github#352 to org/tool#900, but it is not listed as a sub-issue of "
        "org/.github#11 (its parent reads org/.github#11), not settled under org/.github#11; "
        "the route is half-applied. Fix it with: vrg-intake-route 'org/tool#900' one-off "
        "--repo org/tool --epic 'org/.github#11'"
    )
    assert len(env.calls("reparent")) == intake_route._SETTLE_ROUNDS


def test_route_one_off_same_repo_parent_field_only_fails_loudly(env: Env) -> None:
    # The single read-back of a plain link checks the target's listing too.
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    env.unlisted[ADHOC_DOTGITHUB] = {ITEM}
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    with pytest.raises(intake_route.RouteHalfAppliedError, match="not listed as a sub-issue"):
        route(ITEM, OneOff(repo="org/.github"), today=TODAY)


def test_route_one_off_still_listed_outside_the_rerun_search_names_the_ui(env: Env) -> None:
    # A source repo that keeps its own queues: a re-run of the moved item would
    # not search its intake epic, so the by-hand recovery is named instead.
    own_intake = IssueRef("org", "priv", 5)
    env.parent = own_intake
    env.kinds[own_intake] = "intake"
    env.stale_listing[own_intake] = {MOVED}
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert "still listed as a sub-issue of org/priv#5" in message
    assert "remove org/tool#900 from the sub-issues of org/priv#5 in the GitHub UI" in message
    assert "vrg-intake-route" not in message


def test_route_one_off_still_listed_lookup_failure_names_the_ui(env: Env) -> None:
    env.stale_listing[INTAKE] = {MOVED}
    with (
        patch(
            f"{_MOD}.epics.find_standing_epic",
            side_effect=github.GitHubAPIError(1, ["gh"], "", "HTTP 502"),
        ),
        pytest.raises(intake_route.RouteHalfAppliedError) as excinfo,
    ):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert "remove org/tool#900 from the sub-issues of org/.github#7 in the GitHub UI" in str(
        excinfo.value
    )


def test_route_one_off_still_listed_lookup_missing_token_is_left_as_it_came(env: Env) -> None:
    env.stale_listing[INTAKE] = {MOVED}
    with (
        patch(
            f"{_MOD}.epics.find_standing_epic",
            side_effect=github.MissingGitHubTokenError(1, ["gh"], "", "GH_TOKEN"),
        ),
        pytest.raises(github.MissingGitHubTokenError),
    ):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)


# -- size (epic vergil-project/.github#1191) ---------------------------------

SIZING = Sizing(14, "med", "One sentence.")
SIZING_BODY = (
    "Sized 2026-10-23: epic-size M, about 14 tasks.\n"
    "Epic-size: M\n"
    "Estimated-tasks: 14\n"
    "Confidence: med\n"
    "Reason: One sentence."
)
SIZE_COMMAND = (
    "vrg-intake-route 'org/.github#352' size --tasks 14 --confidence med --reason 'One sentence.'"
)


def _sized_labels(env: Env, *names: str) -> None:
    env.issue_view = {**env.issue_view, "labels": [{"name": name} for name in names]}


def _sizing_calls(env: Env) -> list[tuple[Any, ...]]:
    return [c.args for c in env.run.call_args_list]


def _comment_args(body: str) -> tuple[str, ...]:
    return ("issue", "comment", "352", "--repo", "org/.github", "--body", body)


def test_size_planning_item(env: Env) -> None:
    env.parent = PLANNING
    _sized_labels(env, "bug", "epic-size:S")
    result = route(ITEM, SizeItem(SIZING), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    # The comment (the record) lands before the one label edit.
    assert _sizing_calls(env) == [
        _comment_args(SIZING_BODY),
        (
            "issue",
            "edit",
            "352",
            "--repo",
            "org/.github",
            "--add-label",
            "epic-size:M",
            "--remove-label",
            "epic-size:S",
        ),
    ]
    env.ensure_labels.assert_called_once_with("org/.github", ["epic-size:M"])
    assert env.calls("reparent") == []
    assert env.calls("add") == []


def test_size_removes_every_other_size_label(env: Env) -> None:
    env.parent = PLANNING
    _sized_labels(env, "epic-size:XL", "epic-size:M", "epic-size:S")
    route(ITEM, SizeItem(SIZING), today=TODAY)
    assert _sizing_calls(env)[1] == (
        "issue",
        "edit",
        "352",
        "--repo",
        "org/.github",
        "--add-label",
        "epic-size:M",
        "--remove-label",
        "epic-size:S",
        "--remove-label",
        "epic-size:XL",
    )


def test_size_unsized_item_only_adds(env: Env) -> None:
    env.parent = PLANNING
    _sized_labels(env, "bug")
    route(ITEM, SizeItem(Sizing(45, "low", "Huge.")), today=TODAY)
    assert _sizing_calls(env)[1] == (
        "issue",
        "edit",
        "352",
        "--repo",
        "org/.github",
        "--add-label",
        "epic-size:XL",
    )


def test_size_deferred_item(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    _sized_labels(env)
    route(ITEM, SizeItem(SIZING), today=TODAY)
    assert _sizing_calls(env)[0] == _comment_args(SIZING_BODY)
    # Sizing a deferred item moves nothing.
    assert env.calls("reparent") == []
    assert env.native_parent[ITEM] == DEFERRED


@pytest.mark.parametrize(
    ("parent", "match"),
    [
        (INTAKE, "the size route is not allowed from intake"),
        (ADHOC_DOTGITHUB, "the size route is not allowed from ad-hoc"),
        (DEV_EPIC, r"under org/\.github#400, not a container epic"),
        (None, "has no parent"),
    ],
)
def test_size_refused_outside_planning_and_deferred(
    env: Env, parent: IssueRef | None, match: str
) -> None:
    env.parent = parent
    with pytest.raises(RouteRefusedError, match=match):
        route(ITEM, SizeItem(SIZING), today=TODAY)
    env.run.assert_not_called()
    env.ensure_labels.assert_not_called()


def test_size_label_failure_is_half_applied(env: Env) -> None:
    env.parent = PLANNING
    _sized_labels(env)
    env.run.side_effect = [None, github.GitHubAPIError(1, ["gh"], "", "HTTP 502")]
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, SizeItem(SIZING), today=TODAY)
    message = str(excinfo.value)
    assert "org/.github#352: sizing comment posted, label not set" in message
    assert "HTTP 502" in message
    assert "size --tasks 14 --confidence med --reason" in message
    assert message.endswith(f"Finish it with: {SIZE_COMMAND}")


def test_size_ensure_labels_failure_is_half_applied(env: Env) -> None:
    env.parent = PLANNING
    env.ensure_labels.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 403")
    with pytest.raises(intake_route.RouteHalfAppliedError, match="label not set"):
        route(ITEM, SizeItem(SIZING), today=TODAY)
    # The comment posted; no edit was tried.
    assert _sizing_calls(env) == [_comment_args(SIZING_BODY)]


def test_size_label_read_failure_is_half_applied(env: Env) -> None:
    env.parent = PLANNING
    env.read_json.side_effect = lambda *a: ["not", "a", "dict"]
    with pytest.raises(intake_route.RouteHalfAppliedError, match="label not set"):
        route(ITEM, SizeItem(SIZING), today=TODAY)
    assert _sizing_calls(env) == [_comment_args(SIZING_BODY)]


def test_size_label_missing_token_is_left_as_it_came(env: Env) -> None:
    env.parent = PLANNING
    env.run.side_effect = [None, github.MissingGitHubTokenError(1, ["gh"], "", "GH_TOKEN")]
    with pytest.raises(github.MissingGitHubTokenError):
        route(ITEM, SizeItem(SIZING), today=TODAY)


def test_size_comment_failure_changes_nothing(env: Env) -> None:
    env.parent = PLANNING
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(github.GitHubAPIError):
        route(ITEM, SizeItem(SIZING), today=TODAY)
    assert _sizing_calls(env) == [_comment_args(SIZING_BODY)]
    env.ensure_labels.assert_not_called()


def test_size_half_applied_command_quotes_the_reason(env: Env) -> None:
    env.parent = PLANNING
    env.run.side_effect = [None, github.GitHubAPIError(1, ["gh"], "", "HTTP 502")]
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, SizeItem(Sizing(3, "high", "it's $small")), today=TODAY)
    assert str(excinfo.value).endswith(
        "vrg-intake-route 'org/.github#352' size --tasks 3 --confidence high "
        "--reason 'it'\"'\"'s $small'"
    )


def test_size_comment_uses_route_comment_path(env: Env) -> None:
    # Review Focus 1: the sizing comment goes through _comment, the path the
    # drop, defer and brainstorm records use, under the item owner's token.
    env.parent = PLANNING
    seen: list[str | None] = []
    with patch(
        f"{_MOD}._comment", side_effect=lambda *a: seen.append(github._target_org.get())
    ) as mock_comment:
        route(ITEM, SizeItem(SIZING), today=TODAY)
    mock_comment.assert_called_once_with(ITEM, SIZING_BODY)
    assert seen == ["org"]


def test_planning_with_sizing(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    _sized_labels(env, "bug")
    order: list[str] = []
    env.effects["reparent"] = lambda *a: order.append("link") or a[2]()
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    result = route(ITEM, ToPlanning(sizing=SIZING), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert order == ["link", "issue:comment", "issue:edit"]
    assert env.native_parent[ITEM] == PLANNING
    assert env.run.call_args_list[0].args == _comment_args(SIZING_BODY)
    env.ensure_labels.assert_called_once_with("org/.github", ["epic-size:M"])


def test_planning_from_adhoc_with_sizing(env: Env) -> None:
    # #4598: the ad-hoc route takes the same optional sizing as from intake.
    env.parent = ADHOC_DOTGITHUB
    env.native_parent[ITEM] = ADHOC_DOTGITHUB
    _sized_labels(env, "bug")
    order: list[str] = []
    env.effects["reparent"] = lambda *a: order.append("link") or a[2]()
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    route(ITEM, ToPlanning(sizing=SIZING), today=TODAY)
    assert order == ["link", "issue:comment", "issue:edit"]
    assert env.native_parent[ITEM] == PLANNING
    assert env.run.call_args_list[0].args == _comment_args(SIZING_BODY)
    env.ensure_labels.assert_called_once_with("org/.github", ["epic-size:M"])


def test_planning_with_invalid_sizing_is_refused_before_any_change(env: Env) -> None:
    # Sizing validates on construction, so a bad size never reaches the route.
    with pytest.raises(ValueError, match="positive integer"):
        route(ITEM, ToPlanning(sizing=Sizing(0, "med", "x")), today=TODAY)
    assert env.calls("reparent") == []
    env.ensure_planning_epic.assert_not_called()
    env.run.assert_not_called()


def test_planning_sizing_failure_after_link(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, ToPlanning(sizing=SIZING), today=TODAY)
    message = str(excinfo.value)
    assert env.native_parent[ITEM] == PLANNING
    assert "org/.github#352 is now in the planning epic org/.github#8, unsized" in message
    assert "HTTP 502" in message
    assert message.endswith(f"Size it with: {SIZE_COMMAND}")


def test_planning_label_failure_after_link(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.run.side_effect = [None, github.GitHubAPIError(1, ["gh"], "", "HTTP 502")]
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, ToPlanning(sizing=SIZING), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("org/.github#352 is now in the planning epic org/.github#8, but")
    assert "sizing comment posted, label not set" in message
    assert message.endswith(SIZE_COMMAND)


def test_planning_sizing_missing_token_is_left_as_it_came(env: Env) -> None:
    env.native_parent[ITEM] = INTAKE
    env.run.side_effect = github.MissingGitHubTokenError(1, ["gh"], "", "GH_TOKEN")
    with pytest.raises(github.MissingGitHubTokenError):
        route(ITEM, ToPlanning(sizing=SIZING), today=TODAY)


def test_planning_without_sizing_leaves_size(env: Env) -> None:
    # Review Focus 3: a sized deferred item revived to planning keeps its size.
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    _sized_labels(env, "epic-size:M")
    route(ITEM, ToPlanning(), today=TODAY)
    assert env.calls("reparent") == [(PLANNING, ITEM)]
    env.run.assert_not_called()
    env.ensure_labels.assert_not_called()


def test_planning_from_deferred_with_sizing_resizes(env: Env) -> None:
    env.parent = DEFERRED
    env.native_parent[ITEM] = DEFERRED
    _sized_labels(env, "epic-size:M")
    route(ITEM, ToPlanning(sizing=Sizing(25, "high", "Grew.")), today=TODAY)
    assert _sizing_calls(env)[1] == (
        "issue",
        "edit",
        "352",
        "--repo",
        "org/.github",
        "--add-label",
        "epic-size:L",
        "--remove-label",
        "epic-size:M",
    )


def test_planning_brainstorm_with_sizing_records_then_sizes(env: Env) -> None:
    _brainstorm(env)
    route(ITEM, ToPlanning(sizing=SIZING), today=TODAY)
    calls = _sizing_calls(env)
    assert [c[:3] for c in calls] == [
        ("issue", "comment", "400"),
        ("issue", "comment", "352"),
        ("issue", "comment", "352"),
        ("issue", "edit", "352"),
    ]
    assert calls[2] == _comment_args(SIZING_BODY)


def test_planning_brainstorm_record_failure_with_sizing_names_the_size_command(env: Env) -> None:
    _brainstorm(env)
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(RuntimeError) as excinfo:
        route(ITEM, ToPlanning(sizing=SIZING), today=TODAY)
    message = str(excinfo.value)
    assert "on org/.github#352: Handed to planning from: org/.github#400" in message
    assert message.endswith(f"then size it with: {SIZE_COMMAND}")


# -- a one-off clears the size (epic vergil-project/.github#1191) ------------

CLEARED_MARKER = "Size cleared 2026-10-23: moved out of planning as a one-off."


def _sized_item(env: Env, parent: IssueRef = PLANNING, *, label: bool = True) -> None:
    """An item in *parent* sized M: its label (unless *label* is False) and its comment."""
    env.parent = parent
    env.native_parent[ITEM] = parent
    env.labels = ["epic-size:M"] if label else []
    env.comments = [SIZING_BODY]
    # What the final issue's own label read returns.
    _sized_labels(env, "bug", *(["epic-size:M"] if label else []))


def _cleared_calls(number: str, repo: str) -> list[tuple[str, ...]]:
    return [
        ("issue", "edit", number, "--repo", repo, "--remove-label", "epic-size:M"),
        ("issue", "comment", number, "--repo", repo, "--body", CLEARED_MARKER),
    ]


def test_one_off_transfer_clears_size(env: Env) -> None:
    _sized_item(env)
    order: list[str] = []
    env.effects["reparent"] = lambda *a: order.append("link") or a[2]()
    env.run.side_effect = lambda *a: order.append(f"{a[0]}:{a[1]}")
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="transfer")
    assert _sizing_calls(env) == _cleared_calls("900", "org/tool")
    # Both after the move settled, the label edit before the marker.
    assert order == ["link", "issue:edit", "issue:comment"]


def test_one_off_same_repo_clears_size(env: Env) -> None:
    _sized_item(env)
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    result = route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert _sizing_calls(env) == _cleared_calls("352", "org/.github")


def test_one_off_removes_every_size_label_in_one_edit(env: Env) -> None:
    _sized_item(env)
    _sized_labels(env, "epic-size:S", "bug", "epic-size:M")
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert _sizing_calls(env)[0] == (
        "issue",
        "edit",
        "900",
        "--repo",
        "org/tool",
        "--remove-label",
        "epic-size:M",
        "--remove-label",
        "epic-size:S",
    )


def test_one_off_recreate_drops_size_label(env: Env) -> None:
    _sized_item(env)
    _transfer_fails(env)
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=IssueRef("org", "tool", 901), moved_by="recreate")
    assert env.create_issue.call_args.kwargs["labels"] == ["bug"]
    env.ensure_labels.assert_called_once_with("org/tool", ["bug"])
    # Only the original's close: no label edit and no cleared marker.
    assert [c[:2] for c in _sizing_calls(env)] == [("issue", "close")]


def test_one_off_unsized_item_untouched(env: Env) -> None:
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.comments = ["Sized by hand, not a sizing comment"]
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result.moved_by == "transfer"
    env.run.assert_not_called()


def test_one_off_comment_only_size_gets_marker(env: Env) -> None:
    # The label was lost (removed by hand, or dropped by the transfer): no edit.
    _sized_item(env, label=False)
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert _sizing_calls(env) == _cleared_calls("900", "org/tool")[1:]


def test_one_off_label_only_size_gets_marker(env: Env) -> None:
    _sized_item(env)
    env.comments = []
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert _sizing_calls(env) == _cleared_calls("900", "org/tool")


def test_one_off_transfer_that_dropped_the_size_label_only_posts_marker(env: Env) -> None:
    # The target repo lacks the label, so the transfer dropped it.
    _sized_item(env)
    _sized_labels(env, "bug")
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert _sizing_calls(env) == _cleared_calls("900", "org/tool")[1:]


def test_one_off_clear_failure_is_half_applied(env: Env) -> None:
    _sized_item(env)

    def run(*args: str) -> None:
        if args[:2] == ("issue", "comment"):
            raise github.GitHubAPIError(1, ["gh"], "", "HTTP 502")

    env.run.side_effect = run
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("moved org/.github#352 to org/tool#900, but ")
    assert "posting the cleared marker failed (" in message
    assert "HTTP 502" in message
    assert (
        "gh issue comment 900 --repo org/tool --body "
        "'Size cleared 2026-10-23: moved out of planning as a one-off.'"
    ) in message
    assert env.native_parent[MOVED] == TOOL_ADHOC


def test_one_off_size_label_failure_names_both_fixes(env: Env) -> None:
    _sized_item(env)
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("moved org/.github#352 to org/tool#900, but ")
    assert "routing one-off left the 'epic-size:M' label on org/tool#900" in message
    assert "gh issue edit 900 --repo org/tool --remove-label epic-size:M" in message
    assert message.endswith(
        "then post the cleared marker with: gh issue comment 900 --repo org/tool --body "
        "'Size cleared 2026-10-23: moved out of planning as a one-off.'"
    )
    # The marker is never posted over labels left in place.
    assert [c[:2] for c in _sizing_calls(env)] == [("issue", "edit")]


def test_one_off_size_label_read_failure_is_half_applied(env: Env) -> None:
    _sized_item(env)
    plain_read = env.read_json.side_effect

    def read_json(*args: str) -> object:
        if args[-1] == "labels" and args[2] == "900":
            raise github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
        return plain_read(*args)

    env.read_json.side_effect = read_json
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("moved org/.github#352 to org/tool#900, but ")
    assert "could not read the labels of org/tool#900 to clear its size (" in message
    assert "Remove every 'epic-size:*' label from it, then post the cleared marker" in message
    env.run.assert_not_called()


@pytest.mark.parametrize("step", ["read", "edit", "comment"])
def test_one_off_clear_missing_token_is_left_as_it_came(env: Env, step: str) -> None:
    _sized_item(env)
    missing = github.MissingGitHubTokenError(1, ["gh"], "", "GH_TOKEN environment variable")
    plain_read = env.read_json.side_effect

    def read_json(*args: str) -> object:
        if step == "read" and args[-1] == "labels" and args[2] == "900":
            raise missing
        return plain_read(*args)

    def run(*args: str) -> None:
        if args[1] == step:
            raise missing

    env.read_json.side_effect = read_json
    env.run.side_effect = run
    with pytest.raises(github.MissingGitHubTokenError):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)


def test_one_off_in_progress_and_size_failures_are_reported_together(env: Env) -> None:
    _closed_planning_item(env)
    env.labels = ["in-progress", "epic-size:M"]
    env.comments = [*env.comments, SIZING_BODY]
    _sized_labels(env, "in-progress", "epic-size:M")
    env.run.side_effect = github.GitHubAPIError(1, ["gh"], "", "HTTP 502")
    with pytest.raises(intake_route.RouteHalfAppliedError) as excinfo:
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    message = str(excinfo.value)
    assert message.startswith("moved org/.github#352 to org/tool#900, but ")
    assert "gh issue edit 900 --repo org/tool --remove-label in-progress" in message
    assert "gh issue edit 900 --repo org/tool --remove-label epic-size:M" in message
    # The size was still tried after the in-progress edit failed.
    assert [c[:2] for c in _sizing_calls(env)] == [("issue", "edit"), ("issue", "edit")]


def test_one_off_removes_in_progress_then_clears_size(env: Env) -> None:
    _closed_planning_item(env)
    env.labels = ["in-progress", "epic-size:M"]
    _sized_labels(env, "in-progress", "epic-size:M")
    route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert _sizing_calls(env) == [
        ("issue", "edit", "900", "--repo", "org/tool", "--remove-label", "in-progress"),
        *_cleared_calls("900", "org/tool"),
    ]


def test_one_off_from_intake_clears_size(env: Env) -> None:
    # A sized deferred item revived to intake keeps its size until it leaves.
    _sized_item(env, INTAKE)
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="transfer")
    assert _sizing_calls(env) == _cleared_calls("900", "org/tool")


def test_one_off_from_deferred_clears_size(env: Env) -> None:
    _sized_item(env, DEFERRED)
    env.resolve_epic_ref.return_value = ADHOC_DOTGITHUB
    result = route(ITEM, OneOff(repo="org/.github"), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert _sizing_calls(env) == _cleared_calls("352", "org/.github")


def test_one_off_trusted_authors_error_propagates_before_the_move(env: Env) -> None:
    # The size is read from the comments the trust layer trusts, so a one-off
    # needs the trusted authors; without them nothing moves.
    _sized_item(env)
    unconfigured = RuntimeError("trusted authors are not configured")
    with (
        patch("vergil_tooling.lib.trust.load_trusted_authors", side_effect=unconfigured),
        pytest.raises(RuntimeError, match="trusted authors are not configured"),
    ):
        route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert _transfer_calls(env) == []
    env.run.assert_not_called()
    assert env.calls("reparent") == []


def test_one_off_outsider_sizing_comment_posts_no_cleared_marker(
    env: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    # The skip report lists each skipped comment only outside GitHub Actions.
    env.parent = PLANNING
    env.native_parent[ITEM] = PLANNING
    env.labels = ["idea"]
    _sized_labels(env, "bug")
    forged = SIZING_BODY.replace("Reason: ", f"Reason: {trust_stubs.SPAM_TEXT} ", 1)
    assert forged != SIZING_BODY
    env.comment_nodes = [trust_stubs.comment(11, forged, trust_stubs.OUTSIDER)]
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="transfer")
    # Unsized: no label edit and no cleared marker.
    env.run.assert_not_called()
    captured = capsys.readouterr()
    assert (
        "skipped untrusted content: org/.github#352 comment drive-by at "
        f"{trust_stubs.CREATED} on comment 11 (untrusted)"
    ) in captured.err.splitlines()
    for text in (captured.out, captured.err):
        assert "SPAM-MARKER-TEXT" not in text
        assert "example.invalid" not in text
        assert "Sized " not in text


def test_one_off_target_continuation_uses_head(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A one-off's --epic naming a continuation (copied from GitHub's UI) targets
    # its chain's head, so a re-run compares the item's parent with the head.
    graph = FakeSubIssueGraph().install(monkeypatch)
    head = graph.add_issue(IssueRef("org", ".github", 40), title="Epic: big", labels=["epic"])
    continuation = graph.add_continuation(IssueRef("org", ".github", 41), head, head)
    graph.clear_log()
    target = intake_route._one_off_target(MOVED, OneOff(repo="org/tool", epic=continuation.slug))
    assert target == head
    assert "org/.github#41 is a continuation of org/.github#40; using org/.github#40" in (
        capsys.readouterr().err
    )
    assert graph.events == []


# -- chained queues (epic vergil-project/.github#1609) --------------------------------

# The intake epic's continuation, its chain's second container.
INTAKE_C2 = IssueRef("org", ".github", 70)


@pytest.fixture
def chained_intake(env: Env, monkeypatch: pytest.MonkeyPatch) -> Env:
    """Rollover on, and the intake epic a chain: a full head, then an empty INTAKE_C2."""
    env.fill(INTAKE, epic_chain.ITEM_CAP, start=1000)
    env.add_continuation(INTAKE_C2, INTAKE, INTAKE)
    return env


def test_route_item_from_continuation(chained_intake: Env) -> None:
    # An intake item in the chain's continuation is routed to planning: its
    # parent reads as the intake head, and it leaves the continuation for the
    # planning chain in one atomic re-parent.
    env = chained_intake
    env.native_parent[ITEM] = INTAKE_C2
    with patch(f"{_MOD}.epics.parent_of", _REAL_PARENT_OF):
        result = route(ITEM, ToPlanning(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    assert env.writes == [("reparent", PLANNING, ITEM)]
    assert env.native_parent[ITEM] == PLANNING
    assert ITEM not in env.listing(INTAKE_C2)
    assert ITEM in env.listing(PLANNING)


def test_route_into_full_planning_rolls_over(
    env: Env, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A planning epic holding 99 items rolls over: the route's link creates its
    # continuation, links it into the head's last slot, then links the item.
    env.fill(PLANNING, epic_chain.ITEM_CAP, start=1000)
    env.native_parent[ITEM] = INTAKE
    env.create_issue.side_effect = env.graph.create_issue
    result = route(ITEM, ToPlanning(), today=TODAY)
    assert result == RouteResult(final=ITEM, moved_by="none")
    (continuation,) = [event[1] for event in env.graph.events if event[0] == "create"]
    assert env.writes == [("add", PLANNING, continuation), ("reparent", continuation, ITEM)]
    assert env.native_parent[continuation] == PLANNING
    assert env.native_parent[ITEM] == continuation
    assert len(env.listing(PLANNING)) == epic_chain.SUBISSUE_LIMIT
    assert (
        f"queue {PLANNING.slug} rolled over into continuation {continuation.slug}; "
        "run epic-review planning"
    ) in capsys.readouterr().out


def test_settle_after_transfer_finds_listing_in_continuation(
    chained_intake: Env, capsys: pytest.CaptureFixture[str]
) -> None:
    # The #4004 state with a chained queue: moved and under the ad-hoc epic,
    # while the intake chain's continuation still lists it. A re-run clears
    # the listing from the continuation that holds it, never from the head.
    env = chained_intake
    env.parent = TOOL_ADHOC
    env.native_parent[MOVED] = TOOL_ADHOC
    env.stale_listing[INTAKE_C2] = {MOVED}
    result = route(MOVED, OneOff(repo="org/tool"), today=TODAY)
    assert result == RouteResult(final=MOVED, moved_by="none")
    assert env.writes == [("remove", INTAKE_C2, MOVED)]
    assert env.native_parent[MOVED] == TOOL_ADHOC
    assert MOVED not in env.listing(INTAKE_C2)
    assert MOVED in env.listing(TOOL_ADHOC)
    assert (
        "intake-route: org/tool#900 is under org/.github#11, but org/.github#7 still lists "
        "it as a sub-issue; removing the stale listing"
    ) in capsys.readouterr().err


def test_relink_after_failed_transfer_uses_chain(chained_intake: Env) -> None:
    # An item in the intake chain's continuation is unlinked from that
    # container before its transfer; the transfer is refused, so it is linked
    # back into the chain by first fit (the head is full), then recreated.
    env = chained_intake
    env.native_parent[ITEM] = INTAKE_C2
    _transfer_fails(env)
    result = route(ITEM, OneOff(repo="org/tool"), today=TODAY)
    new = IssueRef("org", "tool", 901)
    assert result == RouteResult(final=new, moved_by="recreate")
    assert env.writes == [
        ("remove", INTAKE_C2, ITEM),
        ("add", INTAKE_C2, ITEM),
        ("add", TOOL_ADHOC, new),
    ]
    assert env.native_parent[ITEM] == INTAKE_C2
    assert len(env.listing(INTAKE)) == epic_chain.SUBISSUE_LIMIT
    assert env.native_parent[new] == TOOL_ADHOC
