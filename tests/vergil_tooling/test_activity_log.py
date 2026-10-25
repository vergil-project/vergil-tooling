"""Tests for vergil_tooling.lib.activity_log."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.lib import activity_log
from vergil_tooling.lib.activity_log import ActivityItem

if TYPE_CHECKING:
    import pytest


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
        items = activity_log.gather("2026-06-01", org="vergil-project")
    assert items == [
        ActivityItem("vergil-project/vergil-tooling", 1912, "labels", "u1912", "2026-06-29")
    ]
    # the search carries org scope (--owner) and the since cutoff (--closed)
    args = mock_search.call_args.args
    assert "vergil-project" in args
    assert ">=2026-06-01" in args


def test_gather_skips_release_tracking_issues() -> None:
    # A closed ``release: X.Y.Z`` issue is vrg-release bookkeeping, not work —
    # keep it out of the ledger, whether detected by body marker or by title.
    results = [
        {
            "number": 1912,
            "title": "labels",
            "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
            "url": "u1912",
            "closedAt": "2026-06-29T10:00:00Z",
            "body": "real work",
        },
        {  # release issue by body marker -> skipped
            "number": 373,
            "title": "release: 2.1.4",
            "repository": {"nameWithOwner": "vergil-project/vergil-containers"},
            "url": "u373",
            "closedAt": "2026-06-30T09:00:00Z",
            "body": "<!-- vrg-release:progress -->\n- [x] tag\n",
        },
        {  # release issue by title only (body marker hand-removed) -> skipped
            "number": 400,
            "title": "release: 2.1.5",
            "repository": {"nameWithOwner": "vergil-project/vergil-containers"},
            "url": "u400",
            "closedAt": "2026-06-30T10:00:00Z",
            "body": "",
        },
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=results):
        items = activity_log.gather("2026-06-01", org="vergil-project")
    assert items == [
        ActivityItem("vergil-project/vergil-tooling", 1912, "labels", "u1912", "2026-06-29")
    ]


def test_gather_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"x": 1}):
        assert activity_log.gather("2026-06-01", org="vergil-project") == []


def test_gather_defaults_org_to_current_repo_owner() -> None:
    with (
        patch("vergil_tooling.lib.github.current_org", return_value="acme") as mock_org,
        patch("vergil_tooling.lib.github.read_json", return_value=[]) as mock_search,
    ):
        assert activity_log.gather("2026-06-01") == []
    mock_org.assert_called_once()
    assert "acme" in mock_search.call_args.args


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


def test_render_takes_a_title() -> None:
    items = [ActivityItem("vergil-project/vergil-tooling", 1, "t", "u1", "2026-06-28")]
    assert activity_log.render(items, title="Activity log: vergil-tooling").startswith(
        "# Activity log: vergil-tooling\n"
    )
    assert activity_log.render([], title="X").startswith("# X\n")


def test_gather_repo_scopes_search_to_one_repo() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=[]) as mock_search:
        activity_log.gather("2026-06-01", org="vergil-project", repo="vergil-tooling")
    args = mock_search.call_args.args
    assert args[args.index("--repo") + 1] == "vergil-project/vergil-tooling"
    assert "--owner" not in args


def test_gather_repo_accepts_owner_name() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=[]) as mock_search:
        activity_log.gather("2026-06-01", org="vergil-project", repo="acme/widgets")
    args = mock_search.call_args.args
    assert args[args.index("--repo") + 1] == "acme/widgets"


def test_gather_warns_when_search_hits_its_cap(capsys: pytest.CaptureFixture[str]) -> None:
    entry = {
        "number": 1,
        "title": "t",
        "repository": {"nameWithOwner": "vergil-project/vergil-tooling"},
        "url": "u",
        "closedAt": "2026-06-29T10:00:00Z",
    }
    with patch("vergil_tooling.lib.github.read_json", return_value=[entry] * 1000) as mock_search:
        items = activity_log.gather("2026-06-01", org="vergil-project")
    assert len(items) == 1000
    assert "1000" in mock_search.call_args.args
    assert "cap" in capsys.readouterr().err


def test_gather_below_cap_is_silent(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=[]):
        activity_log.gather("2026-06-01", org="vergil-project")
    assert capsys.readouterr().err == ""


def test_repo_page_names() -> None:
    assert activity_log.repo_page("vergil-project/vergil-tooling") == "vergil-tooling.md"
    # MkDocs skips dot-files, so .github must not render as .github.md
    assert activity_log.repo_page("vergil-project/.github") == "dot-github.md"


_ITEMS = [
    ActivityItem("vergil-project/vergil-tooling", 1912, "labels", "u1912", "2026-06-28"),
    ActivityItem("vergil-project/vergil-tooling", 1926, "umbrella", "u1926", "2026-06-29"),
    ActivityItem("vergil-project/vergil-tooling", 1920, "other", "u1920", "2026-06-29"),
    ActivityItem("vergil-project/.github", 41, "docs", "u41", "2026-06-29"),
]


def test_render_summary_empty() -> None:
    assert "No recently closed issues" in activity_log.render_summary([])


def test_render_summary_table() -> None:
    out = activity_log.render_summary(_ITEMS)
    assert "4 issue(s) closed" in out
    rows = [line for line in out.splitlines() if line.startswith("|")]
    assert rows == [
        "| Day | Total | [.github](repos/dot-github.md)"
        " | [vergil-tooling](repos/vergil-tooling.md) |",
        "| --- | ---: | ---: | ---: |",
        "| [2026-06-29](days/2026-06-29.md) | 3 | 1 | 2 |",
        "| [2026-06-28](days/2026-06-28.md) | 1 |  | 1 |",
    ]


def test_render_day_groups_by_repo() -> None:
    day = [item for item in _ITEMS if item.closed_date == "2026-06-29"]
    out = activity_log.render_day("2026-06-29", day)
    assert out.startswith("# 2026-06-29\n")
    assert "3 issue(s) closed" in out
    assert "](../index.md)" in out
    assert out.index("## vergil-project/.github") < out.index("## vergil-project/vergil-tooling")
    # within a repo, issues are in number order
    assert out.index("#1920]") < out.index("#1926]")
    assert out.index("## vergil-project/vergil-tooling") < out.index("#1920]")


def test_render_pages_layout() -> None:
    pages = activity_log.render_pages(_ITEMS)
    assert sorted(pages) == [
        "days/2026-06-28.md",
        "days/2026-06-29.md",
        "index.md",
        "repos/dot-github.md",
        "repos/vergil-tooling.md",
    ]
    assert pages["index.md"] == activity_log.render_summary(_ITEMS)
    repo = pages["repos/vergil-tooling.md"]
    assert repo.startswith("# Activity log: vergil-tooling\n")
    assert "3 issue(s) closed" in repo
    assert "vergil-project/.github" not in repo


def test_render_pages_empty() -> None:
    assert list(activity_log.render_pages([])) == ["index.md"]
