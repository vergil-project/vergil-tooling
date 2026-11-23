"""Tests for vergil_tooling.lib.roadmap."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import epics, roadmap
from vergil_tooling.lib.roadmap import EpicSummary

if TYPE_CHECKING:
    from collections.abc import Callable


def _child(owner: str, repo: str, number: int, state: str) -> epics.ChildState:
    return epics.ChildState(epics.IssueRef(owner, repo, number), state)


def test_gather_summarizes_open_epics_and_skips_adhoc() -> None:
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
            "labels": [{"name": "epic"}, {"name": "ad-hoc"}],  # ad-hoc -> skipped
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
    # The ad-hoc bucket is perpetual and excluded from the strategic roadmap.
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


def test_roadmap_skips_intake_and_planning_epics() -> None:
    # Intake and planning are perpetual standing queues, not strategic epics.
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
            "number": 7,
            "title": "Epic (intake): vergil-project",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "intake"}],
            "url": "u7",
        },
        {
            "number": 8,
            "title": "Epic (planning): vergil-project",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "planning"}],
            "url": "u8",
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
    layer = trust_stubs.FakeItems(
        {
            ("vergil-project/.github", 373): trust_stubs.item(
                body="<!-- vrg-release:progress -->\n- [x] tag\n"
            )
        },
        strict=True,
    )
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
        patch("vergil_tooling.lib.github.graphql", side_effect=layer),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        result = roadmap.gather("vergil-project")
    assert [e.number for e in result] == [40]
    assert layer.reads == [("vergil-project/.github", 373)]


def test_gather_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"unexpected": True}):
        assert roadmap.gather("vergil-project") == []


def _limited(rows: list[dict[str, object]]) -> Callable[..., list[dict[str, object]]]:
    """A ``gh issue list`` stand-in that returns at most ``--limit`` rows."""

    def fake(*args: str) -> list[dict[str, object]]:
        return rows[: int(args[args.index("--limit") + 1])]

    return fake


def _epics(count: int) -> list[dict[str, object]]:
    return [{"number": i, "title": f"Epic {i}", "labels": []} for i in range(1, count + 1)]


def test_open_epics_sees_an_epic_past_row_200() -> None:
    with patch("vergil_tooling.lib.github.read_json", side_effect=_limited(_epics(201))):
        assert len(roadmap._open_epics("org/.github")) == 201


def test_open_epics_refuses_a_list_that_may_be_truncated() -> None:
    with (
        patch(
            "vergil_tooling.lib.github.read_json",
            side_effect=_limited(_epics(epics.LISTING_LIMIT)),
        ),
        pytest.raises(epics.EpicListTruncatedError, match="open epics of org/.github"),
    ):
        roadmap._open_epics("org/.github")


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


# Hostile titles: tag-like, script-like, link-like and a pipe.
_HOSTILE_TITLES = (
    ("<b>bold</b>", "&lt;b&gt;bold&lt;/b&gt;"),
    ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
    ("[x](javascript:alert(1))", "\\[x\\](javascript:alert(1))"),
    ("a|b", "a\\|b"),
)


def test_roadmap_row_escapes_title() -> None:
    for title, escaped in _HOSTILE_TITLES:
        row = roadmap._row(EpicSummary(9, title, "2026-06-28", None, (), 0, 0, "u9"))
        prefix = "| [#9](u9) "
        assert row == f"{prefix}{escaped} | 0/0 | — | 2026-06-28 |"
        assert "<" not in row
        # The pipe is escaped exactly once.
        assert "\\\\|" not in row


def test_render_escapes_milestone_title() -> None:
    summary = EpicSummary(9, "t", "2026-06-28", "<i>v3</i> | *x*", (), 0, 0, "u9")
    out = roadmap.render([summary], "vergil-project")
    assert "## &lt;i&gt;v3&lt;/i&gt; \\| \\*x\\*" in out
    assert "<i>" not in out


def test_gather_excludes_proxy() -> None:
    # A proxy is not a development epic of its own (#1417): the roadmap lists
    # its primary, never the proxy.
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
            "number": 9,
            "title": "Epic (proxy): keep ad-hoc queues standalone",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "proxy"}],
            "url": "u9",
        },
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]) as mock_children,
    ):
        result = roadmap.gather("vergil-project")
    assert [s.number for s in result] == [40]
    mock_children.assert_called_once_with(epics.IssueRef("vergil-project", ".github", 40))
