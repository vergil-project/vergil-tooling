"""Tests for vergil_tooling.lib.queue_view (the read-only intake/planning/deferred views)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import epics, github, in_flight, queue_view, trust
from vergil_tooling.lib.epic_size import SizeRecord
from vergil_tooling.lib.epics import ChildState, IssueRef
from vergil_tooling.lib.queue_view import AdvisoryHold, Deferral, PlanningMarkers, QueueItem
from vergil_tooling.lib.security_records import Advisory

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_MOD = "vergil_tooling.lib.queue_view"
_EPIC = IssueRef("org", ".github", 1)


@pytest.fixture(autouse=True)
def authors() -> Iterator[MagicMock]:
    # Each view reads the trusted authors once, for the trust layer's reads.
    with patch(f"{_MOD}.load_trusted_authors", return_value=trust_stubs.AUTHORS) as loader:
        yield loader


# What the trust layer reads for each issue number: the item query's keyword
# arguments (its body, the body's author and editor, its comment nodes). Each
# _FakeGh registers its views here; an issue not registered has a trusted body
# and no comments.
_LAYER: dict[int, dict[str, Any]] = {}


@pytest.fixture(autouse=True)
def layer(monkeypatch: pytest.MonkeyPatch) -> Iterator[MagicMock]:
    """The trust layer's GraphQL reads, answered from :data:`_LAYER`."""
    _LAYER.clear()
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def graphql(query: str, **variables: Any) -> dict[str, Any]:
        return trust_stubs.item(**_LAYER.get(int(variables["number"]), {}))

    with patch("vergil_tooling.lib.github.graphql", side_effect=graphql) as fake:
        yield fake


def _layer_actor(login: str) -> dict[str, str]:
    """A ``(author, body)`` comment's author as GraphQL gives it; ``someone`` is the human."""
    if login == "someone":
        return trust_stubs.HUMAN
    if login.startswith("app/"):
        return {"__typename": "Bot", "login": login.removeprefix("app/")}
    return {"__typename": "User", "login": login}


