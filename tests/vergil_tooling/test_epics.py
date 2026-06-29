"""Tests for vergil_tooling.lib.epics (umbrella relationship)."""

from __future__ import annotations

from unittest.mock import patch

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
