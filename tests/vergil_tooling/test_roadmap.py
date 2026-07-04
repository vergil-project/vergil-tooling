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
        result = roadmap.gather("vergil-project")
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


def test_gather_skips_adhoc_epic() -> None:
    # The ad-hoc bucket is perpetual and excluded from the strategic roadmap,
    # just like the deprecated 'standing' alias (epic #85).
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u40",
        },
        {
            "number": 6,
            "title": "Epic (ad hoc): vergil-tooling",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "ad-hoc"}],  # ad-hoc -> skipped
            "url": "u6",
        },
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        result = roadmap.gather("vergil-project")
    assert [s.number for s in result] == [40]


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
        result = roadmap.gather("vergil-project")
    assert result[0].milestone is None
    assert result[0].repos == ()


def test_gather_skips_release_tracking_issue() -> None:
    # Defense in depth: a release tracking issue is never epic-labelled, but if a
    # stray ``epic`` label ever lands on one it must not leak into the roadmap.
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u40",
        },
        {
            "number": 373,
            "title": "release: 2.1.4",
            "createdAt": "2026-06-30T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u373",
            "body": "<!-- vrg-release:progress -->\n- [x] tag\n",
        },
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        result = roadmap.gather("vergil-project")
    assert [e.number for e in result] == [40]


def test_gather_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"unexpected": True}):
        assert roadmap.gather("vergil-project") == []


def test_render_empty() -> None:
    assert "No active epics" in roadmap.render([])


def test_render_groups_by_milestone() -> None:
    summaries = [
        EpicSummary(
            40,
            "Convention | edge",
            "2026-06-28",
            "v2.2",
            ("vergil-project/.github", "vergil-project/vergil-tooling"),
            2,
            1,
            "u40",
        ),
        EpicSummary(7, "Orphan", "2026-02-02", None, (), 0, 0, "u7"),
    ]
    out = roadmap.render(summaries, "vergil-project")
    assert "## v2.2" in out
    assert "## No milestone" in out
    # Caption names the org the epics were read from.
    assert "open epics in vergil-project/.github" in out
    # Table scaffolding, one header per milestone section.
    assert "| Epic | Done | Repos | Created |" in out
    assert out.count("| --- | --- | --- | --- |") == 2
    # Epic row: link + title (pipe escaped), Done as X/Y, repos stacked short
    # names via <br>, and the created date now has its own visible column.
    assert (
        "| [#40](u40) Convention \\| edge | 1/2 | .github<br>vergil-tooling | 2026-06-28 |" in out
    )
    # No repos renders an em dash in the cell.
    assert "| [#7](u7) Orphan | 0/0 | — | 2026-02-02 |" in out


def test_render_without_org_uses_generic_source() -> None:
    summaries = [EpicSummary(7, "Orphan", "2026-02-02", None, (), 0, 0, "u7")]
    out = roadmap.render(summaries)
    assert "open epics in the org .github repo" in out


def test_gather_defaults_org_to_current_repo_owner() -> None:
    epic_list = [
        {
            "number": 9,
            "title": "Y",
            "createdAt": "2026-03-03T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u9",
        }
    ]
    with (
        patch("vergil_tooling.lib.github.current_org", return_value="logical-minds-foundry"),
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list) as mock_read,
        patch("vergil_tooling.lib.epics.child_states", return_value=[]) as mock_children,
    ):
        result = roadmap.gather()
    assert result[0].number == 9
    # The derived org drives both the epic query and the child rollup.
    assert "logical-minds-foundry/.github" in mock_read.call_args.args
    assert mock_children.call_args.args[0].owner == "logical-minds-foundry"