def _view(
    title: str,
    created_at: str,
    *,
    labels: tuple[str, ...] = (),
    comments: tuple[str | tuple[str, str], ...] = (),
    body: str = "",
    body_author: dict[str, str] = trust_stubs.HUMAN,
    body_editor: dict[str, str] | None = None,
    nodes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An issue view; a comment is a body (by ``someone``) or ``(author, body)``.

    ``gh`` shows no body or comments: the trust layer reads them (``someone``
    as the trusted human), or, given, the comment *nodes*. The body is by
    *body_author*, edited once by *body_editor* when given.
    """
    if nodes is None:
        authored = [c if isinstance(c, tuple) else ("someone", c) for c in comments]
        nodes = [
            trust_stubs.comment(i, b, _layer_actor(a)) for i, (a, b) in enumerate(authored, start=1)
        ]
    return {
        "title": title,
        "createdAt": created_at,
        "labels": [{"name": name} for name in labels],
        "_layer": {"body": body, "author": body_author, "editor": body_editor, "nodes": nodes},
    }


def _register(views: dict[int, dict[str, Any]]) -> None:
    for number, view in views.items():
        _LAYER[number] = view["_layer"]


class _FakeGh:
    """A ``github.read_json`` stand-in keyed by issue number, recording its calls."""

    def __init__(
        self,
        views: dict[int, dict[str, Any]],
        states: dict[int, str] | None = None,
        *,
        listing: list[dict[str, Any]] | None = None,
    ) -> None:
        self.views = views
        _register(views)
        self.states = states or {}
        self.listing = listing or []
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> Any:
        self.calls.append(args)
        if args[:2] == ("issue", "list"):
            return self.listing
        number = int(args[2])
        fields = args[args.index("--json") + 1]
        if fields == "state":
            return {"state": self.states[number]}
        # Only the requested fields, as gh returns them: a view that forgets
        # one sees it missing.
        wanted = fields.split(",")
        return {k: v for k, v in self.views[number].items() if k in wanted}


_fake_gh = _FakeGh


# -- parse_planning_markers ------------------------------------------------------


def test_parse_planning_markers_latest_wins() -> None:
    m = queue_view.parse_planning_markers(
        ["Epic: org/.github#10", "Docs task: org/.github#11", "Epic: org/.github#12"]
    )
    assert m.epic == IssueRef("org", ".github", 12)
    assert m.docs_task == IssueRef("org", ".github", 11)
    assert not m.tasks_filed


def test_parse_planning_markers_empty() -> None:
    assert queue_view.parse_planning_markers([]) == PlanningMarkers(
        epic=None, docs_task=None, tasks_filed=False
    )


def test_parse_planning_markers_lines_within_a_multiline_comment() -> None:
    m = queue_view.parse_planning_markers(
        [
            "Progress: spec drafted.\nEpic: org/.github#20\n  Docs task: org/tool#21  \n"
            "Tasks filed: yes\nWaiting on review."
        ]
    )
    assert m == PlanningMarkers(
        epic=IssueRef("org", ".github", 20),
        docs_task=IssueRef("org", "tool", 21),
        tasks_filed=True,
    )


def test_parse_epic_markers_lists_every_epic_once_in_order() -> None:
    refs = queue_view.parse_epic_markers(
        [
            "Epic: org/.github#10\nDocs task: org/.github#11",
            "note\n  Epic: org/tool#12  ",
            "Epic: org/.github#10",
            "Epic: not-a-ref",
        ]
    )
    assert refs == (IssueRef("org", ".github", 10), IssueRef("org", "tool", 12))


def test_parse_epic_markers_empty() -> None:
    assert queue_view.parse_epic_markers(["Just a note."]) == ()


def test_parse_planning_markers_tasks_filed_latest_no_overrides_yes() -> None:
    m = queue_view.parse_planning_markers(["Tasks filed: yes", "Tasks filed: no"])
    assert not m.tasks_filed


def test_parse_planning_markers_tasks_filed_case_insensitive() -> None:
    assert queue_view.parse_planning_markers(["Tasks filed: YES"]).tasks_filed


def test_parse_planning_markers_ignores_non_marker_and_malformed_lines() -> None:
    m = queue_view.parse_planning_markers(
        ["Epic: not-a-ref", "The Epic: org/.github#5 is mentioned inline", "Docs task: #7"]
    )
    assert m == PlanningMarkers(epic=None, docs_task=None, tasks_filed=False)


def test_marker_constants_are_the_line_prefixes() -> None:
    assert queue_view.EPIC_MARKER == "Epic:"
    assert queue_view.DOCS_TASK_MARKER == "Docs task:"
    assert queue_view.TASKS_FILED_MARKER == "Tasks filed:"
    assert queue_view.PLANNING_MARKER == "Planning:"


# -- intake_items ----------------------------------------------------------------


def test_intake_items_oldest_first() -> None:
    children = [
        ChildState(IssueRef("org", "tool", 3), "OPEN", "newer"),
        ChildState(IssueRef("org", "tool", 9), "CLOSED", "done"),
        ChildState(IssueRef("org", ".github", 4), "OPEN", "older"),
    ]
    views = {
        3: _view("newer", "2026-10-20T00:00:00Z", comments=("first\nsecond", "latest note\nmore")),
        4: _view("older", "2026-09-01T00:00:00Z"),
    }
    gh = _fake_gh(views)
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC) as find,
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=gh),
    ):
        items = queue_view.intake_items("org/.github", "org", stops=trust.BatchStops())
    find.assert_called_once_with(epics.INTAKE, "org/.github", "org")
    assert [i.ref.number for i in items] == [4, 3]
    assert all(i.state == "waiting" and i.epic is None for i in items)
    assert items[0] == QueueItem(
        ref=IssueRef("org", ".github", 4),
        title="older",
        created_at="2026-09-01T00:00:00Z",
        state="waiting",
        note="",
        epic=None,
    )
    assert items[1].note == "latest note"
    # The closed child is never fetched.
    assert {int(c[2]) for c in gh.calls} == {3, 4}


def test_intake_note_empty_when_latest_comment_is_blank() -> None:
    children = [ChildState(IssueRef("org", "tool", 3), "OPEN", "")]
    views = {3: _view("t", "2026-10-20T00:00:00Z", comments=("real note", "   \n  "))}
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=_fake_gh(views)),
    ):
        [item] = queue_view.intake_items("org/.github", "org", stops=trust.BatchStops())
    assert item.note == ""


def test_intake_items_missing_queue_epic_raises() -> None:
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=None),
        pytest.raises(queue_view.QueueNotFoundError, match=r"Epic \(intake\): org.*org/\.github"),
    ):
        queue_view.intake_items("org/.github", "org", stops=trust.BatchStops())


def test_items_never_mutate() -> None:
    """The views are read-only: no create/ensure/write on any path."""
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=[]),
        patch(f"{_MOD}.github.read_json"),
        patch(f"{_MOD}.epics.ensure_standing_epic") as ensure,
        patch(f"{_MOD}.github.create_issue") as create,
        patch(f"{_MOD}.github.run") as run,
    ):
        assert queue_view.intake_items("org/.github", "org", stops=trust.BatchStops()) == []
        assert queue_view.planning_items("org/.github", "org", stops=trust.BatchStops()) == []
        assert queue_view.deferred_items("org/.github", "org", stops=trust.BatchStops()) == []
    ensure.assert_not_called()
    create.assert_not_called()
    run.assert_not_called()


# -- planning_items --------------------------------------------------------------


def _planning(
    views: dict[int, dict[str, Any]],
    states: dict[int, str] | None = None,
    *,
    dotgithub: bool = False,
) -> list[QueueItem]:
    """Run the planning view; *dotgithub* is whether the org ``.github`` exists."""
    children = [ChildState(IssueRef("org", ".github", n), "OPEN", "") for n in views]
    gh = _fake_gh(views, states)
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC) as find,
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(f"{_MOD}.github.repo_exists", return_value=dotgithub),
    ):
        items = queue_view.planning_items("org/.github", "org", stops=trust.BatchStops())
    find.assert_called_once_with(epics.PLANNING, "org/.github", "org")
    return items


_ALL_MARKERS = ("Epic: org/.github#50", "Docs task: org/.github#51\nTasks filed: yes")


def test_planning_waiting_without_label() -> None:
    [item] = _planning({5: _view("plan x", "2026-10-01T00:00:00Z", comments=_ALL_MARKERS)})
    assert item.state == "waiting"
    assert item.epic == IssueRef("org", ".github", 50)


def test_planning_ready_requires_docs_task_closed() -> None:
    view = _view("plan x", "2026-10-01T00:00:00Z", labels=("in-progress",), comments=_ALL_MARKERS)
    [open_docs] = _planning({5: view}, {51: "OPEN"})
    assert open_docs.state == "in-progress"
    [closed_docs] = _planning({5: view}, {51: "CLOSED"})
    assert closed_docs.state == "ready-to-close"
    assert closed_docs.note == "Docs task: org/.github#51"


def test_planning_ready_requires_tasks_filed() -> None:
    view = _view(
        "plan x",
        "2026-10-01T00:00:00Z",
        labels=("in-progress",),
        comments=("Epic: org/.github#50", "Docs task: org/.github#51"),
    )
    [item] = _planning({5: view}, {51: "CLOSED"})
    assert item.state == "in-progress"


def test_planning_in_progress_without_epic_marker() -> None:
    view = _view(
        "plan x",
        "2026-10-01T00:00:00Z",
        labels=("in-progress",),
        comments=("Docs task: org/.github#51\nTasks filed: yes",),
    )
    [item] = _planning({5: view}, {51: "CLOSED"})
    assert item.state == "in-progress"
    assert item.epic is None


def test_planning_in_progress_without_docs_task_does_not_fetch_state() -> None:
    view = _view(
        "plan x",
        "2026-10-01T00:00:00Z",
        labels=("in-progress",),
        comments=("Epic: org/.github#50\nTasks filed: yes",),
    )
    # No states map: a docs-task state lookup would KeyError.
    [item] = _planning({5: view})
    assert item.state == "in-progress"


def test_planning_items_oldest_first_and_comments_fetched_once() -> None:
    views = {
        7: _view("new", "2026-10-10T00:00:00Z"),
        6: _view("old", "2026-01-10T00:00:00Z"),
    }
    children = [ChildState(IssueRef("org", ".github", n), "OPEN", "") for n in views]
    gh = _fake_gh(views)
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(f"{_MOD}.github.repo_exists", return_value=False),
    ):
        items = queue_view.planning_items("org/.github", "org", stops=trust.BatchStops())
    assert [i.title for i in items] == ["old", "new"]
    assert sorted(int(c[2]) for c in gh.calls) == [6, 7]


def test_planning_items_missing_queue_epic_raises() -> None:
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=None),
        pytest.raises(queue_view.QueueNotFoundError, match=r"Epic \(planning\): org"),
    ):
        queue_view.planning_items("org/.github", "org", stops=trust.BatchStops())


def test_issue_view_rejects_non_object_response() -> None:
    children = [ChildState(IssueRef("org", ".github", 5), "OPEN", "")]
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", return_value=[]),
        pytest.raises(ValueError, match=r"org/\.github#5"),
    ):
        queue_view.intake_items("org/.github", "org", stops=trust.BatchStops())


def test_docs_task_state_rejects_non_object_response() -> None:
    view = _view("p", "2026-10-01T00:00:00Z", labels=("in-progress",), comments=_ALL_MARKERS)
    children = [ChildState(IssueRef("org", ".github", 5), "OPEN", "")]
    _register({5: view})
    responses: list[object] = [view, []]
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=responses),
        patch(f"{_MOD}.github.repo_exists", return_value=False),
        pytest.raises(ValueError, match=r"org/\.github#51"),
    ):
        queue_view.planning_items("org/.github", "org", stops=trust.BatchStops())


# -- parse_latest_deferral / deferred_items --------------------------------------


def test_parse_latest_deferral_latest_wins_and_reads_revisit_line() -> None:
    d = queue_view.parse_latest_deferral(
        [
            "Deferred 2026-10-01: waits on X\nRevisit when: X ships\nDeferred from: org/.github#7",
            "Deferred 2026-10-25: still waits on X\nsee the thread\nRevisit when: X 2.0 ships",
        ]
    )
    assert d == Deferral(day="2026-10-25", reason="still waits on X", revisit_when="X 2.0 ships")


def test_parse_latest_deferral_ignores_non_deferral_comments() -> None:
    comments = [
        "Deferred 2026-10-01: waits on X",
        "a later discussion comment\nDeferred 2026-10-30: not a first line",
        "We Deferred 2026-10-31: inline mention",
        "Deferred 26-10-31: malformed date",
    ]
    # The newest real deferral has no Revisit line, so revisit_when is "".
    assert queue_view.parse_latest_deferral(comments) == Deferral(
        day="2026-10-01", reason="waits on X", revisit_when=""
    )
    assert queue_view.parse_latest_deferral([]) is None
    assert queue_view.parse_latest_deferral(["just a note", ""]) is None


def _deferred(views: dict[int, dict[str, Any]]) -> list[QueueItem]:
    children = [ChildState(IssueRef("org", ".github", n), "OPEN", "") for n in views]
    children.append(ChildState(IssueRef("org", ".github", 99), "CLOSED", "gone"))
    gh = _fake_gh(views)
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC) as find,
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=gh),
    ):
        items = queue_view.deferred_items("org/.github", "org", stops=trust.BatchStops())
    find.assert_called_once_with(epics.DEFERRED, "org/.github", "org")
    # Closed children are never fetched; each open child is fetched once.
    assert sorted(int(c[2]) for c in gh.calls) == sorted(views)
    return items


def test_deferred_items_least_recently_deferred_first() -> None:
    views = {
        # A: created later, deferred 2026-10-20.
        1: _view(
            "A",
            "2026-09-15T00:00:00Z",
            comments=("Deferred 2026-10-20: waits on Y\nRevisit when: Y lands",),
        ),
        # B: created earlier, deferred 2026-10-01 then refreshed 2026-10-25.
        2: _view(
            "B",
            "2026-09-01T00:00:00Z",
            comments=(
                "Deferred 2026-10-01: waits on X\nRevisit when: X ships\n"
                "Deferred from: org/.github#7",
                "a discussion comment",
                "Deferred 2026-10-25: still waits on X\nRevisit when: X 2.0 ships",
            ),
        ),
    }
    items = _deferred(views)
    assert [i.title for i in items] == ["A", "B"]
    assert all(i.state == "waiting" and i.epic is None for i in items)
    assert items[0].note == "Deferred 2026-10-20: waits on Y | Revisit when: Y lands"
    assert items[1].note == "Deferred 2026-10-25: still waits on X | Revisit when: X 2.0 ships"


def test_deferred_items_without_deferral_sort_first() -> None:
    views = {
        1: _view(
            "deferred",
            "2026-01-01T00:00:00Z",
            comments=("Deferred 2026-10-01: r\nRevisit when: c",),
        ),
        # Hand-moved or legacy items: no deferral comment at all.
        2: _view("hand-moved", "2026-10-20T00:00:00Z", comments=("just a note",)),
        3: _view("legacy", "2026-10-10T00:00:00Z"),
    }
    items = _deferred(views)
    assert [i.title for i in items] == ["legacy", "hand-moved", "deferred"]
    assert items[0].note == ""
    assert items[1].note == ""
    assert items[2].note == "Deferred 2026-10-01: r | Revisit when: c"


def test_deferred_items_missing_queue_epic_raises() -> None:
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=None),
        pytest.raises(queue_view.QueueNotFoundError, match=r"Epic \(deferred\): org.*org/\.github"),
    ):
        queue_view.deferred_items("org/.github", "org", stops=trust.BatchStops())


# -- trusted comments only (epic vergil-project/.github#1467) -----------------------

_FORGED = (
    f"Epic: org/.github#70\nDocs task: org/.github#71\nTasks filed: yes\n{trust_stubs.SPAM_TEXT}"
)


def _assert_reported(err: str, *lines: str) -> None:
    """*err* is exactly the metadata-only skip reports *lines*, and no untrusted text."""
    assert err.splitlines() == [f"skipped untrusted content: {line}" for line in lines]
    assert "SPAM-MARKER-TEXT" not in err
    assert "example.invalid" not in err


def test_forged_planning_markers_ignored_and_reported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    nodes = [
        trust_stubs.comment(1, "Epic: org/.github#50\nDocs task: org/.github#51"),
        trust_stubs.comment(2, _FORGED, trust_stubs.OUTSIDER),
        trust_stubs.comment(3, _FORGED, trust_stubs.HUMAN, editor=trust_stubs.OUTSIDER),
        trust_stubs.comment(4, _FORGED, trust_stubs.IMPOSTOR),
    ]
    view = _view("plan x", "2026-10-01T00:00:00Z", labels=("in-progress",), nodes=nodes)
    # The forged markers would name a closed docs task and set Tasks filed: yes.
    [item] = _planning({5: view}, {51: "OPEN", 71: "CLOSED"})
    assert item.state == "in-progress"
    assert item.epic == IssueRef("org", ".github", 50)
    assert item.note == "Epic: org/.github#50"
    created, edited = trust_stubs.CREATED, trust_stubs.EDITED
    _assert_reported(
        capsys.readouterr().err,
        f"org/.github#5 comment drive-by at {created} on comment 2 (untrusted)",
        f"org/.github#5 edit drive-by at {edited} on comment 3 (untrusted)",
        f"org/.github#5 comment vergil-user at {created} on comment 4 (wrong-type)",
    )


def test_forged_deferral_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    forged = f"Deferred 2026-11-20: {trust_stubs.SPAM_TEXT}\nRevisit when: never"
    nodes = [
        trust_stubs.comment(1, "Deferred 2026-10-01: waits on X\nRevisit when: X ships"),
        trust_stubs.comment(2, forged, trust_stubs.OUTSIDER),
    ]
    [item] = _deferred({1: _view("d", "2026-09-01T00:00:00Z", nodes=nodes)})
    assert item.note == "Deferred 2026-10-01: waits on X | Revisit when: X ships"
    _assert_reported(
        capsys.readouterr().err,
        f"org/.github#1 comment drive-by at {trust_stubs.CREATED} on comment 2 (untrusted)",
    )


def test_latest_note_from_trusted_only(capsys: pytest.CaptureFixture[str]) -> None:
    nodes = [
        trust_stubs.comment(1, "the trusted note\nmore"),
        trust_stubs.comment(2, trust_stubs.SPAM_TEXT, trust_stubs.OUTSIDER),
    ]
    children = [ChildState(IssueRef("org", "tool", 3), "OPEN", "")]
    views = {3: _view("t", "2026-10-20T00:00:00Z", nodes=nodes)}
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=_fake_gh(views)),
    ):
        [item] = queue_view.intake_items("org/.github", "org", stops=trust.BatchStops())
    assert item.note == "the trusted note"
    _assert_reported(
        capsys.readouterr().err,
        f"org/tool#3 comment drive-by at {trust_stubs.CREATED} on comment 2 (untrusted)",
    )
    # The planning view's latest note is a trusted comment's too.
    [planning] = _planning({5: _view("p", "2026-10-01T00:00:00Z", nodes=nodes)})
    assert planning.note == "the trusted note"


@pytest.mark.parametrize(
    "view_fn",
    [queue_view.intake_items, queue_view.planning_items, queue_view.deferred_items],
)
def test_comment_read_failure_stops_only_its_item(
    layer: MagicMock, capsys: pytest.CaptureFixture[str], view_fn: Any
) -> None:
    # Spec §5.3 of epic vergil-project/.github#1467: a batch run stops the item, not the run.
    readable = trust_stubs.item(nodes=[trust_stubs.comment(1, "the trusted note")])

    def graphql(query: str, **variables: Any) -> dict[str, Any]:
        return {"repository": None} if variables["number"] == 3 else readable

    layer.side_effect = graphql
    children = [ChildState(IssueRef("org", ".github", n), "OPEN", "") for n in (3, 4, 5)]
    views = {n: _view(f"t{n}", f"2026-10-2{n}T00:00:00Z") for n in (3, 4, 5)}
    stops = trust.BatchStops()
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=_fake_gh(views)),
        patch(f"{_MOD}.github.repo_exists", return_value=False),
    ):
        items = view_fn("org/.github", "org", stops=stops)
    assert [item.ref.number for item in items] == [4, 5]
    assert stops.stopped == (
        trust.Skipped("org/.github", 3, "issue", None, "", "comments", trust.READ_FAILURE),
    )
    assert stops.exit_code() == 1
    stops.print_summary(stream := io.StringIO())
    assert stream.getvalue().startswith(
        "stopped on a read failure: org/.github#3 on comments: "
        "cannot read who wrote the content (read_issue org/.github#3)"
    )
    # The view itself prints no stop: the caller reports them after its listing.
    assert "stopped" not in capsys.readouterr().err


def test_no_read_failure_no_stop() -> None:
    stops = trust.BatchStops()
    children = [ChildState(IssueRef("org", ".github", 4), "OPEN", "")]
    views = {4: _view("t", "2026-10-20T00:00:00Z", comments=("a note",))}
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=_fake_gh(views)),
    ):
        [item] = queue_view.intake_items("org/.github", "org", stops=stops)
    assert item.note == "a note"
    assert stops.stopped == ()


# -- planning: the security-advisory hold ------------------------------------------

_GHSA = "GHSA-abcd-efgh-ijkl"
_OTHER_GHSA = "GHSA-mnop-qrst-uvwx"
_ADVISORY_BODY = f"Neutral planning body.\n\nSecurity advisory: org/.github {_GHSA}"
_PLAN = IssueRef("org", ".github", 5)
_FIX = IssueRef("org", "tool", 60)
_RETARGET = (
    " — retarget each to the task or tasks that fix it with vrg-security-record retarget "
    "<GHSA> --from 'org/.github#5' --to <task>"
)
_HELD_NOTE = f"held: 1 advisory still tracks this planning issue ({_GHSA}){_RETARGET}"
_CANNOT_READ = (
    "re-run with a token that can read the repository's advisories, or have the human check it"
)
_TWO_ADVISORY_BODY = f"{_ADVISORY_BODY}\nSecurity advisory: org/.github {_OTHER_GHSA}"


def _advisory(ghsa_id: str, *tracking: IssueRef) -> Advisory:
    return Advisory(
        ghsa_id=ghsa_id, state="draft", affected="tool", tracking=tracking, external=False
    )


def _ready_view(
    body: str = _ADVISORY_BODY, *, labels: tuple[str, ...] = ("in-progress", "security")
) -> Any:
    return _view("plan x", "2026-10-01T00:00:00Z", labels=labels, comments=_ALL_MARKERS, body=body)


def _security_planning(
    views: dict[int, dict[str, Any]],
    advisories: list[Advisory],
    states: dict[int, str] | None = None,
    *,
    dotgithub: bool = True,
) -> tuple[list[QueueItem], Any, Any]:
    with (
        patch(f"{_MOD}.security_records.advisory_home", return_value="org/.github") as home,
        patch(f"{_MOD}.security_records.list_advisories", return_value=advisories) as listing,
    ):
        items = _planning(
            views, states if states is not None else {51: "CLOSED"}, dotgithub=dotgithub
        )
    return items, home, listing


def test_read_advisory_hold_lists_each_advisory_tracking_the_issue() -> None:
    advisories = [
        _advisory(_GHSA, _PLAN),
        _advisory(_OTHER_GHSA, _FIX),
        _advisory("GHSA-cccc-cccc-cccc", IssueRef("Org", ".GITHUB", 5)),
    ]
    with (
        patch(f"{_MOD}.security_records.advisory_home", return_value="org/.github") as home,
        patch(f"{_MOD}.security_records.list_advisories", return_value=advisories),
    ):
        hold = queue_view.read_advisory_hold(_PLAN, _ADVISORY_BODY, {"security"})
    assert hold == AdvisoryHold(tracking=(_GHSA, "GHSA-cccc-cccc-cccc"))
    home.assert_called_once_with("org")


def test_read_advisory_hold_none_without_a_public_dotgithub() -> None:
    with (
        patch(f"{_MOD}.github.repo_exists", return_value=False),
        patch(f"{_MOD}.security_records.list_advisories") as listing,
    ):
        assert queue_view.read_advisory_hold(_PLAN, "Neutral body.", {"security"}) == (
            AdvisoryHold()
        )
    listing.assert_not_called()


def test_advisory_hold_tracking_line_holds() -> None:
    # A tracking line holds whatever the body or the label says.
    for body, labels in [
        ("No advisory line.", set()),
        ("No advisory line.", {"security"}),
        (f"Neutral.\n\nSecurity advisory: org/.github {_OTHER_GHSA}", set()),
    ]:
        hold = queue_view.advisory_hold(_PLAN, body, labels, [_advisory(_GHSA, _PLAN)])
        assert hold.tracking == (_GHSA,)
        assert hold.unreadable == ()
        assert hold.broken is None
        assert hold.held


def test_advisory_hold_unreadable_labelled_holds() -> None:
    # Review Focus 5: the listing does not return the advisory the body names.
    hold = queue_view.advisory_hold(_PLAN, _ADVISORY_BODY, {"security"}, [_advisory(_OTHER_GHSA)])
    assert hold == AdvisoryHold(unreadable=(_GHSA,))
    assert hold.held


def test_advisory_hold_unreadable_unlabelled_is_broken() -> None:
    # Review Focus 5: the same body without the label is a broken pair, not a hold.
    hold = queue_view.advisory_hold(_PLAN, _ADVISORY_BODY, set(), [])
    assert hold.tracking == ()
    assert hold.unreadable == ()
    assert hold.broken == (
        f"broken pair: the issue names {_GHSA} on a Security advisory: line but has no "
        "security label, so no advisory holds it — have the human check the pair"
    )
    assert not hold.held


def test_advisory_hold_readable_advisory_tracking_elsewhere_does_not_hold() -> None:
    # The advisory's own tracking lines decide, never the body line alone.
    hold = queue_view.advisory_hold(_PLAN, _ADVISORY_BODY, {"security"}, [_advisory(_GHSA, _FIX)])
    assert hold == AdvisoryHold()
    assert not hold.held


def test_advisory_hold_compares_ghsa_ids_case_insensitively() -> None:
    hold = queue_view.advisory_hold(
        _PLAN, _ADVISORY_BODY, {"security"}, [_advisory(_GHSA.upper(), _FIX)]
    )
    assert hold == AdvisoryHold()


def test_advisory_hold_refuses_a_foreign_advisory_line() -> None:
    body = f"Neutral.\n\nSecurity advisory: other/.github {_GHSA}"
    with pytest.raises(queue_view.AdvisoryLineError, match="the issue's own org"):
        queue_view.advisory_hold(_PLAN, body, {"security"}, [])


def test_planning_state_held_while_advisory_tracks_it() -> None:
    [item], home, listing = _security_planning({5: _ready_view()}, [_advisory(_GHSA, _PLAN)])
    assert item.state == "in-progress"
    assert item.note == _HELD_NOTE
    home.assert_called_once_with("org")
    listing.assert_called_once_with("org/.github")


def test_planning_state_ready_after_retarget() -> None:
    [item], _home, _listing = _security_planning({5: _ready_view()}, [_advisory(_GHSA, _FIX)])
    assert item.state == "ready-to-close"
    assert item.note == "Docs task: org/.github#51"


def test_planning_held_by_an_advisory_its_body_does_not_name() -> None:
    # The body names _GHSA, which the listing does not return; _OTHER_GHSA still
    # tracks the issue (#3826). Unlabelled, the missing advisory is not a hold.
    [item], _home, _listing = _security_planning(
        {5: _ready_view(labels=("in-progress",))}, [_advisory(_OTHER_GHSA, _PLAN)]
    )
    assert item.state == "in-progress"
    assert item.note == (
        f"held: 1 advisory still tracks this planning issue ({_OTHER_GHSA}){_RETARGET}"
    )


def test_planning_items_holds_unreadable() -> None:
    # Labelled, the advisory the body names and the listing does not return
    # cannot be read: it holds the item, beside the one that tracks it (#3996).
    [item], _home, _listing = _security_planning(
        {5: _ready_view()}, [_advisory(_OTHER_GHSA, _PLAN)]
    )
    assert item.state == "in-progress"
    assert item.note == (
        f"held: 1 advisory still tracks this planning issue ({_OTHER_GHSA}); 1 advisory "
        f"this planning issue names cannot be read ({_GHSA}): {_CANNOT_READ}{_RETARGET}"
    )


def test_planning_items_holds_only_unreadable() -> None:
    [item], _home, _listing = _security_planning({5: _ready_view(_TWO_ADVISORY_BODY)}, [])
    assert item.state == "in-progress"
    assert item.note == (
        "held: 2 advisories this planning issue names cannot be read "
        f"({_GHSA}, {_OTHER_GHSA}): {_CANNOT_READ}{_RETARGET}"
    )


def test_planning_items_reports_a_broken_pair_without_holding() -> None:
    [item], _home, _listing = _security_planning(
        {5: _ready_view(labels=("in-progress",))}, [_advisory(_OTHER_GHSA, _FIX)]
    )
    assert item.state == "ready-to-close"
    assert item.note.startswith(f"broken pair: the issue names {_GHSA} on a Security advisory:")


def test_held_note_names_from() -> None:
    [item], _home, _listing = _security_planning({5: _ready_view()}, [_advisory(_GHSA, _PLAN)])
    assert "vrg-security-record retarget <GHSA> --from 'org/.github#5' --to <task>" in item.note


def test_planning_not_held_when_no_advisory_tracks_it() -> None:
    [item], _home, _listing = _security_planning({5: _ready_view()}, [_advisory(_GHSA, _FIX)])
    assert item.state == "ready-to-close"


def test_planning_held_while_any_named_advisory_tracks_it() -> None:
    # The issue names A and B; A was handed off to its fix task, B still tracks it.
    advisories = [_advisory(_GHSA, _FIX), _advisory(_OTHER_GHSA, _PLAN)]
    [item], home, listing = _security_planning({5: _ready_view(_TWO_ADVISORY_BODY)}, advisories)
    assert item.state == "in-progress"
    assert item.note == (
        f"held: 1 advisory still tracks this planning issue ({_OTHER_GHSA}){_RETARGET}"
    )
    home.assert_called_once_with("org")
    listing.assert_called_once_with("org/.github")


def test_planning_held_note_names_every_advisory_still_tracking() -> None:
    advisories = [_advisory(_GHSA, _PLAN), _advisory(_OTHER_GHSA, _PLAN)]
    [item], _home, _listing = _security_planning({5: _ready_view(_TWO_ADVISORY_BODY)}, advisories)
    assert item.state == "in-progress"
    assert item.note == (
        f"held: 2 advisories still track this planning issue ({_GHSA}, {_OTHER_GHSA}){_RETARGET}"
    )


def test_planning_ready_after_every_advisory_retargeted() -> None:
    advisories = [_advisory(_GHSA, _FIX), _advisory(_OTHER_GHSA, IssueRef("org", "lib", 61))]
    [item], _home, listing = _security_planning({5: _ready_view(_TWO_ADVISORY_BODY)}, advisories)
    assert item.state == "ready-to-close"
    assert item.note == "Docs task: org/.github#51"
    listing.assert_called_once_with("org/.github")


def test_planning_held_item_not_otherwise_ready_keeps_its_note() -> None:
    open_docs, _home, _listing = _security_planning(
        {5: _ready_view()}, [_advisory(_GHSA, _PLAN)], {51: "OPEN"}
    )
    assert [(i.state, i.note) for i in open_docs] == [("in-progress", "Docs task: org/.github#51")]
    waiting, _home, _listing = _security_planning(
        {5: _ready_view(labels=())}, [_advisory(_GHSA, _PLAN)]
    )
    assert [i.state for i in waiting] == ["waiting"]


def test_planning_items_lists_advisories_once() -> None:
    views = {5: _ready_view(), 6: _ready_view(), 7: _ready_view(body="no line here")}
    tracked = (_PLAN, IssueRef("org", ".github", 6))
    items, home, listing = _security_planning(views, [_advisory(_GHSA, *tracked)])
    home.assert_called_once_with("org")
    listing.assert_called_once_with("org/.github")
    assert [(i.ref.number, i.state) for i in items] == [
        (5, "in-progress"),
        (6, "in-progress"),
        (7, "ready-to-close"),
    ]


def test_tracking_line_holds_an_item_with_no_advisory_line() -> None:
    views = {5: _ready_view(), 8: _ready_view(body="An ordinary planning issue.")}
    advisories = [_advisory(_GHSA, _PLAN, IssueRef("org", ".github", 8))]
    items, _home, _listing = _security_planning(views, advisories)
    by_number = {i.ref.number: i for i in items}
    assert by_number[5].state == "in-progress"
    # #8's body names no advisory, but the advisory's tracking line names it (#3826).
    assert by_number[8].state == "in-progress"
    assert by_number[8].note == _HELD_NOTE.replace("'org/.github#5'", "'org/.github#8'")


def test_no_advisory_lines_still_lists_an_existing_home() -> None:
    views = {5: _ready_view(body="Mentions Security advisory: org/.github GHSA-x inline.")}
    items, home, listing = _security_planning(views, [_advisory(_GHSA, _PLAN)])
    assert [(i.state, i.note) for i in items] == [("in-progress", _HELD_NOTE)]
    home.assert_called_once_with("org")
    listing.assert_called_once_with("org/.github")


def test_no_advisory_lines_and_no_dotgithub_lists_nothing() -> None:
    # Security records live only in the org .github, so there is none to list.
    views = {5: _ready_view(body="An ordinary planning issue.")}
    items, home, listing = _security_planning(views, [], dotgithub=False)
    assert [i.state for i in items] == ["ready-to-close"]
    home.assert_not_called()
    listing.assert_not_called()


def test_advisory_line_naming_another_org_is_refused() -> None:
    body = f"Neutral.\n\nSecurity advisory: other/.github {_OTHER_GHSA}"
    with pytest.raises(
        queue_view.AdvisoryLineError,
        match=(
            rf"org/\.github#5: its Security advisory: line names other/\.github {_OTHER_GHSA}, "
            r"not an advisory in org/\.github, the issue's own org"
        ),
    ):
        _security_planning({5: _ready_view(body)}, [_advisory(_OTHER_GHSA, _PLAN)])


def test_advisory_line_org_matches_case_insensitively() -> None:
    body = f"Neutral.\n\nSecurity advisory: ORG/.github {_GHSA}"
    [item], home, _listing = _security_planning({5: _ready_view(body)}, [_advisory(_GHSA, _PLAN)])
    assert (item.state, item.note) == ("in-progress", _HELD_NOTE)
    home.assert_called_once_with("org")


def test_planning_unreadable_advisory_home_fails_loudly() -> None:
    children = [ChildState(_PLAN, "OPEN", "")]
    unreadable = github.GitHubAPIError(1, ["gh", "api"], "", "HTTP 404: Not Found")
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=_fake_gh({5: _ready_view()})),
        patch(f"{_MOD}.security_records.list_advisories", side_effect=unreadable),
        pytest.raises(github.GitHubAPIError, match="HTTP 404"),
    ):
        queue_view.planning_items("org/.github", "org", stops=trust.BatchStops())


def _planning_with_stops(
    views: dict[int, dict[str, Any]], advisories: list[Advisory]
) -> tuple[list[QueueItem], trust.BatchStops, MagicMock]:
    """The planning view and its stops, with *advisories* as the org's listing."""
    children = [ChildState(IssueRef("org", ".github", n), "OPEN", "") for n in views]
    stops = trust.BatchStops()
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=_fake_gh(views, {51: "CLOSED"})),
        patch(f"{_MOD}.github.repo_exists", return_value=True),
        patch(f"{_MOD}.security_records.advisory_home", return_value="org/.github"),
        patch(f"{_MOD}.security_records.list_advisories", return_value=advisories) as listing,
    ):
        items = queue_view.planning_items("org/.github", "org", stops=stops)
    return items, stops, listing


