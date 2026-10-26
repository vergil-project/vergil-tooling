"""Tests for vergil_tooling.lib.queue_view (the read-only intake/planning/deferred views)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import epics, queue_view, security_records
from vergil_tooling.lib.epics import ChildState, IssueRef
from vergil_tooling.lib.queue_view import Deferral, PlanningMarkers, QueueItem
from vergil_tooling.lib.security_records import Advisory

_MOD = "vergil_tooling.lib.queue_view"
_EPIC = IssueRef("org", ".github", 1)


def _view(
    title: str,
    created_at: str,
    *,
    labels: tuple[str, ...] = (),
    comments: tuple[str, ...] = (),
    body: str = "",
) -> dict[str, Any]:
    return {
        "title": title,
        "createdAt": created_at,
        "labels": [{"name": name} for name in labels],
        "comments": [{"body": comment} for comment in comments],
        "body": body,
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
        assert queue_view.deferred_items("org/.github", "org") == []
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
        items = queue_view.deferred_items("org/.github", "org")
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
        queue_view.deferred_items("org/.github", "org")


# -- planning: the security-advisory hold ------------------------------------------

_GHSA = "GHSA-abcd-efgh-ijkl"
_OTHER_GHSA = "GHSA-mnop-qrst-uvwx"
_ADVISORY_BODY = f"Neutral planning body.\n\nSecurity advisory: org/.github {_GHSA}"
_PLAN = IssueRef("org", ".github", 5)
_FIX = IssueRef("org", "tool", 60)
_HELD_NOTE = (
    f"held: advisory {_GHSA} still tracks this planning issue — retarget it "
    "to the fix task(s) with vrg-security-record retarget"
)


def _advisory(ghsa_id: str, *tracking: IssueRef) -> Advisory:
    return Advisory(
        ghsa_id=ghsa_id, state="draft", affected="tool", tracking=tracking, external=False
    )


def _ready_view(body: str = _ADVISORY_BODY, *, labels: tuple[str, ...] = ("in-progress",)) -> Any:
    return _view("plan x", "2026-10-01T00:00:00Z", labels=labels, comments=_ALL_MARKERS, body=body)


def _security_planning(
    views: dict[int, dict[str, Any]],
    advisories: list[Advisory],
    states: dict[int, str] | None = None,
) -> tuple[list[QueueItem], Any, Any]:
    with (
        patch(f"{_MOD}.security_records.advisory_home", return_value="org/.github") as home,
        patch(f"{_MOD}.security_records.list_advisories", return_value=advisories) as listing,
    ):
        items = _planning(views, states if states is not None else {51: "CLOSED"})
    return items, home, listing


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


def test_planning_state_not_held_when_named_advisory_is_absent() -> None:
    # The pair is broken (the sweep reports it); this view does not hold the item.
    [item], _home, _listing = _security_planning(
        {5: _ready_view()}, [_advisory(_OTHER_GHSA, _PLAN)]
    )
    assert item.state == "ready-to-close"


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


def test_non_security_planning_item_unchanged() -> None:
    views = {5: _ready_view(), 8: _ready_view(body="An ordinary planning issue.")}
    advisories = [_advisory(_GHSA, _PLAN, IssueRef("org", ".github", 8))]
    items, _home, _listing = _security_planning(views, advisories)
    by_number = {i.ref.number: i for i in items}
    assert by_number[5].state == "in-progress"
    # #8 has no advisory line, so a tracking line naming it does not hold it.
    assert by_number[8].state == "ready-to-close"
    assert by_number[8].note == "Docs task: org/.github#51"


def test_no_advisory_lines_never_resolves_home() -> None:
    views = {5: _ready_view(body="Mentions Security advisory: org/.github GHSA-x inline.")}
    items, home, listing = _security_planning(views, [])
    assert [i.state for i in items] == ["ready-to-close"]
    home.assert_not_called()
    listing.assert_not_called()


def test_planning_private_advisory_home_is_refused_loudly() -> None:
    children = [ChildState(_PLAN, "OPEN", "")]
    with (
        patch(f"{_MOD}.epics.find_standing_epic", return_value=_EPIC),
        patch(f"{_MOD}.epics.child_states", return_value=children),
        patch(f"{_MOD}.github.read_json", side_effect=_fake_gh({5: _ready_view()})),
        patch(
            f"{_MOD}.security_records.advisory_home",
            side_effect=security_records.PrivateHomeError("org/.github is not public"),
        ),
        pytest.raises(security_records.PrivateHomeError, match="not public"),
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
