"""Tests for vergil_tooling.lib.roadmap."""

from __future__ import annotations

from unittest.mock import patch

from vergil_tooling.lib import epics, roadmap
from vergil_tooling.lib.roadmap import EpicSummary


def _child(owner: str, repo: str, number: int, state: str) -> epics.ChildState:
    return epics.ChildState(epics.IssueRef(owner, repo, number), state)


def test_gather_summarizes_open_epics_and_skips_standing() -> None:
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": {"title": "v2.2"},
            "labels": [{"name": "epic"}],
            "url": "https://github.com/vergil-project/.github/issues/40",
        },
        {
            "number": 5,
            "title": "Ad-hoc",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "standing"}],  # standing -> skipped
            "url": "u5",
        },
    ]
    children = [
        _child("vergil-project", "vergil-tooling", 1912, "CLOSED"),
        _child("vergil-project", "vergil-claude-plugin", 524, "OPEN"),
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
    ):
        result = roadmap.gather()
    assert len(result) == 1
    summary = result[0]
    assert summary.number == 40
    assert summary.created == "2026-06-28"
    assert summary.milestone == "v2.2"
    assert summary.repos == (
        "vergil-project/vergil-claude-plugin",
        "vergil-project/vergil-tooling",
    )
    assert (summary.total, summary.closed) == (2, 1)


def test_gather_handles_no_milestone_and_no_children() -> None:
    epic_list = [
        {
            "number": 7,
            "title": "X",
            "createdAt": "2026-02-02T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u7",
        }
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        result = roadmap.gather()
    assert result[0].milestone is None
    assert result[0].repos == ()


def test_gather_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"unexpected": True}):
        assert roadmap.gather() == []


def test_render_empty() -> None:
    assert "No active epics" in roadmap.render([])


def test_render_groups_by_milestone() -> None:
    summaries = [
        EpicSummary(
            40, "Convention", "2026-06-28", "v2.2", ("vergil-project/vergil-tooling",), 2, 1, "u40"
        ),
        EpicSummary(7, "Orphan", "2026-02-02", None, (), 0, 0, "u7"),
    ]
    out = roadmap.render(summaries)
    assert "## v2.2" in out
    assert "## No milestone" in out
    assert "[#40](u40) Convention" in out
    assert "1/2 done" in out
    assert "repos: vergil-project/vergil-tooling" in out
    assert "repos: —" in out