@pytest.mark.parametrize(
    ("author", "editor", "kind", "login", "cause"),
    [
        (trust_stubs.OUTSIDER, None, "issue", "drive-by", "untrusted"),
        (trust_stubs.IMPOSTOR, None, "issue", "vergil-user", "wrong-type"),
        (trust_stubs.HUMAN, trust_stubs.OUTSIDER, "edit", "drive-by", "untrusted"),
    ],
)
def test_untrusted_planning_body_stops_only_its_item(  # noqa: PLR0913 - the parametrised case
    capsys: pytest.CaptureFixture[str],
    author: dict[str, str],
    editor: dict[str, str] | None,
    kind: str,
    login: str,
    cause: str,
) -> None:
    # The Security advisory: lines count only from a body the trust layer
    # trusts (spec §5.2 of epic vergil-project/.github#1467). An untrusted one
    # is a stop on its item (§5.3), never "names no advisory", and none of its
    # lines reaches the hold rule.
    forged = f"Security advisory: other/.github {_OTHER_GHSA}\n{_ADVISORY_BODY}"
    views = {
        5: _view(
            "plan x",
            "2026-10-01T00:00:00Z",
            labels=("in-progress",),
            comments=_ALL_MARKERS,
            body=forged,
            body_author=author,
            body_editor=editor,
        ),
        6: _view("plan y", "2026-10-02T00:00:00Z", comments=("a note",)),
    }
    items, stops, _listing = _planning_with_stops(views, [])
    assert [item.ref.number for item in items] == [6]
    assert items[0].note == "a note"
    at = trust_stubs.EDITED if kind == "edit" else trust_stubs.CREATED
    assert stops.stopped == (trust.Skipped("org/.github", 5, kind, login, at, "body", cause),)
    assert stops.exit_code() == 1
    stops.print_summary(stream := io.StringIO())
    assert f"org/.github#5 {kind} {login} at {at} on body ({cause})" in stream.getvalue()
    err = capsys.readouterr().err
    assert _OTHER_GHSA not in err
    assert "stopped" not in err


