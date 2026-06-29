"""Tests for vergil_tooling.lib.activity_log."""

from __future__ import annotations

from unittest.mock import patch

from vergil_tooling.lib import activity_log
from vergil_tooling.lib.activity_log import ActivityItem


def test_gather_parses_and_skips_incomplete() -> None:
    results = [
        {
            "number": 1912,
            "title": "labels",
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u1912",
            "closedAt": "2026-06-29T10:00:00Z",
        },
        {  # missing repository -> skipped
            "number": 99,
            "title": "orphan",
            "repository": {},
            "url": "u99",
            "closedAt": "2026-06-29T11:00:00Z",
        },
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=results) as mock_search:
        items = activity_log.gather("2026-06-01")
    assert items == [
        ActivityItem("vergil-project/vergil-tooling", 1912, "labels", "u1912", "2026-06-29")
    ]
    # the search carries org scope (--owner) and the since cutoff (--closed)
    args = mock_search.call_args.args
    assert "vergil-project" in args
    assert ">=2026-06-01" in args


def test_gather_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"x": 1}):
        assert activity_log.gather("2026-06-01") == []


def test_render_empty() -> None:
    assert "No recently closed issues" in activity_log.render([])


def test_render_groups_by_date_descending() -> None:
    items = [
        ActivityItem("vergil-project/vergil-tooling", 1912, "labels", "u1912", "2026-06-28"),
        ActivityItem("vergil-project/vergil-tooling", 1926, "umbrella", "u1926", "2026-06-29"),
        ActivityItem("vergil-project/.github", 41, "docs", "u41", "2026-06-29"),
    ]
    out = activity_log.render(items)
    assert "3 issue(s) closed" in out
    # most recent date first
    assert out.index("## 2026-06-29") < out.index("## 2026-06-28")
    assert "[vergil-project/.github#41](u41) docs" in out
    assert "[vergil-project/vergil-tooling#1912](u1912) labels" in out
