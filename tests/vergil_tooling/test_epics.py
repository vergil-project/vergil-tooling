"""Tests for vergil_tooling.lib.epics (umbrella relationship)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib import epics
from vergil_tooling.lib.epics import ChildState, IssueRef

EPIC = IssueRef("org", ".github", 40)
TASK = IssueRef("org", "repo-a", 101)


def _repo_node(login: str, name: str) -> dict[str, object]:
    return {"name": name, "owner": {"login": login}}


# -- child_states ------------------------------------------------------------


def test_child_states_native() -> None:
    data = {
        "node": {
            "subIssues": {
                "nodes": [
                    {"number": 101, "state": "CLOSED", "repository": _repo_node("org", "repo-a")},
                    {"number": 102, "state": "OPEN", "repository": _repo_node("org", "repo-b")},
                ]
            }
        }
    }
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=data),
    ):
        result = epics.child_states(EPIC)
    assert result == [
        ChildState(IssueRef("org", "repo-a", 101), "CLOSED"),
        ChildState(IssueRef("org", "repo-b", 102), "OPEN"),
    ]


def test_child_states_reflink_fallback_when_native_empty() -> None:
    empty = {"node": {"subIssues": {"nodes": []}}}
    search = [{"number": 41, "state": "OPEN", "repository": {"nameWithOwner": "org/.github"}}]
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=empty),
        patch("vergil_tooling.lib.github.read_json", return_value=search) as mock_search,
    ):
        result = epics.child_states(EPIC)
    assert result == [ChildState(IssueRef("org", ".github", 41), "OPEN")]
    # the fallback searches for the epic's Parent: marker
    assert "Parent: org/.github#40" in mock_search.call_args.args


# -- parent_of ---------------------------------------------------------------


def test_parent_of_native() -> None:
    data = {"node": {"parent": {"number": 40, "repository": _repo_node("org", ".github")}}}
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=data),
    ):
        assert epics.parent_of(TASK) == IssueRef("org", ".github", 40)


def test_parent_of_reflink_fallback_parses_body() -> None:
    no_parent = {"node": {"parent": None}}
    body = "Some description.\n\nParent: org/.github#40\n"
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=no_parent),
        patch("vergil_tooling.lib.github.read_output", return_value=body),
    ):
        assert epics.parent_of(TASK) == IssueRef("org", ".github", 40)


def test_parent_of_none_when_unlinked() -> None:
    no_parent = {"node": {"parent": None}}
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=no_parent),
        patch("vergil_tooling.lib.github.read_output", return_value="no marker here"),
    ):
        assert epics.parent_of(TASK) is None


# -- add_child (reopen-on-late-child) ----------------------------------------


def test_add_child_reopens_closed_epic_before_linking() -> None:
    with (
        patch("vergil_tooling.lib.epics._issue_state", return_value="CLOSED"),
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.run") as mock_run,
        patch("vergil_tooling.lib.github.graphql") as mock_graphql,
    ):
        epics.add_child(EPIC, TASK)
    mock_run.assert_called_once()
    assert mock_run.call_args.args[:2] == ("issue", "reopen")
    assert "40" in mock_run.call_args.args
    mock_graphql.assert_called_once()


def test_add_child_open_epic_is_not_reopened() -> None:
    with (
        patch("vergil_tooling.lib.epics._issue_state", return_value="OPEN"),
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.run") as mock_run,
        patch("vergil_tooling.lib.github.graphql") as mock_graphql,
    ):
        epics.add_child(EPIC, TASK)
    mock_run.assert_not_called()
    mock_graphql.assert_called_once()


# -- all_children_closed -----------------------------------------------------


def test_all_children_closed_true_when_all_closed() -> None:
    children = [
        ChildState(IssueRef("o", "r", 1), "CLOSED"),
        ChildState(IssueRef("o", "r", 2), "CLOSED"),
    ]
    with patch("vergil_tooling.lib.epics.child_states", return_value=children):
        assert epics.all_children_closed(EPIC) is True


def test_all_children_closed_false_with_an_open_child() -> None:
    children = [
        ChildState(IssueRef("o", "r", 1), "CLOSED"),
        ChildState(IssueRef("o", "r", 2), "OPEN"),
    ]
    with patch("vergil_tooling.lib.epics.child_states", return_value=children):
        assert epics.all_children_closed(EPIC) is False


def test_all_children_closed_false_when_no_children() -> None:
    with patch("vergil_tooling.lib.epics.child_states", return_value=[]):
        assert epics.all_children_closed(EPIC) is False


# -- helpers (node id / state / malformed reflink) ---------------------------


def test_node_id_resolves_via_rest() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="I_node123") as mock_read:
        assert epics._node_id(TASK) == "I_node123"
    assert mock_read.call_args.args == ("api", "repos/org/repo-a/issues/101", "--jq", ".node_id")


def test_issue_state_uppercases() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="closed"):
        assert epics._issue_state(EPIC) == "CLOSED"


def test_reflink_skips_results_without_repo() -> None:
    search = [
        {"number": 41, "state": "OPEN", "repository": {"nameWithOwner": "org/.github"}},
        {"number": 99, "state": "OPEN", "repository": {}},  # malformed -> skipped
    ]
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch(
            "vergil_tooling.lib.github.graphql",
            return_value={"node": {"subIssues": {"nodes": []}}},
        ),
        patch("vergil_tooling.lib.github.read_json", return_value=search),
    ):
        result = epics.child_states(EPIC)
    assert result == [ChildState(IssueRef("org", ".github", 41), "OPEN")]


# -- is_epic / rollup --------------------------------------------------------


def test_is_epic_true_when_labeled() -> None:
    labels = {"labels": [{"name": "epic"}, {"name": "enhancement"}]}
    with patch("vergil_tooling.lib.github.read_json", return_value=labels):
        assert epics.is_epic(EPIC) is True


def test_is_epic_false_without_label() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"labels": [{"name": "bug"}]}):
        assert epics.is_epic(TASK) is False


def test_is_epic_linkage_true_for_epic() -> None:
    with patch("vergil_tooling.lib.epics.is_epic", return_value=True) as mock:
        assert epics.is_epic_linkage("org/.github#40", default_repo="org/repo") is True
    mock.assert_called_once_with(IssueRef("org", ".github", 40))


def test_is_epic_linkage_false_for_task() -> None:
    with patch("vergil_tooling.lib.epics.is_epic", return_value=False):
        assert epics.is_epic_linkage("#42", default_repo="org/repo") is False


def test_is_epic_linkage_false_for_unparseable_ref() -> None:
    # No resolvable default repo -> parse fails -> never an epic (is_epic unused).
    with patch("vergil_tooling.lib.epics.is_epic") as mock:
        assert epics.is_epic_linkage("#42", default_repo="") is False
    mock.assert_not_called()


def test_rollup_closes_finite_epic_when_all_children_closed() -> None:
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=EPIC),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic"}),
        patch("vergil_tooling.lib.epics.all_children_closed", return_value=True),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(TASK)
    mock_run.assert_called_once()
    assert mock_run.call_args.args[:2] == ("issue", "close")
    assert "40" in mock_run.call_args.args


def test_rollup_skips_standing_epic() -> None:
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=EPIC),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "standing"}),
        patch("vergil_tooling.lib.epics.all_children_closed", return_value=True),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(TASK)
    mock_run.assert_not_called()


def test_rollup_skips_when_children_remain_open() -> None:
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=EPIC),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic"}),
        patch("vergil_tooling.lib.epics.all_children_closed", return_value=False),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(TASK)
    mock_run.assert_not_called()


def test_rollup_noop_for_unmanaged_task() -> None:
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=None),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(TASK)
    mock_run.assert_not_called()


def test_rollup_noop_when_parent_not_epic() -> None:
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=EPIC),
        patch("vergil_tooling.lib.epics.is_epic", return_value=False),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(TASK)
    mock_run.assert_not_called()


# -- parse_issue_ref ---------------------------------------------------------


def test_parse_issue_ref_bare_uses_default_repo() -> None:
    assert epics.parse_issue_ref("#42", default_repo="org/repo") == IssueRef("org", "repo", 42)


def test_parse_issue_ref_cross_repo() -> None:
    ref = epics.parse_issue_ref("org/.github#40", default_repo="x/y")
    assert ref == IssueRef("org", ".github", 40)


def test_parse_issue_ref_malformed_raises() -> None:
    with pytest.raises(ValueError, match="not an issue ref"):
        epics.parse_issue_ref("not-a-ref", default_repo="org/repo")


def test_parse_issue_ref_no_repo_raises() -> None:
    with pytest.raises(ValueError, match="cannot resolve repo"):
        epics.parse_issue_ref("#42", default_repo="")


# -- remove_child ------------------------------------------------------------


def test_remove_child_issues_removesubissue_mutation() -> None:
    with (
        patch("vergil_tooling.lib.epics._node_id", side_effect=["EPIC_ID", "TASK_ID"]),
        patch("vergil_tooling.lib.github.graphql") as mock_graphql,
    ):
        epics.remove_child(EPIC, TASK)
    mock_graphql.assert_called_once()
    assert "removeSubIssue" in mock_graphql.call_args.args[0]
    assert mock_graphql.call_args.kwargs == {"parent": "EPIC_ID", "child": "TASK_ID"}


# -- resolve_epic_ref --------------------------------------------------------


def test_resolve_epic_ref_standing_discovers_single_epic() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=[{"number": 1972}]) as mock_list:
        assert epics.resolve_epic_ref("standing", repo="org/tooling") == IssueRef(
            "org", "tooling", 1972
        )
    # discovery filters open issues carrying both the epic and standing labels
    args = mock_list.call_args.args
    assert "epic" in args and "standing" in args


def test_resolve_epic_ref_standing_zero_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        pytest.raises(ValueError, match="no standing epic"),
    ):
        epics.resolve_epic_ref("standing", repo="org/tooling")


def test_resolve_epic_ref_standing_multiple_raises() -> None:
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=[{"number": 1}, {"number": 2}]),
        pytest.raises(ValueError, match="multiple standing epics"),
    ):
        epics.resolve_epic_ref("standing", repo="org/tooling")


def test_resolve_epic_ref_explicit_validates_epic() -> None:
    with patch("vergil_tooling.lib.epics.is_epic", return_value=True):
        assert epics.resolve_epic_ref("org/.github#40", repo="org/repo") == IssueRef(
            "org", ".github", 40
        )


def test_resolve_epic_ref_explicit_non_epic_raises() -> None:
    with (
        patch("vergil_tooling.lib.epics.is_epic", return_value=False),
        pytest.raises(ValueError, match="not an epic"),
    ):
        epics.resolve_epic_ref("#123", repo="org/repo")


def test_resolve_epic_ref_standing_repo_without_owner_raises() -> None:
    with pytest.raises(ValueError, match="cannot resolve repo"):
        epics.resolve_epic_ref("standing", repo="tooling")