def test_untrusted_planning_body_under_actions_is_counted_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    views = {
        5: _view(
            "plan x", "2026-10-01T00:00:00Z", body=_ADVISORY_BODY, body_author=trust_stubs.OUTSIDER
        )
    }
    items, stops, _listing = _planning_with_stops(views, [])
    assert items == []
    assert stops.named == ()
    stops.print_summary(stream := io.StringIO())
    assert "drive-by" not in stream.getvalue()
    assert "#5" not in stream.getvalue()


def test_trusted_planning_body_still_holds() -> None:
    # The same body by a trusted author holds the item, as the hold rule says.
    views = {5: _ready_view()}
    items, stops, _listing = _planning_with_stops(views, [_advisory(_GHSA, _PLAN)])
    [item] = items
    assert (item.state, item.note) == ("in-progress", _HELD_NOTE)
    assert stops.stopped == ()


# -- rendering -------------------------------------------------------------------

_NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def test_render_text_line_format() -> None:
    item = QueueItem(
        ref=IssueRef("org", "tool", 12),
        title="fix the thing",
        created_at="2026-10-20T08:00:00Z",
        state="in-progress",
        note="waiting on research",
        epic=None,
    )
    assert queue_view.render_text([item], now=_NOW) == (
        "org/tool#12  3d  in-progress  fix the thing  — waiting on research"
    )


