"""Tests for vergil_tooling.lib.epic_audit."""

from __future__ import annotations

from unittest.mock import patch

from vergil_tooling.lib import epic_audit, roadmap
from vergil_tooling.lib.epic_audit import TaskDrift


def test_task_drift_flags_open_task_behind_merged_pr() -> None:
    prs = [
        {
            "number": 1948,
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u1948",
            "body": "Ref #1947",
        },
        {  # task closed -> not drift
            "number": 100,
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u100",
            "body": "Ref #99",
        },
        {  # no ref -> skipped
            "number": 101,
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u101",
            "body": "no linkage",
        },
        {"number": 102, "repository": {}, "url": "u102", "body": "Ref #5"},  # no repo -> skipped
        {  # multiple refs -> skipped
            "number": 103,
            "repository": {"nameWithOwner": "o/r"},
            "url": "u103",
            "body": "Ref #1\nRef #2",
        },
    ]

    def fake_state(*args: str) -> str:
        return "open" if args[2] == "1947" else "closed"

    with (
        patch("vergil_tooling.lib.github.read_json", return_value=prs),
        patch("vergil_tooling.lib.github.read_output", side_effect=fake_state),
    ):
        result = epic_audit.task_drift("2026-06-01")
    assert result == [TaskDrift("vergil-project/vergil-tooling", 1947, 1948, "u1948")]


def test_task_drift_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"x": 1}):
        assert epic_audit.task_drift("2026-06-01") == []


def test_epic_drift_flags_all_done_open_epics() -> None:
    summaries = [
        roadmap.EpicSummary(40, "Convention", "2026-06-28", None, (), 9, 9, "u40"),  # all done
        roadmap.EpicSummary(7, "WIP", "2026-06-01", None, (), 3, 1, "u7"),  # not all done
        roadmap.EpicSummary(8, "Empty", "2026-06-01", None, (), 0, 0, "u8"),  # no children
    ]
    with patch("vergil_tooling.lib.epic_audit.roadmap.gather", return_value=summaries):
        result = epic_audit.epic_drift()
    assert [e.number for e in result] == [40]


def test_render_clean() -> None:
    assert "No drift" in epic_audit.render([], [])


def test_render_task_drift_only() -> None:
    out = epic_audit.render([TaskDrift("o/r", 1947, 1948, "u1948")], [])
    assert "o/r#1947 — open; PR [#1948](u1948) merged" in out
    assert "## Epic drift" in out
    assert out.count("_none_") == 1  # epic section is empty


def test_render_epic_drift_only() -> None:
    epic = roadmap.EpicSummary(40, "Convention", "2026-06-28", None, (), 9, 9, "u40")
    out = epic_audit.render([], [epic])
    assert "[#40](u40) Convention — 9/9 done" in out
    assert "## Task drift" in out
    assert out.count("_none_") == 1  # task section is empty
