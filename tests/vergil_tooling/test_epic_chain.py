"""Tests for vergil_tooling.lib.epic_chain: an epic's chain of containers (read side)."""

from __future__ import annotations

import pytest

from tests.vergil_tooling import trust_stubs
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
    epics.add_child(HEAD, task)
    epics.reparent_child(OTHER_HEAD, task)
    assert graph.children_of(HEAD) == []
    assert graph.parent_of(task) == OTHER_HEAD
    epics.remove_child(OTHER_HEAD, task)
    assert graph.mutations == [
        ("add", HEAD, task),
        ("reparent", OTHER_HEAD, task),
        ("remove", OTHER_HEAD, task),
    ]
    with pytest.raises(github.GitHubAPIError):
        epics.remove_child(OTHER_HEAD, task)
    epics.add_child(HEAD, task)
    assert [c.ref for c in epics.child_states(HEAD)] == [task]
    assert [c.ref for c in epics.child_states_with_blockers(HEAD)] == [task]
    assert epics.native_parent_of(task) == HEAD


# -- parse_continues -------------------------------------------------------------


def test_parse_continues_reads_the_line_alone_on_its_line() -> None:
    assert epic_chain.parse_continues(_continuation_body(HEAD, 2)) == HEAD
    assert epic_chain.parse_continues("Continues: org/.github#10 and more") is None
    assert epic_chain.parse_continues("no line at all") is None
    assert epic_chain.parse_continues("Continues: org/.github#10\nContinues: org/.github#10") == HEAD
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
        C3, title="Epic: x", labels=["epic"], body="Continues: org/.github#10",
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