def test_render_text_omits_empty_note_and_joins_lines() -> None:
    items = [
        QueueItem(IssueRef("org", ".github", 1), "a", "2026-10-23T11:00:00Z", "waiting", "", None),
        QueueItem(IssueRef("org", ".github", 2), "b", "2025-10-23T12:00:00Z", "waiting", "n", None),
    ]
    assert queue_view.render_text(items, now=_NOW) == (
        "org/.github#1  0d  waiting  a\norg/.github#2  365d  waiting  b  — n"
    )


def test_render_text_empty() -> None:
    assert queue_view.render_text([], now=_NOW) == ""


def test_to_json_dicts() -> None:
    item = QueueItem(
        IssueRef("org", ".github", 5),
        "t",
        "2026-10-01T00:00:00Z",
        "ready-to-close",
        "n",
        IssueRef("org", ".github", 50),
    )
    assert queue_view.to_json([item]) == [
        {
            "ref": {"owner": "org", "repo": ".github", "number": 5},
            "title": "t",
            "created_at": "2026-10-01T00:00:00Z",
            "state": "ready-to-close",
            "note": "n",
            "epic": {"owner": "org", "repo": ".github", "number": 50},
        }
    ]


# -- epic size --------------------------------------------------------------------

_SIZED_M14 = (
    "app/vergil-user",
    "Sized 2026-11-16: epic-size M, about 14 tasks.\nEpic-size: M\n"
    "Estimated-tasks: 14\nConfidence: med\nReason: One sentence.",
)
_M14 = SizeRecord(size="M", tasks=14, confidence="med", error=None)
_LABEL_ERROR = "label epic-size:M without a sizing comment"


def _fields(gh: _FakeGh) -> set[str]:
    return {c[c.index("--json") + 1] for c in gh.calls}


def test_view_field_lists() -> None:
    views = {5: _view("p", "2026-10-01T00:00:00Z")}
    children = [ChildState(IssueRef("org", ".github", 5), "OPEN", "")]
    for reader, kind in (
        (queue_view.planning_items, epics.PLANNING),
        (queue_view.deferred_items, epics.DEFERRED),
    ):
        gh = _fake_gh(views)
        with (
            patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC) as find,
            patch(f"{_MOD}.epics.child_states", return_value=children),
            patch(f"{_MOD}.github.read_json", side_effect=gh),
            patch(f"{_MOD}.github.repo_exists", return_value=False),
        ):
            reader("org/.github", "org", stops=trust.BatchStops())
        find.assert_called_once_with(kind, "org/.github", "org")
        [fields] = _fields(gh)
        # The size comes from the labels and the trust layer's comments: gh
        # is never asked for comments (or their authors).
        assert "labels" in fields.split(","), (kind, fields)
        assert "comments" not in fields.split(","), (kind, fields)
        # The planning hold's body comes from the trust layer, never from gh.
        assert "body" not in fields.split(","), (kind, fields)


def test_planning_item_carries_size(authors: MagicMock) -> None:
    [item] = _planning(
        {5: _view("p", "2026-10-01T00:00:00Z", labels=("epic-size:M",), comments=(_SIZED_M14,))}
    )
    assert item.size == _M14
    assert item.note == "Sized 2026-11-16: epic-size M, about 14 tasks."
    authors.assert_called_once_with()


def test_planning_item_untrusted_sizing_is_ignored(capsys: pytest.CaptureFixture[str]) -> None:
    untrusted = ("stranger", _SIZED_M14[1])
    [item] = _planning({5: _view("p", "2026-10-01T00:00:00Z", comments=(untrusted,))})
    assert item.size == SizeRecord(None, None, None, None)
    _assert_reported(
        capsys.readouterr().err,
        f"org/.github#5 comment stranger at {trust_stubs.CREATED} on comment 1 (untrusted)",
    )


# A sizing comment whose reason carries synthetic spam: never shown anywhere.
_FORGED_SIZING = (
    "Sized 2026-11-20: epic-size M, about 14 tasks.\nEpic-size: M\n"
    f"Estimated-tasks: 14\nConfidence: high\nReason: {trust_stubs.SPAM_TEXT}"
)


def _assert_forged_sizing_unseen(item: QueueItem, out: str) -> None:
    """The forged sizing sized nothing and its text is nowhere in *item* or *out*."""
    assert item.size is not None
    assert item.size.error == _LABEL_ERROR
    assert item.note == f"size: {_LABEL_ERROR}"
    rendered = queue_view.render_text([item], now=_NOW) + str(queue_view.to_json([item]))
    for text in (rendered, out):
        assert "SPAM-MARKER-TEXT" not in text
        assert "example.invalid" not in text
        assert "Sized 2026-11-20" not in text


def test_sizing_record_from_user_named_like_app(capsys: pytest.CaptureFixture[str]) -> None:
    nodes = [trust_stubs.comment(1, _FORGED_SIZING, trust_stubs.IMPOSTOR)]
    view = _view("p", "2026-10-01T00:00:00Z", labels=("epic-size:M",), nodes=nodes)
    [item] = _planning({5: view})
    captured = capsys.readouterr()
    _assert_forged_sizing_unseen(item, captured.out)
    _assert_reported(
        captured.err,
        f"org/.github#5 comment vergil-user at {trust_stubs.CREATED} on comment 1 (wrong-type)",
    )


def test_trusted_sizing_comment_edited_by_outsider_does_not_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    nodes = [trust_stubs.comment(1, _FORGED_SIZING, trust_stubs.AGENT, editor=trust_stubs.OUTSIDER)]
    view = _view("d", "2026-10-01T00:00:00Z", labels=("epic-size:M",), nodes=nodes)
    [item] = _deferred({1: view})
    captured = capsys.readouterr()
    _assert_forged_sizing_unseen(item, captured.out)
    _assert_reported(
        captured.err,
        f"org/.github#1 edit drive-by at {trust_stubs.EDITED} on comment 1 (untrusted)",
    )


def test_deferred_item_carries_size(authors: MagicMock) -> None:
    deferral = ("app/vergil-user", "Deferred 2026-11-17: waits\nRevisit when: X ships")
    [item] = _deferred(
        {
            1: _view(
                "d",
                "2026-10-01T00:00:00Z",
                labels=("epic-size:M",),
                comments=(_SIZED_M14, deferral),
            )
        }
    )
    assert item.size == _M14
    assert item.size.display == "M/14"
    assert item.note == "Deferred 2026-11-17: waits | Revisit when: X ships"
    authors.assert_called_once_with()


def test_error_keeps_held_note() -> None:
    view = _ready_view()
    view["labels"].append({"name": "epic-size:M"})
    [item], _home, _listing = _security_planning({5: view}, [_advisory(_GHSA, _PLAN)])
    assert item.size is not None
    assert item.size.error == _LABEL_ERROR
    assert item.note == f"{_HELD_NOTE} | size: {_LABEL_ERROR}"


def test_error_keeps_deferral_note() -> None:
    view = _view(
        "d",
        "2026-10-01T00:00:00Z",
        labels=("epic-size:M",),
        comments=("Deferred 2026-10-01: r\nRevisit when: c",),
    )
    [item] = _deferred({1: view})
    assert item.note == f"Deferred 2026-10-01: r | Revisit when: c | size: {_LABEL_ERROR}"


def test_error_on_deferred_item_without_deferral() -> None:
    [item] = _deferred({1: _view("d", "2026-10-01T00:00:00Z", labels=("epic-size:M",))})
    assert item.note == f"size: {_LABEL_ERROR}"


def test_error_replaces_latest_comment_note() -> None:
    view = _view("p", "2026-10-01T00:00:00Z", labels=("epic-size:M",), comments=("a note",))
    [item] = _planning({5: view})
    assert item.note == f"size: {_LABEL_ERROR}"


def test_intake_item_has_no_size() -> None:
    children = [ChildState(IssueRef("org", "tool", 3), "OPEN", "")]
    views = {3: _view("t", "2026-10-20T00:00:00Z", labels=("epic-size:M",))}
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=_fake_gh(views)),
    ):
        [item] = queue_view.intake_items("org/.github", "org", stops=trust.BatchStops())
    assert item.size is None


def test_trusted_authors_error_propagates(authors: MagicMock) -> None:
    authors.side_effect = ValueError("trusted_authors: 'human' is empty")
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=[]),
        pytest.raises(ValueError, match="'human' is empty"),
    ):
        queue_view.deferred_items("org/.github", "org", stops=trust.BatchStops())


def _sized(number: int, created_at: str, size: SizeRecord | None) -> QueueItem:
    return QueueItem(
        IssueRef("org", ".github", number), f"t{number}", created_at, "waiting", "", None, size
    )


_UNSIZED_RECORD = SizeRecord(None, None, None, None)
_S3_OLD = _sized(1, "2026-01-01T00:00:00Z", SizeRecord("S", 3, "high", None))
_S8_NEW = _sized(2, "2026-09-01T00:00:00Z", SizeRecord("S", 8, "low", None))
_M12 = _sized(3, "2026-03-01T00:00:00Z", SizeRecord("M", 12, "med", None))
_UNSIZED_ITEM = _sized(4, "2025-01-01T00:00:00Z", _UNSIZED_RECORD)
_ERROR_ITEM = _sized(5, "2024-01-01T00:00:00Z", SizeRecord(None, None, None, "bad"))
_SORT_INPUT = [_ERROR_ITEM, _M12, _UNSIZED_ITEM, _S8_NEW, _S3_OLD]


