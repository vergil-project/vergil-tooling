"""Tests for vergil_tooling.lib.adhoc_migrate."""

from __future__ import annotations

from unittest.mock import patch

from vergil_tooling.lib import adhoc_migrate
from vergil_tooling.lib.adhoc_migrate import Relocation
from vergil_tooling.lib.epics import ChildState, IssueRef

_STANDING = IssueRef("org", "tooling", 100)


def test_find_standing_epics_returns_refs() -> None:
    issues = [
        {"number": 100, "repository": {"nameWithOwner": "org/tooling"}},
        {"number": 5, "repository": {"nameWithOwner": "org/.github"}},
        {"number": 9, "repository": {}},  # no repo -> skipped
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=issues) as mock_search:
        result = adhoc_migrate.find_standing_epics("org")
    assert result == [IssueRef("org", "tooling", 100), IssueRef("org", ".github", 5)]
    args = mock_search.call_args.args
    assert "search" in args and "standing" in args and "epic" in args


def test_find_standing_epics_non_list_is_empty() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"x": 1}):
        assert adhoc_migrate.find_standing_epics("org") == []


def test_plan_pairs_standing_with_open_children_only() -> None:
    children = [
        ChildState(IssueRef("org", "tooling", 101), "OPEN"),
        ChildState(IssueRef("org", "tooling", 102), "CLOSED"),  # excluded
    ]
    with (
        patch(
            "vergil_tooling.lib.adhoc_migrate.find_standing_epics",
            return_value=[_STANDING],
        ),
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
    ):
        result = adhoc_migrate.plan("org")
    assert result == [
        Relocation(standing=_STANDING, open_children=(IssueRef("org", "tooling", 101),))
    ]
    assert result[0].target_repo == "org/tooling"


def test_apply_one_reparents_open_children_and_closes() -> None:
    adhoc = IssueRef("org", ".github", 40)
    child = IssueRef("org", "tooling", 101)
    reloc = Relocation(standing=_STANDING, open_children=(child,))
    with (
        patch("vergil_tooling.lib.epics.ensure_adhoc_epic", return_value=adhoc) as mock_ensure,
        patch("vergil_tooling.lib.epics.remove_child") as mock_remove,
        patch("vergil_tooling.lib.epics.add_child") as mock_add,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        summary = adhoc_migrate.apply_one(reloc)
    mock_ensure.assert_called_once_with("org/tooling")
    mock_remove.assert_called_once_with(_STANDING, child)
    mock_add.assert_called_once_with(adhoc, child)
    assert mock_run.call_args.args[:5] == ("issue", "close", "100", "--repo", "org/tooling")
    assert "org/.github#40" in mock_run.call_args.args[-1]
    assert summary == "org/tooling#100 → org/.github#40 (1 open child(ren) moved)"


def test_apply_one_no_children_still_closes() -> None:
    adhoc = IssueRef("org", ".github", 40)
    reloc = Relocation(standing=_STANDING, open_children=())
    with (
        patch("vergil_tooling.lib.epics.ensure_adhoc_epic", return_value=adhoc),
        patch("vergil_tooling.lib.epics.remove_child") as mock_remove,
        patch("vergil_tooling.lib.epics.add_child") as mock_add,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        summary = adhoc_migrate.apply_one(reloc)
    mock_remove.assert_not_called()
    mock_add.assert_not_called()
    mock_run.assert_called_once()
    assert "0 open child(ren) moved" in summary


def test_render_plan_empty() -> None:
    out = adhoc_migrate.render_plan([], org="org")
    assert "dry run" in out
    assert "nothing to migrate" in out


def test_render_plan_lists_relocations_and_children() -> None:
    reloc = Relocation(
        standing=IssueRef("org", "tooling", 100),
        open_children=(IssueRef("org", "tooling", 101),),
    )
    out = adhoc_migrate.render_plan([reloc], org="org")
    assert "Planned relocations" in out
    assert "org/tooling#100" in out
    assert "Epic (ad hoc): tooling" in out
    assert "org/tooling#101" in out


def test_render_applied_empty() -> None:
    assert "nothing migrated" in adhoc_migrate.render_applied([], org="org")


def test_render_applied_lists_summaries() -> None:
    out = adhoc_migrate.render_applied(
        ["org/tooling#100 → org/.github#40 (1 open child(ren) moved)"], org="org"
    )
    assert "Relocated" in out
    assert "org/tooling#100 → org/.github#40" in out
