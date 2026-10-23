"""Tests for vergil_tooling.lib.queue_view (the read-only intake/planning views)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import epics, queue_view
from vergil_tooling.lib.epics import ChildState, IssueRef
from vergil_tooling.lib.queue_view import PlanningMarkers, QueueItem

_MOD = "vergil_tooling.lib.queue_view"
_EPIC = IssueRef("org", ".github", 1)


def _view(
    title: str,
    created_at: str,
    *,
    labels: tuple[str, ...] = (),
    comments: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "title": title,
        "createdAt": created_at,
        "labels": [{"name": name} for name in labels],
        "comments": [{"body": body} for body in comments],
    }


class _FakeGh:
    """A ``github.read_json`` stand-in keyed by issue number, recording its calls."""

    def __init__(
        self, views: dict[int, dict[str, Any]], states: dict[int, str] | None = None
    ) -> None:
        self.views = views
        self.states = states or {}
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, *args: str) -> dict[str, Any]:
        self.calls.append(args)
        number = int(args[2])
        if args[args.index("--json") + 1] == "state":
            return {"state": self.states[number]}
        return self.views[number]


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
        items = queue_view.intake_items("org/.github", "org")
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
        [item] = queue_view.intake_items("org/.github", "org")
    assert item.note == ""


def test_intake_items_missing_queue_epic_raises() -> None:
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=None),
        pytest.raises(queue_view.QueueNotFoundError, match=r"Epic \(intake\): org.*org/\.github"),
    ):
        queue_view.intake_items("org/.github", "org")


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
        assert queue_view.intake_items("org/.github", "org") == []
        assert queue_view.planning_items("org/.github", "org") == []
    ensure.assert_not_called()
    create.assert_not_called()
    run.assert_not_called()


# -- planning_items --------------------------------------------------------------


def _planning(
    views: dict[int, dict[str, Any]], states: dict[int, str] | None = None
) -> list[QueueItem]:
    children = [ChildState(IssueRef("org", ".github", n), "OPEN", "") for n in views]
    gh = _fake_gh(views, states)
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC) as find,
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=gh),
    ):
        items = queue_view.planning_items("org/.github", "org")
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
    ):
        items = queue_view.planning_items("org/.github", "org")
    assert [i.title for i in items] == ["old", "new"]
    assert sorted(int(c[2]) for c in gh.calls) == [6, 7]


def test_planning_items_missing_queue_epic_raises() -> None:
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=None),
        pytest.raises(queue_view.QueueNotFoundError, match=r"Epic \(planning\): org"),
    ):
        queue_view.planning_items("org/.github", "org")


def test_issue_view_rejects_non_object_response() -> None:
    children = [ChildState(IssueRef("org", ".github", 5), "OPEN", "")]
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", return_value=[]),
        pytest.raises(ValueError, match=r"org/\.github#5"),
    ):
        queue_view.intake_items("org/.github", "org")


def test_docs_task_state_rejects_non_object_response() -> None:
    view = _view("p", "2026-10-01T00:00:00Z", labels=("in-progress",), comments=_ALL_MARKERS)
    children = [ChildState(IssueRef("org", ".github", 5), "OPEN", "")]
    responses: list[object] = [view, []]
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=responses),
        pytest.raises(ValueError, match=r"org/\.github#51"),
    ):
        queue_view.planning_items("org/.github", "org")


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