def test_sort_age() -> None:
    assert queue_view.sort_planning(_SORT_INPUT, "age") == [
        _ERROR_ITEM,
        _UNSIZED_ITEM,
        _S3_OLD,
        _M12,
        _S8_NEW,
    ]


def test_sort_size() -> None:
    assert queue_view.sort_planning(_SORT_INPUT, "size") == [
        _S3_OLD,
        _S8_NEW,
        _M12,
        _UNSIZED_ITEM,
        _ERROR_ITEM,
    ]
    # Ties within a size are oldest first, whatever the task count.
    s9_old = _sized(6, "2025-06-01T00:00:00Z", SizeRecord("S", 9, "low", None))
    assert queue_view.sort_planning([_S3_OLD, _S8_NEW, s9_old], "size") == [
        s9_old,
        _S3_OLD,
        _S8_NEW,
    ]


def test_sort_tasks() -> None:
    m12_old = _sized(7, "2020-01-01T00:00:00Z", SizeRecord("M", 12, "med", None))
    assert queue_view.sort_planning([*_SORT_INPUT, m12_old], "tasks") == [
        _S3_OLD,
        _S8_NEW,
        m12_old,
        _M12,
        _UNSIZED_ITEM,
        _ERROR_ITEM,
    ]


def test_sort_rejects_unknown_key() -> None:
    bogus: Any = "bogus"
    with pytest.raises(ValueError, match="bogus"):
        queue_view.sort_planning(_SORT_INPUT, bogus)


def test_no_size_record_sorts_and_filters_as_unsized() -> None:
    bare = _sized(8, "2026-12-01T00:00:00Z", None)
    assert queue_view.sort_planning([bare, _ERROR_ITEM, _S3_OLD], "size") == [
        _S3_OLD,
        bare,
        _ERROR_ITEM,
    ]
    assert queue_view.filter_planning([bare, _S3_OLD], frozenset({"S"})) == [_S3_OLD]
    assert queue_view.filter_planning([bare, _S3_OLD], frozenset({"unsized"})) == [bare]


def test_filter_keeps_errors() -> None:
    assert queue_view.filter_planning(_SORT_INPUT, frozenset({"S"})) == [
        _ERROR_ITEM,
        _S8_NEW,
        _S3_OLD,
    ]


def test_filter_unsized() -> None:
    assert queue_view.filter_planning(_SORT_INPUT, frozenset({"unsized", "M"})) == [
        _ERROR_ITEM,
        _M12,
        _UNSIZED_ITEM,
    ]


def test_parse_size_filter() -> None:
    assert queue_view.parse_size_filter("S,unsized") == frozenset({"S", "unsized"})
    assert queue_view.parse_size_filter(" M , XL") == frozenset({"M", "XL"})
    for bad in ("s", "S,medium", "", "S,"):
        with pytest.raises(ValueError, match="unknown size"):
            queue_view.parse_size_filter(bad)


def test_size_errors() -> None:
    assert queue_view.size_errors(_SORT_INPUT) == [(_ERROR_ITEM, "bad")]


def test_render_text_size_column() -> None:
    error = QueueItem(
        IssueRef("org", ".github", 5),
        "e",
        "2026-10-20T08:00:00Z",
        "waiting",
        "size: bad",
        None,
        SizeRecord(None, None, None, "bad"),
    )
    items = [
        _sized(1, "2026-10-20T08:00:00Z", SizeRecord("M", 14, "med", None)),
        _sized(2, "2026-10-20T08:00:00Z", _UNSIZED_RECORD),
        error,
    ]
    assert queue_view.render_text(items, now=_NOW).splitlines() == [
        "org/.github#1  3d  M/14  waiting  t1",
        "org/.github#2  3d  ?  waiting  t2",
        "org/.github#5  3d  !  waiting  e  — size: bad",
    ]


def test_to_json_size_fields() -> None:
    [sized, unsized, error] = queue_view.to_json([_M12, _UNSIZED_ITEM, _ERROR_ITEM])
    assert "size" not in sized
    assert {k: sized[k] for k in ("epic_size", "estimated_tasks", "confidence", "size_error")} == {
        "epic_size": "M",
        "estimated_tasks": 12,
        "confidence": "med",
        "size_error": None,
    }
    assert [unsized[k] for k in ("epic_size", "estimated_tasks", "confidence", "size_error")] == [
        None,
        None,
        None,
        None,
    ]
    assert error["size_error"] == "bad"
    assert error["epic_size"] is None
    intake = QueueItem(
        IssueRef("org", ".github", 9), "i", "2026-10-01T00:00:00Z", "waiting", "", None
    )
    assert set(queue_view.to_json([intake])[0]) == {
        "ref",
        "title",
        "created_at",
        "state",
        "note",
        "epic",
    }


# -- adhoc_items -----------------------------------------------------------------

_TOOL_EPIC = IssueRef("org", ".github", 100)
_PLUGIN_EPIC = IssueRef("org", ".github", 200)
_ADHOC_LISTING = [
    {"number": 100, "title": "Epic (ad hoc): vergil-tooling"},
    {"number": 200, "title": "Epic (ad hoc): vergil-claude-plugin"},
]


def _no_marks_clones(repos: Any, roots: Any) -> dict[str, Path]:
    return {repo: Path(f"/roots/{repo}") for repo in repos}


def _no_marks_signals(repo: str, clone: Path) -> in_flight.RepoSignals:
    return in_flight.RepoSignals(repo, clone, frozenset(), frozenset(), frozenset(), frozenset())


def _adhoc(
    listing: list[dict[str, Any]],
    children: dict[int, list[ChildState]],
    views: dict[int, dict[str, Any]],
    *,
    blockers: Mapping[int, list[IssueRef] | Exception] | None = None,
    states: dict[int, str] | None = None,
    repo: str | None = None,
    stops: trust.BatchStops | None = None,
    native: Mapping[int, tuple[epics.BlockerState, ...] | Exception] | None = None,
) -> tuple[list[queue_view.AdhocItem], _FakeGh]:
    """Run :func:`queue_view.adhoc_items` on ``org/.github`` against fakes.

    A *blockers* value that is an exception is raised by that task's read.
    *native* is what :func:`epics.native_blockers_of` gives a reflink child
    (none by default); an exception there is raised too.
    """
    gh = _FakeGh(views, listing=listing)
    blocked = blockers or {}
    natives = native or {}

    def blockers_of(ref: IssueRef) -> list[IssueRef]:
        found = blocked.get(ref.number, [])
        if isinstance(found, Exception):
            raise found
        return found

    def native_blockers_of(ref: IssueRef) -> tuple[epics.BlockerState, ...]:
        found = natives.get(ref.number, ())
        if isinstance(found, Exception):
            raise found
        return found

    with (
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(
            f"{_MOD}.epics.child_states_with_blockers",
            side_effect=lambda epic: children[epic.number],
        ),
        patch(f"{_MOD}.epics.blockers_of", side_effect=blockers_of),
        patch(f"{_MOD}.epics.native_blockers_of", side_effect=native_blockers_of),
        patch(f"{_MOD}.epics.issue_state", side_effect=lambda ref: (states or {})[ref.number]),
        patch(f"{_MOD}.in_flight.find_clones", side_effect=_no_marks_clones),
        patch(f"{_MOD}.in_flight.repo_signals", side_effect=_no_marks_signals),
    ):
        items = queue_view.adhoc_items(
            "org/.github",
            stops=trust.BatchStops() if stops is None else stops,
            repo=repo,
            roots=[Path("/roots")],
        )
    return items, gh


def test_adhoc_items_lists_open_tasks_across_epics() -> None:
    children = {
        100: [
            ChildState(IssueRef("org", "vergil-tooling", 3), "OPEN", "newer"),
            ChildState(IssueRef("org", "vergil-tooling", 4), "CLOSED", "done"),
            ChildState(IssueRef("org", "vergil-tooling", 5), "OPEN", "older"),
        ],
        200: [ChildState(IssueRef("org", "vergil-claude-plugin", 9), "OPEN", "plugin")],
    }
    views = {
        3: _view("newer", "2026-10-20T00:00:00Z"),
        5: _view("older", "2026-09-01T00:00:00Z"),
        9: _view("plugin", "2026-10-25T00:00:00Z"),
    }
    items, gh = _adhoc(_ADHOC_LISTING, children, views)
    # Repos in name order, oldest first within each; the closed child is gone.
    assert [i.ref.slug for i in items] == [
        "org/vergil-claude-plugin#9",
        "org/vergil-tooling#5",
        "org/vergil-tooling#3",
    ]
    assert items[1] == queue_view.AdhocItem(
        ref=IssueRef("org", "vergil-tooling", 5),
        title="older",
        created_at="2026-09-01T00:00:00Z",
        kind="task",
        blocked_by=(),
        state="ready",
        marks=(),
        disposition=None,
    )
    # One label search, with the listing limit, and the closed child never fetched.
    [listing] = [c for c in gh.calls if c[:2] == ("issue", "list")]
    assert listing == (
        "issue",
        "list",
        "--repo",
        "org/.github",
        "--label",
        "epic",
        "--label",
        "ad-hoc",
        "--state",
        "open",
        "--limit",
        str(epics.LISTING_LIMIT),
        "--json",
        "number,title",
    )
    assert {int(c[2]) for c in gh.calls if c[1] == "view"} == {3, 5, 9}


def test_adhoc_items_skips_archives_and_legacy_titles() -> None:
    listing = [
        {"number": 100, "title": "Epic (ad hoc): vergil-tooling"},
        {"number": 300, "title": "Epic (ad hoc): vergil-tooling — 2026-Q2"},
        {"number": 301, "title": "Archive (ad hoc): vergil-tooling — 2026-Q3"},
        {"number": 302, "title": "Something else"},
    ]
    children = {100: [ChildState(IssueRef("org", "vergil-tooling", 3), "OPEN", "t")]}
    views = {3: _view("t", "2026-10-20T00:00:00Z")}
    # child_states is keyed only for the live epic: reading an archive would KeyError.
    items, _ = _adhoc(listing, children, views)
    assert [i.ref.number for i in items] == [3]


