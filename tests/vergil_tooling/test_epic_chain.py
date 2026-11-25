"""Tests for vergil_tooling.lib.epic_chain: an epic's chain of containers."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.vergil_tooling import subissue_fake, trust_stubs
from tests.vergil_tooling.subissue_fake import DEPTH_ERROR, LIMIT_ERROR, FakeSubIssueGraph
from vergil_tooling.lib import epic_chain, epics, github
from vergil_tooling.lib.epic_chain import ChainDriftError, Container
from vergil_tooling.lib.epics import EpicRefusalError, IssueRef
from vergil_tooling.lib.labels import load_labels

HEAD = IssueRef("org", ".github", 10)
OTHER_HEAD = IssueRef("org", ".github", 11)
C2 = IssueRef("org", ".github", 20)
C3 = IssueRef("org", ".github", 30)
C2B = IssueRef("org", ".github", 25)

_next_item = iter(range(1000, 100000))


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> FakeSubIssueGraph:
    fake = FakeSubIssueGraph().install(monkeypatch)
    fake.add_issue(HEAD, title="Epic: big", labels=["epic", "epic-development"])
    fake.add_issue(OTHER_HEAD, title="Epic: other", labels=["epic", "epic-development"])
    return fake


def _continuation_body(head: IssueRef, k: int) -> str:
    return (
        f"Holds the overflow of {head.slug}; managed by the tooling.\n\n"
        f"Continues: {head.slug}\nContainer: {k}\n"
    )


def _continuation(
    graph: FakeSubIssueGraph,
    ref: IssueRef,
    under: IssueRef | None,
    *,
    k: int = 2,
    names: IssueRef = HEAD,
    labels: tuple[str, ...] = ("epic", epics.CONTINUATION_LABEL),
    author: object = trust_stubs.HUMAN,
    state: str = "OPEN",
) -> IssueRef:
    graph.add_issue(
        ref,
        title=f"Epic: big (continuation {k})",
        labels=labels,
        body=_continuation_body(names, k),
        author=author,
        state=state,
    )
    if under is not None:
        graph.link(under, ref)
    return ref


def _items(graph: FakeSubIssueGraph, under: IssueRef, count: int) -> list[IssueRef]:
    refs = []
    for _ in range(count):
        ref = graph.add_issue(IssueRef("org", "repo-a", next(_next_item)), title="a task")
        graph.link(under, ref)
        refs.append(ref)
    return refs


# -- the fake's own rules ------------------------------------------------------


def test_fake_refuses_past_the_limit(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 100)
    extra = graph.add_issue(IssueRef("org", "repo-a", 9), title="one too many")
    with pytest.raises(github.GitHubAPIError) as exc:
        graph.link(HEAD, extra)
    assert LIMIT_ERROR.format(child=9, parent=10) in str(exc.value.stderr)


def test_fake_refuses_past_the_depth(graph: FakeSubIssueGraph) -> None:
    parent = HEAD
    for number in range(201, 208):  # levels 2..8 under the head
        ref = graph.add_issue(IssueRef("org", "repo-a", number), title="nested")
        graph.link(parent, ref)
        parent = ref
    deepest = graph.add_issue(IssueRef("org", "repo-a", 300), title="level 9")
    with pytest.raises(github.GitHubAPIError) as exc:
        graph.link(parent, deepest)
    assert DEPTH_ERROR.format(child=300, parent=207) in str(exc.value.stderr)


def test_fake_mutations_go_through_epics(graph: FakeSubIssueGraph) -> None:
    task = graph.add_issue(IssueRef("org", "repo-a", 5), title="t")
    epics._add_child(HEAD, task)
    epics._reparent_child(OTHER_HEAD, task)
    assert graph.children_of(HEAD) == []
    assert graph.parent_of(task) == OTHER_HEAD
    epics._remove_child(OTHER_HEAD, task)
    assert graph.mutations == [
        ("add", HEAD, task),
        ("reparent", OTHER_HEAD, task),
        ("remove", OTHER_HEAD, task),
    ]
    with pytest.raises(github.GitHubAPIError):
        epics._remove_child(OTHER_HEAD, task)
    epics._add_child(HEAD, task)
    assert [c.ref for c in epics.child_states(HEAD)] == [task]
    assert [c.ref for c in epics.child_states_with_blockers(HEAD)] == [task]
    assert epics.native_parent_of(task) == HEAD


# -- parse_continues -------------------------------------------------------------


def test_parse_continues_reads_the_line_alone_on_its_line() -> None:
    assert epic_chain.parse_continues(_continuation_body(HEAD, 2)) == HEAD
    assert epic_chain.parse_continues("Continues: org/.github#10 and more") is None
    assert epic_chain.parse_continues("no line at all") is None
    assert (
        epic_chain.parse_continues("Continues: org/.github#10\nContinues: org/.github#10") == HEAD
    )
    two = "Continues: org/.github#10\nContinues: org/.github#11\n"
    assert epic_chain.parse_continues(two) is None


# -- walking a chain ---------------------------------------------------------------


def test_chain_of_single_head(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 3)
    assert epic_chain.chain_of(HEAD) == [HEAD]
    assert epic_chain.containers(HEAD) == [
        Container(ref=HEAD, state="OPEN", item_count=3, continuation=None)
    ]


def test_chain_of_follows_continuations(graph: FakeSubIssueGraph) -> None:
    head_items = _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD, k=2)
    c2_items = _items(graph, C2, 99)
    _continuation(graph, C3, C2, k=3)
    c3_items = _items(graph, C3, 5)

    assert epic_chain.chain_of(HEAD) == [HEAD, C2, C3]
    states = epic_chain.chain_child_states(HEAD)
    assert len(states) == 203
    assert [s.ref for s in states] == head_items + c2_items + c3_items
    assert not {C2, C3} & {s.ref for s in states}
    assert [c.item_count for c in epic_chain.containers(HEAD)] == [99, 99, 5]


def test_head_of_continuation_walks_parents(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD, k=2)
    _items(graph, C2, 99)
    _continuation(graph, C3, C2, k=3)
    assert epic_chain.head_of(C3) == HEAD
    assert epic_chain.head_of(C2) == HEAD
    assert epic_chain.head_of(HEAD) == HEAD


def test_head_of_an_item_is_itself(graph: FakeSubIssueGraph) -> None:
    (task,) = _items(graph, HEAD, 1)
    assert epic_chain.head_of(task) == task


def test_head_of_orphaned_continuation_reports_drift(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _continuation(graph, C2, None)
    assert epic_chain.head_of(C2) == HEAD
    assert capsys.readouterr().err.strip() == (
        f"chain drift: {C2.slug} is an orphaned continuation of {HEAD.slug}"
    )
    assert graph.mutations == []


def test_continuation_with_wrong_continues_is_drift(graph: FakeSubIssueGraph) -> None:
    _continuation(graph, C2, HEAD, names=OTHER_HEAD)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.chain_of(HEAD)
    assert exc.value.rule == "continues-mismatch"
    assert exc.value.container == C2
    assert C2.slug in str(exc.value) and "continues-mismatch" in str(exc.value)


def test_continuation_without_continues_line_is_drift(graph: FakeSubIssueGraph) -> None:
    graph.add_issue(C2, title="Epic: big (continuation 2)", labels=["epic", "continuation"])
    graph.link(HEAD, C2)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.chain_of(HEAD)
    assert exc.value.rule == "continues-mismatch"


def test_head_of_parent_naming_another_head_is_drift(graph: FakeSubIssueGraph) -> None:
    _continuation(graph, C2, HEAD, names=OTHER_HEAD)
    _continuation(graph, C3, C2, k=3)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.head_of(C3)
    assert (exc.value.rule, exc.value.container) == ("continues-mismatch", C2)


def test_continuation_untrusted_body_is_drift(graph: FakeSubIssueGraph) -> None:
    _continuation(graph, C2, HEAD, author=trust_stubs.OUTSIDER)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.chain_of(HEAD)
    assert (exc.value.rule, exc.value.container) == ("untrusted-body", C2)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.head_of(C2)
    assert exc.value.rule == "untrusted-body"


@pytest.mark.parametrize("labels", [("epic",), ("continuation",)])
def test_continuation_missing_label_is_drift(
    graph: FakeSubIssueGraph, labels: tuple[str, ...]
) -> None:
    _continuation(graph, C2, HEAD, labels=labels)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.chain_of(HEAD)
    assert (exc.value.rule, exc.value.container) == ("label", C2)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.head_of(C2)
    assert exc.value.rule == "label"


def test_epic_child_without_continues_line_is_an_item(graph: FakeSubIssueGraph) -> None:
    child_epic = graph.add_issue(C2, title="Epic: nested", labels=["epic"], body="Plain body.")
    graph.link(HEAD, child_epic)
    untrusted = graph.add_issue(
        C3,
        title="Epic: x",
        labels=["epic"],
        body="Continues: org/.github#10",
        author=trust_stubs.OUTSIDER,
    )
    graph.link(HEAD, untrusted)
    assert epic_chain.chain_of(HEAD) == [HEAD]
    assert [s.ref for s in epic_chain.chain_child_states(HEAD)] == [C2, C3]
    assert epic_chain.head_of(child_epic) == child_epic


def test_continuation_not_native_child_is_drift(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # head_of: C2's Continues: names HEAD, but its native parents reach OTHER_HEAD.
    _continuation(graph, C2, OTHER_HEAD)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.head_of(C2)
    assert (exc.value.rule, exc.value.container) == ("not-native-child", C2)
    assert OTHER_HEAD.slug in str(exc.value)

    # chain_of: HEAD's listing shows C3, but C3's native parent says otherwise
    # (a listing GitHub has not settled yet).
    _continuation(graph, C3, HEAD)
    real_parent = epics._container_parent
    monkeypatch.setattr(
        epics, "_container_parent", lambda ref: None if ref == C3 else real_parent(ref)
    )
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.chain_of(HEAD)
    assert (exc.value.rule, exc.value.container) == ("not-native-child", C3)
    assert "no parent" in str(exc.value)


def test_two_continuations_under_one_container_is_drift(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 98)
    _continuation(graph, C2, HEAD)
    _continuation(graph, C2B, HEAD)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.chain_child_states(HEAD)
    assert (exc.value.rule, exc.value.container) == ("two-continuations", HEAD)
    assert C2.slug in str(exc.value) and C2B.slug in str(exc.value)


def test_closed_continuation_is_walked(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD, state="CLOSED")
    c2_items = _items(graph, C2, 4)
    assert epic_chain.chain_of(HEAD) == [HEAD, C2]
    assert [c.state for c in epic_chain.containers(HEAD)] == ["OPEN", "CLOSED"]
    assert [s.ref for s in epic_chain.chain_child_states(HEAD)][-4:] == c2_items
    assert epic_chain.head_of(C2) == HEAD


def test_item_count_excludes_continuation(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    head, c2 = epic_chain.containers(HEAD)
    assert head == Container(ref=HEAD, state="OPEN", item_count=99, continuation=C2)
    assert c2 == Container(ref=C2, state="OPEN", item_count=0, continuation=None)


def test_item_count_is_read_from_the_total(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The count is GitHub's sub-issue total, not the length of a listing.
    _items(graph, HEAD, 3)
    real = graph.graphql

    def counted(query: str, **variables: object) -> dict[str, object]:
        answer = real(query, **variables)
        if query == epics._CONTAINER_QUERY:
            answer["node"]["subIssues"]["totalCount"] = 2  # type: ignore[index]
        return answer

    monkeypatch.setattr(github, "graphql", counted)
    assert epic_chain.containers(HEAD)[0].item_count == 2


def test_partial_listing_raises(graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch) -> None:
    _items(graph, HEAD, 3)
    real = graph.graphql

    def short(query: str, **variables: object) -> dict[str, object]:
        answer = real(query, **variables)
        if query == epics._CONTAINER_QUERY:
            answer["node"]["subIssues"]["totalCount"] = 4  # type: ignore[index]
        return answer

    monkeypatch.setattr(github, "graphql", short)
    with pytest.raises(RuntimeError, match="listed 3 of its 4 children"):
        epic_chain.chain_of(HEAD)


def test_truncated_labels_raise(graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch) -> None:
    _items(graph, HEAD, 1)
    real = graph.graphql

    def short(query: str, **variables: object) -> dict[str, object]:
        answer = real(query, **variables)
        if query == epics._CONTAINER_QUERY:
            answer["node"]["subIssues"]["nodes"][0]["labels"]["totalCount"] = 101  # type: ignore[index]
        return answer

    monkeypatch.setattr(github, "graphql", short)
    with pytest.raises(RuntimeError, match="label read .* was truncated"):
        epic_chain.chain_of(HEAD)


def test_missing_container_node_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "read_output", lambda *args: "I_x")
    monkeypatch.setattr(github, "graphql", lambda query, **variables: {"node": None})
    with pytest.raises(RuntimeError, match="returned no issue"):
        epics._container_child_states(HEAD)


def test_container_child_states_is_one_container(graph: FakeSubIssueGraph) -> None:
    head_items = _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    _items(graph, C2, 2)
    assert [s.ref for s in epics._container_child_states(HEAD)] == [*head_items, C2]
    assert epics._container_parent(C2) == HEAD


def test_chain_contains_item_in_continuation(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    (in_c2,) = _items(graph, C2, 1)
    elsewhere = graph.add_issue(IssueRef("org", "repo-a", 7), title="elsewhere")
    assert epic_chain.chain_contains(HEAD, in_c2)
    assert not epic_chain.chain_contains(HEAD, elsewhere)
    assert not epic_chain.chain_contains(HEAD, C2)


# -- the error and the label ------------------------------------------------------


def test_chain_drift_error_is_a_refusal() -> None:
    error = ChainDriftError(C2, "label", "it lacks 'continuation'")
    assert isinstance(error, EpicRefusalError)
    assert isinstance(error, RuntimeError)
    assert str(error) == f"chain drift: {C2.slug} breaks the label rule: it lacks 'continuation'"
    with pytest.raises(ValueError, match="unknown chain drift rule"):
        ChainDriftError(C2, "made-up", "x")


def test_is_continuation_needs_both_labels() -> None:
    assert epics.is_continuation({"epic", "continuation"})
    assert not epics.is_continuation({"continuation"})
    assert not epics.is_continuation({"epic"})


def test_continuation_label_registered() -> None:
    by_name = {label["name"]: label for label in load_labels()["labels"]}
    assert "100-sub-issue limit" in by_name[epics.CONTINUATION_LABEL]["description"]


# -- the write side ------------------------------------------------------------------


def _task(number: int | None = None) -> IssueRef:
    return IssueRef("org", "repo-a", number if number is not None else next(_next_item))


def _new_task(graph: FakeSubIssueGraph) -> IssueRef:
    return graph.add_issue(_task(), title="a new task")


def _created(graph: FakeSubIssueGraph) -> list[IssueRef]:
    return [event[1] for event in graph.events if event[0] == "create"]


def _full_chain(graph: FakeSubIssueGraph, head: IssueRef, count: int) -> list[IssueRef]:
    """*head* and count-1 continuations under it, every container holding 99 items."""
    chain = [head]
    _items(graph, head, epic_chain.ITEM_CAP)
    for k in range(2, count + 1):
        ref = _continuation(
            graph,
            IssueRef(head.owner, head.repo, head.number * 100 + k),
            chain[-1],
            k=k,
            names=head,
        )
        _items(graph, ref, epic_chain.ITEM_CAP)
        chain.append(ref)
    return chain


def _deep_task(graph: FakeSubIssueGraph, height: int) -> IssueRef:
    """An unparented task whose own sub-issues make a tree *height* levels tall."""
    top = _new_task(graph)
    parent = top
    for _ in range(height - 1):
        child = _new_task(graph)
        graph.link(parent, child)
        parent = child
    return top


def _rollover_line(head: IssueRef, continuation: IssueRef) -> str:
    return (
        f"epic {head.slug} rolled over into continuation {continuation.slug} at 99 children; "
        "consider splitting it"
    )


def test_write_side_errors_are_refusals() -> None:
    depth = epic_chain.ChainDepthError(HEAD, items=1, containers=1)
    assert isinstance(depth, EpicRefusalError) and isinstance(depth, RuntimeError)
    assert str(depth) == (
        f"epic {HEAD.slug} cannot grow: 1 item in 1 container, at GitHub's nesting limit of 8; "
        "split the epic"
    )
    assert epic_chain.MAX_DEPTH == 8


def test_link_into_full_head_rolls_over_by_default(graph: FakeSubIssueGraph) -> None:
    # Rollover is on, with no switch to set: a link into a head holding its
    # 99 items creates C2 in the head's 100th slot and links the item into it.
    _items(graph, HEAD, epic_chain.ITEM_CAP)
    task = _new_task(graph)
    graph.clear_log()
    container = epic_chain.link_into(HEAD, task)
    (c2,) = _created(graph)
    assert container == c2
    assert graph.children_of(HEAD)[-1] == c2
    assert len(graph.children_of(HEAD)) == epic_chain.SUBISSUE_LIMIT
    assert graph.children_of(c2) == [task]


@pytest.mark.parametrize(("head_items", "into"), [(99, C2), (98, HEAD)])
def test_link_into_first_fit(graph: FakeSubIssueGraph, head_items: int, into: IssueRef) -> None:
    _items(graph, HEAD, head_items)
    _continuation(graph, C2, HEAD)
    _items(graph, C2, 40)
    task = _new_task(graph)
    graph.clear_log()
    assert epic_chain.link_into(HEAD, task) == into
    assert graph.events == [("add", into, task)]


def test_link_into_reuses_freed_slot_before_growing(graph: FakeSubIssueGraph) -> None:
    head_items = _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    _items(graph, C2, 99)
    epics._remove_child(HEAD, head_items[0])
    task = _new_task(graph)
    graph.clear_log()
    assert epic_chain.link_into(HEAD, task) == HEAD
    assert graph.events == [("add", HEAD, task)]
    assert epic_chain.chain_of(HEAD) == [HEAD, C2]


def test_link_into_rolls_over_at_99(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _items(graph, HEAD, 99)
    task = _new_task(graph)
    graph.clear_log()

    into = epic_chain.link_into(HEAD, task)

    (c2,) = _created(graph)
    assert into == c2
    line = _rollover_line(HEAD, c2)
    assert graph.events == [
        ("label", "org/.github", "epic"),
        ("label", "org/.github", "continuation"),
        ("create", c2),
        ("add", HEAD, c2),
        ("comment", HEAD, line),
        ("add", c2, task),
    ]
    issue = graph.issues[c2]
    assert issue.title == "Epic: big (continuation 2)"
    assert issue.labels == ["epic", "continuation"]
    lines = issue.body.splitlines()
    assert f"Continues: {HEAD.slug}" in lines and "Container: 2" in lines
    assert graph.children_of(HEAD)[-1] == c2 and len(graph.children_of(HEAD)) == 100
    assert epic_chain.chain_of(HEAD) == [HEAD, c2]
    assert capsys.readouterr().out == line + "\n"


def test_link_into_skips_closed_continuation(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD, state="CLOSED")
    _items(graph, C2, 5)
    _continuation(graph, C3, C2, k=3)
    _items(graph, C3, 10)
    task = _new_task(graph)
    graph.clear_log()
    assert epic_chain.link_into(HEAD, task) == C3
    assert graph.events == [("add", C3, task)]
    assert graph.issues[C2].state == "CLOSED"


def test_link_into_closed_continuation_without_successor(graph: FakeSubIssueGraph) -> None:
    # Task 4 recorded "Link under closed parent: accepted", so the next
    # continuation is linked into the closed one.
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD, state="CLOSED")
    _items(graph, C2, 99)
    epic_chain.check_capacity(HEAD)
    task = _new_task(graph)
    graph.clear_log()

    into = epic_chain.link_into(HEAD, task)

    (c3,) = _created(graph)
    assert into == c3
    assert graph.mutations == [("add", C2, c3), ("add", c3, task)]
    assert graph.issues[C2].state == "CLOSED"
    assert graph.issues[c3].title == "Epic: big (continuation 3)"
    assert "Container: 3" in graph.issues[c3].body.splitlines()
    assert epic_chain.chain_of(HEAD) == [HEAD, C2, c3]


def test_closed_continuation_link_uses_unchecked_add(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD, state="CLOSED")
    _items(graph, C2, 99)
    unchecked: list[tuple[IssueRef, IssueRef]] = []
    real = epics._add_child_unchecked

    def spy(parent: IssueRef, child: IssueRef) -> None:
        unchecked.append((parent, child))
        real(parent, child)

    monkeypatch.setattr(epics, "_add_child_unchecked", spy)
    epic_chain.link_into(HEAD, _new_task(graph))
    (c3,) = _created(graph)
    assert unchecked == [(C2, c3)]
    with pytest.raises(epics.ClosedEpicError):
        epics._add_child(C2, _new_task(graph))


def _references(tree: ast.AST, name: str) -> list[str | None]:
    """The innermost function of every reference to *name* in *tree* (None at module level)."""
    found: list[str | None] = []

    def walk(node: ast.AST, function: str | None) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if (isinstance(node, ast.Attribute) and node.attr == name) or (
            isinstance(node, ast.Name) and node.id == name
        ):
            found.append(function)
        for child in ast.iter_child_nodes(node):
            walk(child, function)

    walk(tree, None)
    return found


def test_add_child_unchecked_has_one_caller() -> None:
    src = Path(epics.__file__).resolve().parents[1]
    where: set[tuple[str, str | None]] = set()
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        where |= {(path.name, function) for function in _references(tree, "_add_child_unchecked")}
    assert where == {("epic_chain.py", "find_or_create_continuation")}


def test_link_into_refuses_closed_head(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 99)
    graph.close(HEAD)
    task = _new_task(graph)
    graph.clear_log()
    with pytest.raises(epics.ClosedEpicError, match=f"epic {HEAD.slug} is closed"):
        epic_chain.link_into(HEAD, task)
    with pytest.raises(epics.ClosedEpicError):
        epic_chain.check_capacity(HEAD)
    assert graph.events == []
    assert graph.issues[HEAD].state == "CLOSED"


def test_find_or_create_refuses_closed_head(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 99)
    graph.close(HEAD)
    _continuation(graph, C2, None, k=7)
    body = graph.issues[C2].body

    def no_orphan_read(head: IssueRef) -> list[IssueRef]:
        raise AssertionError("orphans were read for a closed head")

    monkeypatch.setattr(epic_chain, "find_orphans", no_orphan_read)
    graph.clear_log()
    with pytest.raises(epics.ClosedEpicError):
        epic_chain.find_or_create_continuation(HEAD, HEAD)
    assert graph.events == []
    assert graph.parent_of(C2) is None
    assert graph.issues[C2].body == body


def test_find_or_create_reuses_existing_child_continuation(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    graph.clear_log()
    assert epic_chain.find_or_create_continuation(HEAD, HEAD) == C2
    assert graph.events == []


def test_find_or_create_adopts_orphan(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C3, None, k=5)
    orphan_items = _items(graph, C3, 2)
    graph.clear_log()

    assert epic_chain.find_or_create_continuation(HEAD, HEAD) == C3

    assert _created(graph) == []
    line = _rollover_line(HEAD, C3)
    assert graph.events == [("add", HEAD, C3), ("edit", C3), ("comment", HEAD, line)]
    assert graph.issues[C3].title == "Epic: big (continuation 2)"
    lines = graph.issues[C3].body.splitlines()
    assert "Container: 2" in lines and "Container: 5" not in lines
    assert f"Continues: {HEAD.slug}" in lines
    assert epic_chain.chain_of(HEAD) == [HEAD, C3]
    assert [s.ref for s in epic_chain.chain_child_states(HEAD)][-2:] == orphan_items
    assert capsys.readouterr().out == line + "\n"

    # The next link fills the adopted continuation by first fit.
    task = _new_task(graph)
    assert epic_chain.link_into(HEAD, task) == C3


def test_link_into_adopts_orphan_before_creating(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, None)
    task = _new_task(graph)
    graph.clear_log()
    assert epic_chain.link_into(HEAD, task) == C2
    assert _created(graph) == []
    assert graph.mutations == [("add", HEAD, C2), ("add", C2, task)]


def test_orphan_failing_trust_is_not_adopted(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C3, None, author=trust_stubs.OUTSIDER)
    task = _new_task(graph)
    graph.clear_log()
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.link_into(HEAD, task)
    assert (exc.value.rule, exc.value.container) == ("untrusted-body", C3)
    assert graph.events == []
    assert graph.parent_of(C3) is None and graph.parent_of(task) is None


def test_find_orphans_skips_other_chains(graph: FakeSubIssueGraph) -> None:
    # Continuations of another head, linked or not, and an untrusted one
    # linked in another chain, are not this head's orphans.
    _continuation(graph, C2B, OTHER_HEAD, names=OTHER_HEAD)
    _continuation(graph, IssueRef("org", ".github", 26), None, names=OTHER_HEAD)
    _continuation(graph, IssueRef("org", ".github", 27), OTHER_HEAD, author=trust_stubs.OUTSIDER)
    _continuation(graph, C2, HEAD)
    graph.add_issue(IssueRef("org", ".github", 28), title="closed", state="CLOSED")
    assert epic_chain.find_orphans(HEAD) == []
    orphan = _continuation(graph, C3, None)
    assert epic_chain.find_orphans(HEAD) == [orphan]


@pytest.mark.parametrize(
    ("labels", "under", "rule"),
    [
        (("continuation",), None, "label"),
        (("epic", "continuation"), OTHER_HEAD, "not-native-child"),
    ],
)
def test_find_orphans_raises_on_a_broken_continuation_of_this_head(
    graph: FakeSubIssueGraph, labels: tuple[str, ...], under: IssueRef | None, rule: str
) -> None:
    _continuation(graph, C3, under, labels=labels)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.find_orphans(HEAD)
    assert (exc.value.rule, exc.value.container) == (rule, C3)


def test_find_orphans_raises_on_unlinked_continuation_without_continues(
    graph: FakeSubIssueGraph,
) -> None:
    graph.add_issue(C3, title="x", labels=["epic", "continuation"], body="no line")
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.find_orphans(HEAD)
    assert (exc.value.rule, exc.value.container) == ("continues-mismatch", C3)


def test_depth_refusal_before_any_write(graph: FakeSubIssueGraph) -> None:
    _full_chain(graph, HEAD, 7)
    task = _new_task(graph)
    graph.clear_log()
    message = (
        f"epic {HEAD.slug} cannot grow: 693 items in 7 containers, at GitHub's nesting limit "
        "of 8; split the epic"
    )
    with pytest.raises(epic_chain.ChainDepthError) as exc:
        epic_chain.link_into(HEAD, task)
    assert str(exc.value) == message
    with pytest.raises(epic_chain.ChainDepthError) as exc:
        epic_chain.check_capacity(HEAD)
    assert str(exc.value) == message
    assert graph.events == []


def test_rollover_into_the_last_level_fits(graph: FakeSubIssueGraph) -> None:
    # Six full containers: a seventh puts its items at level 8, the limit.
    chain = _full_chain(graph, HEAD, 6)
    task = _new_task(graph)
    into = epic_chain.link_into(HEAD, task)
    assert epic_chain.chain_of(HEAD) == [*chain, into]


def test_github_depth_error_is_reworded(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GitHub refuses at a shallower depth than the constant says, so its own
    # error comes first, on the continuation's link.
    _full_chain(graph, HEAD, 6)
    monkeypatch.setattr(subissue_fake, "MAX_DEPTH", 6)
    task = _new_task(graph)
    with pytest.raises(epic_chain.ChainDepthError) as exc:
        epic_chain.link_into(HEAD, task)
    assert str(exc.value) == (
        f"epic {HEAD.slug} cannot grow: 594 items in 6 containers, at GitHub's nesting limit "
        "of 8; split the epic"
    )
    assert isinstance(exc.value.__cause__, github.GitHubAPIError)
    assert graph.parent_of(task) is None


def test_move_into_depth_error_names_item(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 3)
    task = _deep_task(graph, 8)
    graph.clear_log()
    with pytest.raises(epic_chain.ChainDepthError) as exc:
        epic_chain.move_into(HEAD, task)
    assert str(exc.value) == (
        f"epic {HEAD.slug} cannot grow: 3 items in 1 container, at GitHub's nesting limit of 8; "
        f"split the epic, or {task.slug}'s own sub-issues are too deep to fit under it"
    )
    assert graph.events == []
    assert graph.parent_of(task) is None


def test_depth_counts_head_nested_under_chained_parent(graph: FakeSubIssueGraph) -> None:
    # HEAD is an item in OTHER_HEAD's second container, so it sits at level 3:
    # five full containers already reach level 8 with their items, though a
    # top-level head could roll over twice more.
    _items(graph, OTHER_HEAD, 99)
    _continuation(graph, C2B, OTHER_HEAD, names=OTHER_HEAD)
    graph.link(C2B, HEAD)
    _full_chain(graph, HEAD, 5)
    task = _new_task(graph)
    graph.clear_log()
    with pytest.raises(epic_chain.ChainDepthError) as exc:
        epic_chain.link_into(HEAD, task)
    assert str(exc.value) == (
        f"epic {HEAD.slug} cannot grow: 495 items in 5 containers, at GitHub's nesting limit "
        "of 8; split the epic"
    )
    with pytest.raises(epic_chain.ChainDepthError):
        epic_chain.check_capacity(HEAD)
    assert graph.events == []


def _head_of_kind(graph: FakeSubIssueGraph, labels: list[str], title: str) -> IssueRef:
    head = graph.add_issue(IssueRef("org", ".github", 12), title=title, labels=labels)
    _items(graph, head, 99)
    graph.clear_log()
    return head


@pytest.mark.parametrize(
    "labels", [["epic", "epic-development"], ["epic", "proxy"]], ids=["development", "proxy"]
)
def test_development_rollover_is_loud(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str], labels: list[str]
) -> None:
    head = _head_of_kind(graph, labels, "Epic: something")
    epic_chain.link_into(head, _new_task(graph))
    (c2,) = _created(graph)
    line = _rollover_line(head, c2)
    assert capsys.readouterr().out == line + "\n"
    assert graph.comments == {head: [line]}


def test_queue_rollover_prints_review_nudge(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    head = _head_of_kind(graph, ["epic", "intake", "epic-management"], "Epic (intake): org")
    epic_chain.link_into(head, _new_task(graph))
    (c2,) = _created(graph)
    assert capsys.readouterr().out == (
        f"queue {head.slug} rolled over into continuation {c2.slug}; run epic-review intake\n"
    )
    assert graph.comments == {}
    assert graph.issues[c2].title == "Epic (intake): org (continuation 2)"
    assert graph.issues[c2].labels == ["epic", "continuation"]


@pytest.mark.parametrize(
    ("labels", "title"),
    [
        (["epic", "ad-hoc"], "Epic (ad hoc): repo-a"),
        (["epic", "hoshin", "epic-management"], "Epic (hoshin): org"),
        (["epic", "operations", "epic-management"], "Epic (operations): org"),
    ],
    ids=["ad-hoc", "hoshin", "operations"],
)
def test_other_standing_rollover_is_plain(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str], labels: list[str], title: str
) -> None:
    head = _head_of_kind(graph, labels, title)
    epic_chain.link_into(head, _new_task(graph))
    (c2,) = _created(graph)
    assert capsys.readouterr().out == (
        f"epic {head.slug} rolled over into continuation {c2.slug}\n"
    )
    assert graph.comments == {}


def test_move_into_is_one_reparent(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 3)
    (task,) = _items(graph, OTHER_HEAD, 1)
    graph.clear_log()
    assert epic_chain.move_into(HEAD, task) == HEAD
    assert graph.events == [("reparent", HEAD, task)]
    assert graph.parent_of(task) == HEAD


def test_move_into_rolls_over_with_one_reparent(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    (task,) = _items(graph, OTHER_HEAD, 1)
    graph.clear_log()
    into = epic_chain.move_into(HEAD, task)
    (c2,) = _created(graph)
    assert into == c2
    assert graph.mutations == [("add", HEAD, c2), ("reparent", c2, task)]


def test_move_into_leaves_an_item_already_in_the_chain(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    (task,) = _items(graph, C2, 1)
    graph.clear_log()
    assert epic_chain.move_into(HEAD, task) == C2
    assert graph.events == []


def test_move_into_refuses_closed_head(graph: FakeSubIssueGraph) -> None:
    (task,) = _items(graph, OTHER_HEAD, 1)
    graph.close(HEAD)
    graph.clear_log()
    with pytest.raises(epics.ClosedEpicError, match=f"epic {HEAD.slug} is closed"):
        epic_chain.move_into(HEAD, task)
    assert graph.events == []
    assert graph.parent_of(task) == OTHER_HEAD


def test_unlink_from_finds_container(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    (task,) = _items(graph, C2, 1)
    graph.clear_log()
    epic_chain.unlink_from(HEAD, task)
    assert graph.events == [("remove", C2, task)]
    with pytest.raises(ValueError, match=f"{task.slug} is not linked in the chain of epic"):
        epic_chain.unlink_from(HEAD, task)
    with pytest.raises(ValueError, match=HEAD.slug):
        epic_chain.unlink_from(HEAD, C2)


# -- settling after a transfer (spec §4.2) ------------------------------------------


def _chain_with_task_in_c2(graph: FakeSubIssueGraph) -> IssueRef:
    """HEAD full, then C2 holding one task, which is returned."""
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    (task,) = _items(graph, C2, 1)
    return task


def test_move_into_task_already_in_chain_is_noop(graph: FakeSubIssueGraph) -> None:
    # Its parent field names a container of the chain whose listing holds it:
    # nothing changes, and that container is returned.
    (task,) = _items(graph, HEAD, 1)
    graph.clear_log()
    assert epic_chain.move_into(HEAD, task) == HEAD
    assert graph.events == []
    assert graph.parent_of(task) == HEAD
    assert graph.children_of(HEAD) == [task]


def test_move_into_task_its_container_does_not_list_is_moved(graph: FakeSubIssueGraph) -> None:
    # The parent field alone is not proof of the link (#4291): a task the
    # named container does not list is moved like any other, never left out.
    _items(graph, HEAD, 3)
    (task,) = _items(graph, HEAD, 1)
    graph.unlist(HEAD, task)
    graph.clear_log()
    assert epic_chain.move_into(HEAD, task) == HEAD
    assert graph.events == [("reparent", HEAD, task)]
    assert task in graph.children_of(HEAD)


def test_observe_parent_reads_field_and_listings(graph: FakeSubIssueGraph) -> None:
    task = _chain_with_task_in_c2(graph)
    (moved,) = _items(graph, OTHER_HEAD, 1)
    graph.clear_log()
    assert epic_chain.observe_parent(HEAD, task) == (C2, [C2])
    # Its field names C2 while C2 lists it no longer.
    graph.unlist(C2, task)
    assert epic_chain.observe_parent(HEAD, task) == (C2, [])
    # A listing left in the chain after it moved to another epic (#4004).
    graph.list_stale(C2, moved)
    assert epic_chain.observe_parent(HEAD, moved) == (OTHER_HEAD, [C2])
    assert graph.events == []


def test_clear_stale_listing_clears_the_container_that_holds_it(
    graph: FakeSubIssueGraph,
) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C2, HEAD)
    (moved,) = _items(graph, OTHER_HEAD, 1)
    graph.list_stale(C2, moved)
    graph.clear_log()
    epic_chain.clear_stale_listing(HEAD, moved, keep=OTHER_HEAD)
    assert graph.events == [("remove", C2, moved)]
    assert moved not in graph.children_of(C2)
    assert graph.parent_of(moved) == OTHER_HEAD
    assert graph.children_of(OTHER_HEAD) == [moved]
    # Nothing is left to clear: refused by name, nothing written.
    graph.clear_log()
    with pytest.raises(ValueError, match=f"no container of epic {HEAD.slug} but"):
        epic_chain.clear_stale_listing(HEAD, moved, keep=OTHER_HEAD)
    assert graph.events == []


def test_clear_stale_listing_never_clears_keep(graph: FakeSubIssueGraph) -> None:
    (task,) = _items(graph, HEAD, 1)
    graph.clear_log()
    with pytest.raises(ValueError, match=task.slug):
        epic_chain.clear_stale_listing(HEAD, task, keep=HEAD)
    assert graph.events == []
    assert graph.children_of(HEAD) == [task]


def test_relink_forces_listing_in_continuation(graph: FakeSubIssueGraph) -> None:
    # The #4291 case in a continuation: the task's parent field names C2, but
    # C2's listing lacks it. One replaceParent link into C2 lists it again.
    task = _chain_with_task_in_c2(graph)
    graph.unlist(C2, task)
    graph.clear_log()
    assert epic_chain.relink(HEAD, task) == C2
    assert graph.mutations == [("reparent", C2, task)]
    assert graph.children_of(C2) == [task]
    assert graph.parent_of(task) == C2


def test_relink_outside_the_chain_is_refused(graph: FakeSubIssueGraph) -> None:
    (elsewhere,) = _items(graph, OTHER_HEAD, 1)
    unparented = _new_task(graph)
    graph.clear_log()
    with pytest.raises(
        ValueError, match=f"names {OTHER_HEAD.slug}, not a container of epic {HEAD.slug}"
    ):
        epic_chain.relink(HEAD, elsewhere)
    with pytest.raises(ValueError, match="is empty"):
        epic_chain.relink(HEAD, unparented)
    assert graph.events == []


def test_relink_refuses_closed_head(graph: FakeSubIssueGraph) -> None:
    (task,) = _items(graph, HEAD, 1)
    graph.unlist(HEAD, task)
    graph.close(HEAD)
    graph.clear_log()
    with pytest.raises(epics.ClosedEpicError, match=f"epic {HEAD.slug} is closed"):
        epic_chain.relink(HEAD, task)
    assert graph.events == []
    assert graph.issues[HEAD].state == "CLOSED"


def test_fake_models_a_transfer_s_disagreeing_views(graph: FakeSubIssueGraph) -> None:
    # A link into a container listing the child under another parent field
    # is refused as a duplicate (#3839); removing the field's link hands it
    # to the container still listing it (#4004).
    (task,) = _items(graph, OTHER_HEAD, 1)
    graph.list_stale(HEAD, task)
    with pytest.raises(github.GitHubAPIError, match="Issue may not contain duplicate sub-issues"):
        epics._reparent_child(HEAD, task)
    epics._remove_child(OTHER_HEAD, task)
    assert graph.parent_of(task) == HEAD
    assert graph.children_of(HEAD) == [task]
    assert graph.children_of(OTHER_HEAD) == []


# -- the write side's other failures -------------------------------------------------


def test_link_error_other_than_depth_surfaces(graph: FakeSubIssueGraph) -> None:
    (task,) = _items(graph, OTHER_HEAD, 1)
    with pytest.raises(github.GitHubAPIError, match="may only have one parent"):
        epic_chain.link_into(HEAD, task)


def test_level_refuses_a_parent_cycle(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(epics, "_container_parent", lambda ref: OTHER_HEAD)
    with pytest.raises(RuntimeError, match="deeper than GitHub's nesting limit of 8"):
        epic_chain.check_capacity(HEAD)


def test_rollover_that_never_settles_raises(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 99)
    monkeypatch.setattr(epic_chain, "find_or_create_continuation", lambda head, tail: tail)
    with pytest.raises(RuntimeError, match="had a free slot .* after 9 rollovers"):
        epic_chain.link_into(HEAD, _new_task(graph))


def test_find_orphans_refuses_an_unreadable_listing(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "read_json", lambda *args: {})
    with pytest.raises(RuntimeError, match="returned no list"):
        epic_chain.find_orphans(HEAD)


def test_find_or_create_refuses_a_tail_outside_the_chain(graph: FakeSubIssueGraph) -> None:
    with pytest.raises(ValueError, match=f"{OTHER_HEAD.slug} is not a container"):
        epic_chain.find_or_create_continuation(HEAD, OTHER_HEAD)


def test_adopted_orphan_without_container_line_gains_one(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    graph.add_issue(
        C3, title="old", labels=["epic", "continuation"], body=f"Continues: {HEAD.slug}"
    )
    assert epic_chain.find_or_create_continuation(HEAD, HEAD) == C3
    assert graph.issues[C3].body.splitlines() == [f"Continues: {HEAD.slug}", "Container: 2"]
    assert graph.issues[C3].title == "Epic: big (continuation 2)"


def test_adopted_orphan_already_numbered_is_not_edited(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C3, None, k=2)
    graph.clear_log()
    assert epic_chain.find_or_create_continuation(HEAD, HEAD) == C3
    assert [event[0] for event in graph.events] == ["add", "comment"]


def test_adopted_orphan_whose_body_turns_untrusted_is_not_renumbered(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 99)
    _continuation(graph, C3, None, k=5)
    real = epic_chain.find_orphans

    def then_untrusted(head: IssueRef) -> list[IssueRef]:
        found = real(head)
        monkeypatch.setattr(epics, "_trusted_body", lambda ref: None)
        return found

    monkeypatch.setattr(epic_chain, "find_orphans", then_untrusted)
    with pytest.raises(ChainDriftError) as exc:
        epic_chain.find_or_create_continuation(HEAD, HEAD)
    assert (exc.value.rule, exc.value.container) == ("untrusted-body", C3)
    assert graph.issues[C3].title == "Epic: big (continuation 5)"


# -- race heals (spec §4.4) -----------------------------------------------------------

C2A = IssueRef("org", ".github", 201)
C2B = IssueRef("org", ".github", 205)
RIVAL = IssueRef("org", ".github", 500)


def _heal_line(tail: IssueRef, moved: IssueRef, continuation: IssueRef) -> str:
    return (
        f"healed epic {HEAD.slug}: its tail {tail.slug} held 100 items and no continuation; "
        f"moved {moved.slug} into continuation {continuation.slug} and linked it into the "
        "freed slot"
    )


def _race(
    graph: FakeSubIssueGraph,
    monkeypatch: pytest.MonkeyPatch,
    task: IssueRef,
    *,
    fill_to: int = 100,
    times: int = 1,
    error: str | None = None,
) -> list[IssueRef]:
    """Racing writers fill the parent of *task*'s next *times* links up to *fill_to* children.

    With *error*, the link then fails with that text instead of reaching
    GitHub. Returns the racers' items, in the order they were linked.
    """
    real = epics._add_child
    racers: list[IssueRef] = []
    left = [times]

    def racing(parent: IssueRef, child: IssueRef) -> None:
        if child == task and left[0] > 0:
            left[0] -= 1
            while len(graph.children_of(parent)) < fill_to:
                racer = graph.add_issue(_task(), title="a racing writer's task")
                graph.link(parent, racer)
                racers.append(racer)
            if error is not None:
                raise subissue_fake._graphql_error(error)
        real(parent, child)

    monkeypatch.setattr(epics, "_add_child", racing)
    return racers


def _every_item_parented(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch, items: list[IssueRef]
) -> None:
    """Assert after every mutation of *graph* that each of *items* has a parent."""
    real = graph._record

    def checked(event: tuple[object, ...]) -> None:
        real(event)
        orphaned = [item.slug for item in items if graph.parent_of(item) is None]
        assert not orphaned, f"{orphaned} left without a parent by {event}"

    monkeypatch.setattr(graph, "_record", checked)


def test_chain_limit_error_is_a_refusal() -> None:
    task = _task(7)
    error = epic_chain.ChainLimitError(HEAD, C2, task)
    assert isinstance(error, EpicRefusalError) and isinstance(error, RuntimeError)
    assert str(error) == (
        f"could not link {task.slug} into {C2.slug} of epic {HEAD.slug}: GitHub refused it at "
        "its limit of 100 sub-issues in 3 tries"
    )
    assert epic_chain.LINK_RETRIES == 3
    noted = epic_chain.ChainLimitError(HEAD, C2, None, note="see here")
    assert str(noted).startswith("could not link a continuation into")
    assert str(noted).endswith("; see here")


def test_link_into_heals_head_at_100_without_continuation(
    graph: FakeSubIssueGraph,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    items = _items(graph, HEAD, 100)
    task = _new_task(graph)
    graph.clear_log()
    _every_item_parented(graph, monkeypatch, items)

    into = epic_chain.link_into(HEAD, task)

    (c2,) = _created(graph)
    newest = items[-1]
    assert into == c2
    assert graph.events == [
        ("label", "org/.github", "epic"),
        ("label", "org/.github", "continuation"),
        ("create", c2),
        ("reparent", c2, newest),
        ("add", HEAD, c2),
        ("comment", HEAD, _rollover_line(HEAD, c2)),
        ("add", c2, task),
    ]
    assert graph.children_of(HEAD) == [*items[:-1], c2]
    assert graph.children_of(c2) == [newest, task]
    assert epic_chain.chain_of(HEAD) == [HEAD, c2]
    out = capsys.readouterr().out.splitlines()
    assert out == [_heal_line(HEAD, newest, c2), _rollover_line(HEAD, c2)]


def test_full_tail_heal_moves_own_item(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = _items(graph, HEAD, 98)
    task = _new_task(graph)
    racers = _race(graph, monkeypatch, task, fill_to=99)
    graph.clear_log()

    into = epic_chain.link_into(HEAD, task)

    (c2,) = _created(graph)
    (racer,) = racers
    assert into == c2
    assert graph.events[1:] == [
        ("add", HEAD, task),
        ("label", "org/.github", "epic"),
        ("label", "org/.github", "continuation"),
        ("create", c2),
        ("reparent", c2, task),
        ("add", HEAD, c2),
        ("comment", HEAD, _rollover_line(HEAD, c2)),
    ]
    assert graph.children_of(HEAD) == [*items, racer, c2]
    assert graph.children_of(c2) == [task]


def test_full_tail_heal_without_own_item_in_tail_moves_newest(graph: FakeSubIssueGraph) -> None:
    items = _items(graph, HEAD, 100)
    elsewhere = _items(graph, OTHER_HEAD, 1)[0]
    assert epic_chain.heal_full_tail(HEAD, own_item=elsewhere) is True
    (c2,) = _created(graph)
    assert graph.children_of(c2) == [items[-1]]
    assert graph.parent_of(elsewhere) == OTHER_HEAD


def test_limit_error_retries_then_succeeds(
    graph: FakeSubIssueGraph,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _items(graph, HEAD, 98)
    task = _new_task(graph)
    racers = _race(graph, monkeypatch, task)
    assert len(racers) == 0  # the racers link once our writer tries

    into = epic_chain.link_into(HEAD, task)

    (c2,) = _created(graph)
    assert into == c2
    assert len(racers) == 2
    assert graph.children_of(c2) == [racers[-1], task]
    assert graph.children_of(HEAD)[-1] == c2 and len(graph.children_of(HEAD)) == 100
    out = capsys.readouterr().out
    assert (
        f"{HEAD.slug} of epic {HEAD.slug} was full when {task.slug} was linked (a racing writer); "
        "re-reading the chain to try again (1 of 3)"
    ) in out
    assert _heal_line(HEAD, racers[-1], c2) in out


def test_limit_error_retries_exhausted_names_limit(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 98)
    task = _new_task(graph)
    _race(graph, monkeypatch, task, times=epic_chain.LINK_RETRIES)

    with pytest.raises(epic_chain.ChainLimitError) as exc:
        epic_chain.link_into(HEAD, task)

    c2, c3 = _created(graph)
    assert epic_chain.chain_of(HEAD) == [HEAD, c2, c3]
    assert exc.value.container == c3 and exc.value.item == task
    assert str(exc.value) == (
        f"could not link {task.slug} into {c3.slug} of epic {HEAD.slug}: GitHub refused it at "
        "its limit of 100 sub-issues in 3 tries"
    )
    assert isinstance(exc.value.__cause__, github.GitHubAPIError)
    assert graph.parent_of(task) is None


def test_unrecognised_link_error_at_100_is_full(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 98)
    task = _new_task(graph)
    racers = _race(graph, monkeypatch, task, error="Something went wrong on our side")

    into = epic_chain.link_into(HEAD, task)

    (c2,) = _created(graph)
    assert into == c2
    assert graph.children_of(c2) == [racers[-1], task]


def test_link_error_below_100_surfaces(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 5)
    task = _new_task(graph)
    _race(graph, monkeypatch, task, fill_to=0, error="Something went wrong on our side")
    graph.clear_log()

    with pytest.raises(github.GitHubAPIError, match="Something went wrong on our side"):
        epic_chain.link_into(HEAD, task)

    assert graph.events == []
    assert graph.parent_of(task) is None


def test_full_tail_heal_lost_to_a_racing_continuation(
    graph: FakeSubIssueGraph,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Our heal moved the newest item out; a racing healer's continuation took
    # the freed slot first. The moved item goes into the racer's
    # continuation, and ours is left unlinked, an orphan for adoption.
    items = _items(graph, HEAD, 100)
    _continuation(graph, RIVAL, None, k=2)
    real = epics._add_child
    raced = [False]

    def racing(parent: IssueRef, child: IssueRef) -> None:
        if parent == HEAD and child != RIVAL and not raced[0]:
            raced[0] = True
            real(HEAD, RIVAL)
        real(parent, child)

    monkeypatch.setattr(epics, "_add_child", racing)
    monkeypatch.setattr(epic_chain, "find_orphans", lambda head: [])
    _every_item_parented(graph, monkeypatch, items)

    assert epic_chain.heal_full_tail(HEAD) is True

    (ours,) = _created(graph)
    assert graph.parent_of(ours) is None and graph.children_of(ours) == []
    assert graph.children_of(RIVAL) == [items[-1]]
    assert epic_chain.chain_of(HEAD) == [HEAD, RIVAL]
    assert (
        f"epic {HEAD.slug}: a racing writer took the slot freed in {HEAD.slug}; moved "
        f"{items[-1].slug} into {RIVAL.slug} and left continuation {ours.slug} unlinked, for "
        "the next rollover to adopt"
    ) in capsys.readouterr().out


def test_full_tail_heal_lost_with_no_room_says_where_the_item_waits(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = _items(graph, HEAD, 100)
    real = epics._add_child

    def racing(parent: IssueRef, child: IssueRef) -> None:
        if parent == HEAD and len(graph.children_of(HEAD)) < 100:
            graph.link(HEAD, _new_task(graph))  # a racer's item takes the freed slot
        real(parent, child)

    monkeypatch.setattr(epics, "_add_child", racing)

    with pytest.raises(epic_chain.ChainLimitError) as exc:
        epic_chain.heal_full_tail(HEAD)

    (ours,) = _created(graph)
    assert graph.children_of(ours) == [items[-1]]
    assert str(exc.value).endswith(
        f"; {items[-1].slug} waits in the unlinked continuation {ours.slug}, which the next "
        "rollover adopts"
    )


def test_full_tail_heal_link_failure_moves_the_item_back(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = _items(graph, HEAD, 100)
    real = epics._add_child

    def failing(parent: IssueRef, child: IssueRef) -> None:
        if parent == HEAD:
            raise subissue_fake._graphql_error("Something went wrong on our side")
        real(parent, child)

    monkeypatch.setattr(epics, "_add_child", failing)
    _every_item_parented(graph, monkeypatch, items)

    with pytest.raises(RuntimeError, match="Something went wrong on our side") as exc:
        epic_chain.heal_full_tail(HEAD)

    assert not isinstance(exc.value, github.GitHubAPIError)
    (ours,) = _created(graph)
    assert graph.children_of(HEAD) == items
    assert graph.children_of(ours) == []


def test_full_tail_heal_reparent_failure_is_not_a_race(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    items = _items(graph, HEAD, 100)
    task = _new_task(graph)

    def failing(new_parent: IssueRef, child: IssueRef) -> None:
        raise subissue_fake._graphql_error("Something went wrong on our side")

    monkeypatch.setattr(epics, "_reparent_child", failing)

    with pytest.raises(RuntimeError, match="Something went wrong on our side") as exc:
        epic_chain.link_into(HEAD, task)

    assert not isinstance(exc.value, (github.GitHubAPIError, epic_chain.ChainLimitError))
    (ours,) = _created(graph)
    assert str(exc.value).startswith(
        f"could not move {items[-1].slug} from the full tail {HEAD.slug} of epic {HEAD.slug} "
        f"into continuation {ours.slug}"
    )
    assert graph.children_of(HEAD) == items
    assert "racing writer" not in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="Something went wrong on our side"):
        epic_chain.heal_full_tail(HEAD)


def test_full_tail_heal_depth_error_moves_the_item_back(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = _items(graph, HEAD, 100)
    real = epics._add_child

    def too_deep(parent: IssueRef, child: IssueRef) -> None:
        if parent == HEAD:
            raise subissue_fake._graphql_error(DEPTH_ERROR.format(child=child.number, parent=10))
        real(parent, child)

    monkeypatch.setattr(epics, "_add_child", too_deep)

    with pytest.raises(epic_chain.ChainDepthError):
        epic_chain.heal_full_tail(HEAD)

    assert graph.children_of(HEAD) == items


def test_full_tail_heal_retries_exhausted(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 100)

    def always_full(
        head: IssueRef, tail: IssueRef, *, own_item: IssueRef | None = None
    ) -> IssueRef:
        raise subissue_fake._graphql_error(LIMIT_ERROR.format(child=1, parent=tail.number))

    monkeypatch.setattr(epic_chain, "find_or_create_continuation", always_full)
    with pytest.raises(epic_chain.ChainLimitError) as exc:
        epic_chain.heal_full_tail(HEAD)
    assert exc.value.container == HEAD and exc.value.item is None


def test_full_tail_heal_surfaces_a_failure_once_the_tail_has_room(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    items = _items(graph, HEAD, 100)

    def unlink_then_fail(
        head: IssueRef, tail: IssueRef, *, own_item: IssueRef | None = None
    ) -> IssueRef:
        graph._remove(tail, items[0])  # someone else freed a slot meanwhile
        raise subissue_fake._graphql_error("Something went wrong on our side")

    monkeypatch.setattr(epic_chain, "find_or_create_continuation", unlink_then_fail)
    with pytest.raises(github.GitHubAPIError, match="Something went wrong on our side"):
        epic_chain.heal_full_tail(HEAD)


def test_settle_refuses_an_item_left_without_a_parent(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _new_task(graph)
    real = epics._container_parent
    monkeypatch.setattr(epic_chain, "heal_full_tail", lambda head, *, own_item=None: True)
    monkeypatch.setattr(epics, "_container_parent", lambda ref: None if ref == task else real(ref))
    with pytest.raises(RuntimeError, match=f"{task.slug} has no parent after the chain"):
        epic_chain.link_into(HEAD, task)


def test_full_tail_heal_needs_nothing_at_room_or_at_the_depth_limit(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 99)
    graph.clear_log()
    assert epic_chain.heal_full_tail(HEAD) is False
    _items(graph, HEAD, 1)
    monkeypatch.setattr(epic_chain, "MAX_DEPTH", 2)
    assert epic_chain.heal_full_tail(HEAD) is False
    assert graph.events[-1][0] == "add" and _created(graph) == []


def test_duplicate_continuations_keep_lower(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _items(graph, HEAD, 98)
    _continuation(graph, C2A, HEAD)
    (kept_item,) = _items(graph, C2A, 1)
    _continuation(graph, C2B, HEAD)
    (moved_item,) = _items(graph, C2B, 1)
    graph.clear_log()

    assert epic_chain.heal_duplicate_continuations(HEAD) is True

    assert graph.events == [
        ("reparent", C2A, moved_item),
        ("remove", HEAD, C2B),
        ("close", C2B, "not planned"),
    ]
    (comment,) = graph.comments[C2B]
    assert C2A.slug in comment
    assert graph.issues[C2B].state == "CLOSED" and graph.parent_of(C2B) is None
    assert graph.children_of(C2A) == [kept_item, moved_item]
    assert epic_chain.chain_of(HEAD) == [HEAD, C2A]
    assert capsys.readouterr().out == (
        f"healed epic {HEAD.slug}: {HEAD.slug} held two continuations; kept {C2A.slug}, moved "
        f"1 sub-issue from {C2B.slug} into it, then unlinked {C2B.slug} and closed it as not "
        "planned\n"
    )
    graph.clear_log()
    assert epic_chain.heal_duplicate_continuations(HEAD) is False
    assert graph.events == []


def test_duplicate_heal_follows_a_duplicate_with_its_own_continuation(
    graph: FakeSubIssueGraph,
) -> None:
    _items(graph, HEAD, 98)
    _continuation(graph, C2A, HEAD)
    _continuation(graph, C2B, HEAD)
    _items(graph, C2B, 2)
    _continuation(graph, C3, C2B, k=3)
    (deep_item,) = _items(graph, C3, 1)

    assert epic_chain.heal_duplicate_continuations(HEAD) is True

    assert epic_chain.chain_of(HEAD) == [HEAD, C2A, C3]
    assert graph.parent_of(deep_item) == C3


def test_duplicate_heal_refuses_to_overfill_the_kept_one(graph: FakeSubIssueGraph) -> None:
    _items(graph, HEAD, 98)
    _continuation(graph, C2A, HEAD)
    _items(graph, C2A, 60)
    _continuation(graph, C2B, HEAD)
    _items(graph, C2B, 41)
    graph.clear_log()
    with pytest.raises(RuntimeError, match="past GitHub's limit of 100; a person must"):
        epic_chain.heal_duplicate_continuations(HEAD)
    assert graph.events == []


def test_link_into_heals_duplicates_after_its_own_link(
    graph: FakeSubIssueGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    _items(graph, HEAD, 97)
    _continuation(graph, C2A, HEAD)
    _continuation(graph, C2B, None)
    (rival_item,) = _items(graph, C2B, 1)
    task = _new_task(graph)
    real = epics._add_child

    def racing(parent: IssueRef, child: IssueRef) -> None:
        if child == task:
            graph.link(HEAD, C2B)  # a racing writer's continuation
        real(parent, child)

    monkeypatch.setattr(epics, "_add_child", racing)

    assert epic_chain.link_into(HEAD, task) == HEAD
    assert epic_chain.chain_of(HEAD) == [HEAD, C2A]
    assert graph.children_of(C2A) == [rival_item]
    assert graph.issues[C2B].state == "CLOSED"


# -- close_chain (spec §4.6) ------------------------------------------------------------


def test_close_chain_head_first(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _items(graph, HEAD, 1)
    _continuation(graph, C2, HEAD)
    _items(graph, C2, 1)
    _continuation(graph, C3, C2, k=3)
    graph.clear_log()

    assert epic_chain.close_chain(HEAD) == [HEAD, C2, C3]

    assert graph.events == [
        ("close", HEAD, "completed"),
        ("close", C2, "completed"),
        ("close", C3, "completed"),
    ]
    assert capsys.readouterr().out == (
        f"closed 2 continuations of epic {HEAD.slug}: {C2.slug}, {C3.slug}\n"
    )


def test_close_chain_skips_already_closed(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    _continuation(graph, C2, HEAD, state="CLOSED")
    _continuation(graph, C3, C2, k=3)
    graph.close(HEAD)
    graph.clear_log()

    assert epic_chain.close_chain(HEAD) == [C3]
    assert graph.events == [("close", C3, "completed")]
    assert capsys.readouterr().out == f"closed 1 continuation of epic {HEAD.slug}: {C3.slug}\n"

    graph.clear_log()
    assert epic_chain.close_chain(HEAD) == []
    assert graph.events == []
    assert capsys.readouterr().out == ""


def test_close_chain_of_a_lone_head_prints_nothing(
    graph: FakeSubIssueGraph, capsys: pytest.CaptureFixture[str]
) -> None:
    assert epic_chain.close_chain(HEAD) == [HEAD]
    assert capsys.readouterr().out == ""


# -- the writers' messages ----------------------------------------------------------


def test_container_note_names_a_continuation_only() -> None:
    assert epic_chain.container_note(HEAD, HEAD) == ""
    assert epic_chain.container_note(HEAD, C2) == " (in continuation org/.github#20)"


def test_unlinked_hint_suggests_the_move_for_an_ordinary_failure() -> None:
    task = IssueRef("org", "repo", 5)
    hint = epic_chain.unlinked_hint(task, HEAD, RuntimeError("gh failed"))
    assert hint == "Link it with: vrg-epic-move --task 'org/repo#5' --epic 'org/.github#10'"


@pytest.mark.parametrize(
    "exc",
    [
        epics.ClosedEpicError(HEAD),
        epic_chain.ChainDepthError(HEAD, items=693, containers=7),
    ],
    ids=["closed", "depth"],
)
def test_unlinked_hint_names_a_chain_refusal_instead_of_the_move(exc: Exception) -> None:
    # The move would be refused the same way, so it is not suggested.
    task = IssueRef("org", "repo", 5)
    assert epic_chain.unlinked_hint(task, HEAD, exc) == (
        "org/repo#5 is left unlinked, and vrg-epic-move would be refused the same way "
        "until that is resolved"
    )