def test_adhoc_items_refuses_a_truncated_search() -> None:
    listing = [
        {"number": n, "title": f"Epic (ad hoc): repo-{n}"} for n in range(epics.LISTING_LIMIT)
    ]
    with pytest.raises(epics.EpicListTruncatedError, match="limit"):
        _adhoc(listing, {}, {})


def test_adhoc_items_refuses_two_live_epics_for_one_repo() -> None:
    listing = [
        {"number": 100, "title": "Epic (ad hoc): vergil-tooling"},
        {"number": 101, "title": "Epic (ad hoc): vergil-tooling"},
    ]
    with pytest.raises(epics.EpicTitleAmbiguousError, match="#100, #101"):
        _adhoc(listing, {}, {})


def test_adhoc_items_blocked_state() -> None:
    children = {
        100: [
            ChildState(IssueRef("org", "vergil-tooling", 3), "OPEN", "held"),
            ChildState(IssueRef("org", "vergil-tooling", 5), "OPEN", "free"),
        ]
    }
    views = {
        3: _view("held", "2026-09-01T00:00:00Z"),
        5: _view("free", "2026-10-01T00:00:00Z"),
    }
    blockers = {
        3: [IssueRef("org", "vergil-tooling", 40), IssueRef("org", "docs", 41)],
        5: [IssueRef("org", "vergil-tooling", 42)],
    }
    states = {40: "CLOSED", 41: "OPEN", 42: "CLOSED"}
    items, _ = _adhoc(_ADHOC_LISTING[:1], children, views, blockers=blockers, states=states)
    held, free = items
    assert held.state == "blocked"
    assert held.blocked_by == (IssueRef("org", "docs", 41),)
    assert free.state == "ready"
    assert free.blocked_by == ()


def _blocker(owner: str, repo: str, number: int, state: str) -> epics.BlockerState:
    return epics.BlockerState(IssueRef(owner, repo, number), state)


def test_adhoc_items_native_blockers_from_the_listing() -> None:
    # #5094: a task held only by native blocked-by relationships is blocked,
    # a blocker in another org included, and the listing's read is used as is.
    children = {
        100: [
            ChildState(
                IssueRef("org", "vergil-tooling", 3),
                "OPEN",
                "held",
                native_blockers=(
                    _blocker("other-org", "lib", 131, "OPEN"),
                    _blocker("org", "vergil-tooling", 40, "CLOSED"),
                ),
            ),
            ChildState(
                IssueRef("org", "vergil-tooling", 5),
                "OPEN",
                "free",
                native_blockers=(_blocker("other-org", "lib", 84, "CLOSED"),),
            ),
        ]
    }
    views = {
        3: _view("held", "2026-09-01T00:00:00Z"),
        5: _view("free", "2026-10-01T00:00:00Z"),
    }
    # No second read: a per-task read of either task would raise.
    unread: dict[int, tuple[epics.BlockerState, ...] | Exception] = {
        n: AssertionError("read again") for n in (3, 5)
    }
    items, _ = _adhoc(_ADHOC_LISTING[:1], children, views, native=unread)
    held, free = items
    assert (held.state, held.blocked_by) == ("blocked", (IssueRef("other-org", "lib", 131),))
    assert (free.state, free.blocked_by) == ("ready", ())


def test_adhoc_items_body_and_native_blockers_merge() -> None:
    # Body refs first, then native ones not already named (GitHub names ignore case).
    children = {
        100: [
            ChildState(
                IssueRef("org", "vergil-tooling", 3),
                "OPEN",
                "held",
                native_blockers=(
                    _blocker("Org", "Docs", 41, "OPEN"),
                    _blocker("org", "vergil-tooling", 43, "OPEN"),
                ),
            )
        ]
    }
    views = {3: _view("held", "2026-09-01T00:00:00Z")}
    # A body ref named twice is listed once.
    blockers = {
        3: [
            IssueRef("org", "docs", 41),
            IssueRef("org", "vergil-tooling", 40),
            IssueRef("org", "Docs", 41),
        ]
    }
    states = {40: "OPEN", 41: "OPEN"}
    items, _ = _adhoc(_ADHOC_LISTING[:1], children, views, blockers=blockers, states=states)
    [held] = items
    assert held.state == "blocked"
    assert held.blocked_by == (
        IssueRef("org", "docs", 41),
        IssueRef("org", "vergil-tooling", 40),
        IssueRef("org", "vergil-tooling", 43),
    )


def test_adhoc_items_reads_a_reflink_childs_native_blockers() -> None:
    # A reflink-fallback child carries no native read, so it is read on its own.
    children = {100: [ChildState(IssueRef("org", "vergil-tooling", 3), "OPEN", "held")]}
    views = {3: _view("held", "2026-09-01T00:00:00Z")}
    native = {3: (_blocker("other-org", "lib", 131, "OPEN"),)}
    items, _ = _adhoc(_ADHOC_LISTING[:1], children, views, native=native)
    [held] = items
    assert (held.state, held.blocked_by) == ("blocked", (IssueRef("other-org", "lib", 131),))


def test_adhoc_items_native_blocker_read_failure_raises() -> None:
    # A native list that cannot be read in full fails the view, never shows the task ready.
    children = {100: [ChildState(IssueRef("org", "vergil-tooling", 3), "OPEN", "held")]}
    views = {3: _view("held", "2026-09-01T00:00:00Z")}
    failure = epics.NativeBlockersReadError("org/vergil-tooling#3 has 2 native blockers")
    with pytest.raises(epics.NativeBlockersReadError, match="2 native blockers"):
        _adhoc(_ADHOC_LISTING[:1], children, views, native={3: failure})


# A task whose body an outsider edited, and one whose body cannot be read in full.
_UNTRUSTED_BODY = trust.Skipped(
    "org/vergil-tooling", 3, "edit", "drive-by", "2026-11-01T00:00:00Z", "body", trust.UNTRUSTED
)
_BODY_READ_FAILURE = trust.Skipped(
    "org/vergil-tooling", 4, "issue", None, "", "body", trust.READ_FAILURE
)


def _stopping_blockers() -> dict[int, list[IssueRef] | Exception]:
    """#3's body untrusted, #4's unreadable, #5 blocked by nothing."""
    return {
        3: trust.UntrustedContentError(_UNTRUSTED_BODY, "the issue body is untrusted"),
        4: trust.TrustReadError("read_issue org/vergil-tooling#4", "the body is missing"),
        5: [],
    }


def test_adhoc_untrusted_body_stops_only_its_task(capsys: pytest.CaptureFixture[str]) -> None:
    # Spec §5.3 of epic vergil-project/.github#1467: a batch run stops the item (#4971).
    children = {
        100: [ChildState(IssueRef("org", "vergil-tooling", n), "OPEN", "") for n in (3, 4, 5)]
    }
    views = {n: _view(f"t{n}", f"2026-10-2{n}T00:00:00Z") for n in (3, 4, 5)}
    stops = trust.BatchStops()
    items, _ = _adhoc(
        _ADHOC_LISTING[:1], children, views, blockers=_stopping_blockers(), stops=stops
    )
    assert [(item.ref.number, item.state) for item in items] == [(5, "ready")]
    assert stops.stopped == (_UNTRUSTED_BODY, _BODY_READ_FAILURE)
    assert stops.exit_code() == 1
    stops.print_summary(stream := io.StringIO())
    assert stream.getvalue() == (
        "stopped: org/vergil-tooling#3 edit drive-by at 2026-11-01T00:00:00Z on body (untrusted)\n"
        "stopped on a read failure: org/vergil-tooling#4 on body: cannot read who wrote the "
        "content (read_issue org/vergil-tooling#4): the body is missing\n"
    )
    # The view prints no stop itself: the caller reports them after its listing.
    assert capsys.readouterr().err == ""


def test_hoshin_untrusted_body_stops_only_its_task() -> None:
    children = [ChildState(IssueRef("org", "vergil-tooling", n), "OPEN", "") for n in (3, 4, 5)]
    views = {n: _view(f"t{n}", f"2026-10-2{n}T00:00:00Z") for n in (3, 4, 5)}
    blocked = _stopping_blockers()

    def blockers_of(ref: IssueRef) -> list[IssueRef]:
        found = blocked[ref.number]
        if isinstance(found, Exception):
            raise found
        return found

    stops = trust.BatchStops()
    with (
        patch(f"{_MOD}.github.read_json", side_effect=_FakeGh(views)),
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_HOSHIN_EPIC),
        patch(f"{_MOD}.epics.child_states_with_blockers", return_value=children),
        patch(f"{_MOD}.epics.blockers_of", side_effect=blockers_of),
        patch(f"{_MOD}.epics.native_blockers_of", return_value=()),
        patch(f"{_MOD}.in_flight.find_clones", side_effect=_no_marks_clones),
        patch(f"{_MOD}.in_flight.repo_signals", side_effect=_no_marks_signals),
    ):
        items = queue_view.hoshin_items("org/.github", stops=stops, roots=[Path("/roots")])
    assert [item.ref.number for item in items] == [5]
    assert stops.stopped == (_UNTRUSTED_BODY, _BODY_READ_FAILURE)


def test_adhoc_items_kind_from_labels() -> None:
    children = {
        100: [
            ChildState(IssueRef("org", "vergil-tooling", 3), "OPEN", ""),
            ChildState(IssueRef("org", "vergil-tooling", 4), "OPEN", ""),
            ChildState(IssueRef("org", "vergil-tooling", 5), "OPEN", ""),
        ]
    }
    views = {
        3: _view("v", "2026-09-01T00:00:00Z", labels=("validation",)),
        4: _view("d", "2026-09-02T00:00:00Z", labels=("deployment", "bug")),
        5: _view("t", "2026-09-03T00:00:00Z", labels=("bug",)),
    }
    items, _ = _adhoc(_ADHOC_LISTING[:1], children, views)
    assert [i.kind for i in items] == ["validation", "deployment", "task"]


def test_adhoc_repo_narrows_to_one_epic() -> None:
    children = {100: [ChildState(IssueRef("org", "vergil-tooling", 3), "OPEN", "t")]}
    views = {3: _view("t", "2026-10-20T00:00:00Z")}
    # The plugin epic is never read: child_states has no entry for it.
    items, _ = _adhoc(_ADHOC_LISTING, children, views, repo="org/vergil-tooling")
    assert [i.ref.slug for i in items] == ["org/vergil-tooling#3"]


def test_adhoc_repo_without_epic_is_empty() -> None:
    items, _ = _adhoc(_ADHOC_LISTING, {}, {}, repo="org/no-adhoc-yet")
    assert items == []


@pytest.mark.parametrize(
    ("listing", "match"),
    [
        ({"message": "not a list"}, "unexpected 'gh issue list' response"),
        (["not a row"], "unexpected 'gh issue list' row"),
    ],
)
def test_adhoc_items_malformed_search_raises(listing: Any, match: str) -> None:
    with (
        patch(f"{_MOD}.github.read_json", return_value=listing),
        pytest.raises(ValueError, match=match),
    ):
        queue_view.adhoc_items("org/.github", stops=trust.BatchStops())


def test_adhoc_items_no_epics_is_empty() -> None:
    assert _adhoc([], {}, {})[0] == []


def test_adhoc_items_rejects_a_bare_repo() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        queue_view.adhoc_items("org/.github", stops=trust.BatchStops(), repo="vergil-tooling")


def test_adhoc_items_sub_issue_read_failure_raises() -> None:
    gh = _FakeGh({}, listing=_ADHOC_LISTING[:1])
    failure = github.GitHubAPIError(1, ["gh", "api", "graphql"], "", "HTTP 502")
    with (
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(f"{_MOD}.epics.child_states_with_blockers", side_effect=failure),
        pytest.raises(github.GitHubAPIError, match="502"),
    ):
        queue_view.adhoc_items("org/.github", stops=trust.BatchStops())


@pytest.mark.parametrize("failing", ["blockers_of", "issue_state"])
def test_adhoc_items_blocker_read_failure_raises(failing: str) -> None:
    gh = _FakeGh({3: _view("t", "2026-10-20T00:00:00Z")}, listing=_ADHOC_LISTING[:1])
    failure = github.GitHubAPIError(1, ["gh", "api"], "", "HTTP 403")
    dep = IssueRef("org", "vergil-tooling", 40)
    blockers: Any = failure if failing == "blockers_of" else [dep]
    state: Any = failure if failing == "issue_state" else "OPEN"
    with (
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(
            f"{_MOD}.epics.child_states_with_blockers",
            return_value=[ChildState(IssueRef("org", "vergil-tooling", 3), "OPEN", "t")],
        ),
        patch(f"{_MOD}.epics.blockers_of", **_effect(blockers)),
        patch(f"{_MOD}.epics.native_blockers_of", return_value=()),
        patch(f"{_MOD}.epics.issue_state", **_effect(state)),
        pytest.raises(github.GitHubAPIError, match="403"),
    ):
        queue_view.adhoc_items("org/.github", stops=trust.BatchStops())


def _effect(value: Any) -> dict[str, Any]:
    """A ``patch`` keyword: raise *value* when it is an exception, else return it."""
    if isinstance(value, BaseException):
        return {"side_effect": value}
    return {"return_value": value}


def test_adhoc_items_never_mutate() -> None:
    with (
        patch(f"{_MOD}.github.read_json", side_effect=_FakeGh({}, listing=_ADHOC_LISTING)),
        patch(f"{_MOD}.epics.child_states_with_blockers", return_value=[]),
        patch(f"{_MOD}.epics.ensure_standing_epic") as ensure,
        patch(f"{_MOD}.github.create_issue") as create,
        patch(f"{_MOD}.github.run") as run,
    ):
        assert queue_view.adhoc_items("org/.github", stops=trust.BatchStops()) == []
    ensure.assert_not_called()
    create.assert_not_called()
    run.assert_not_called()


# -- hoshin_items ----------------------------------------------------------------

_HOSHIN_EPIC = IssueRef("org", ".github", 500)


def _worktree_signals(repo: str, clone: Path) -> in_flight.RepoSignals:
    """Signals with a local ``feature/7-…`` branch, the worktree mark of task #7."""
    return in_flight.RepoSignals(
        repo, clone, frozenset(), frozenset(), frozenset(), frozenset({"feature/7-logbook"})
    )


def _hoshin(
    epic: IssueRef | None,
    children: list[ChildState],
    views: dict[int, dict[str, Any]],
    *,
    repo: str | None = None,
    signals: Any = _no_marks_signals,
) -> tuple[list[queue_view.AdhocItem], MagicMock, MagicMock]:
    """Run :func:`queue_view.hoshin_items` on ``org/.github`` against fakes."""
    gh = _FakeGh(views)
    with (
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(f"{_MOD}.epics.find_standing_epic", return_value=epic) as find,
        patch(f"{_MOD}.epics.child_states_with_blockers", return_value=children) as kids,
        patch(f"{_MOD}.epics.blockers_of", return_value=[]),
        patch(f"{_MOD}.epics.native_blockers_of", return_value=()),
        patch(f"{_MOD}.in_flight.find_clones", side_effect=_no_marks_clones),
        patch(f"{_MOD}.in_flight.repo_signals", side_effect=signals),
        patch(f"{_MOD}.epics.ensure_standing_epic") as ensure,
    ):
        items = queue_view.hoshin_items(
            "org/.github", stops=trust.BatchStops(), repo=repo, roots=[Path("/roots")]
        )
    ensure.assert_not_called()
    find.assert_called_once_with(epics.HOSHIN, "org/.github", "org")
    return items, find, kids


def test_hoshin_items_lists_open_tasks() -> None:
    children = [
        ChildState(IssueRef("org", "docs", 7), "OPEN", "Logbook: 2026-11-18"),
        ChildState(IssueRef("org", "docs", 6), "CLOSED", "Logbook: 2026-11-17"),
    ]
    views = {7: _view("Logbook: 2026-11-18", "2026-11-18T00:00:00Z")}
    items, _, kids = _hoshin(_HOSHIN_EPIC, children, views, signals=_worktree_signals)
    kids.assert_called_once_with(_HOSHIN_EPIC)
    assert items == [
        queue_view.AdhocItem(
            ref=IssueRef("org", "docs", 7),
            title="Logbook: 2026-11-18",
            created_at="2026-11-18T00:00:00Z",
            kind="task",
            blocked_by=(),
            state="in-flight",
            marks=("worktree",),
            disposition="resume",
        )
    ]


def test_hoshin_items_no_epic_is_empty() -> None:
    items, _, kids = _hoshin(None, [], {})
    assert items == []
    kids.assert_not_called()


def test_hoshin_items_repo_filters_children() -> None:
    children = [
        ChildState(IssueRef("org", "docs", 7), "OPEN", "Logbook: 2026-11-18"),
        ChildState(IssueRef("org", "vergil-tooling", 8), "OPEN", "stray"),
    ]
    # Only the docs task is fetched: the other repo's view would KeyError.
    views = {7: _view("Logbook: 2026-11-18", "2026-11-18T00:00:00Z")}
    items, _, _ = _hoshin(_HOSHIN_EPIC, children, views, repo="org/Docs")
    assert [i.ref.slug for i in items] == ["org/docs#7"]


def test_hoshin_items_without_repo_lists_every_repo() -> None:
    children = [
        ChildState(IssueRef("org", "vergil-tooling", 8), "OPEN", "stray"),
        ChildState(IssueRef("org", "docs", 7), "OPEN", "Logbook: 2026-11-18"),
    ]
    views = {
        7: _view("Logbook: 2026-11-18", "2026-11-18T00:00:00Z"),
        8: _view("stray", "2026-11-01T00:00:00Z"),
    }
    items, _, _ = _hoshin(_HOSHIN_EPIC, children, views)
    assert [i.ref.slug for i in items] == ["org/docs#7", "org/vergil-tooling#8"]


def test_hoshin_items_rejects_a_bare_repo() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        queue_view.hoshin_items("org/.github", stops=trust.BatchStops(), repo="docs")


def test_hoshin_items_ambiguous_epic_raises() -> None:
    failure = epics.EpicTitleAmbiguousError("two hoshin epics", [_HOSHIN_EPIC])
    with (
        patch(f"{_MOD}.epics.find_standing_epic", side_effect=failure),
        pytest.raises(epics.EpicTitleAmbiguousError, match="two hoshin epics"),
    ):
        queue_view.hoshin_items("org/.github", stops=trust.BatchStops())


def test_hoshin_items_sub_issue_read_failure_raises() -> None:
    failure = github.GitHubAPIError(1, ["gh", "api", "graphql"], "", "HTTP 502")
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_HOSHIN_EPIC),
        patch(f"{_MOD}.epics.child_states_with_blockers", side_effect=failure),
        pytest.raises(github.GitHubAPIError, match="502"),
    ):
        queue_view.hoshin_items("org/.github", stops=trust.BatchStops())


# -- render_adhoc_text / adhoc_to_json --------------------------------------------

_ADHOC_ITEMS = [
    queue_view.AdhocItem(
        IssueRef("org", "tool", 1), "a", "2026-10-23T12:00:00Z", "task", (), "ready", (), None
    ),
    queue_view.AdhocItem(
        IssueRef("org", "tool", 2),
        "b",
        "2025-10-23T12:00:00Z",
        "validation",
        (IssueRef("org", "tool", 7), IssueRef("org", "docs", 8)),
        "blocked",
        (),
        None,
    ),
]


def test_render_adhoc_text() -> None:
    assert queue_view.render_adhoc_text(_ADHOC_ITEMS, now=_NOW) == (
        "org/tool#1  0d  ready  task  a\n"
        "org/tool#2  365d  blocked  validation  b  — blocked by org/tool#7, org/docs#8"
    )


def test_render_adhoc_text_empty() -> None:
    assert queue_view.render_adhoc_text([], now=_NOW) == ""


def test_adhoc_to_json_dicts() -> None:
    assert queue_view.adhoc_to_json(_ADHOC_ITEMS[1:]) == [
        {
            "ref": {"owner": "org", "repo": "tool", "number": 2},
            "title": "b",
            "created_at": "2025-10-23T12:00:00Z",
            "kind": "validation",
            "blocked_by": [
                {"owner": "org", "repo": "tool", "number": 7},
                {"owner": "org", "repo": "docs", "number": 8},
            ],
            "state": "blocked",
            "marks": [],
            "disposition": None,
        }
    ]
