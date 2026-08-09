"""Tests for vergil_tooling.lib.epics (umbrella relationship)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.epics import _SUBISSUES_QUERY, ChildState, IssueRef

EPIC = IssueRef("org", ".github", 40)
TASK = IssueRef("org", "repo-a", 101)


def _repo_node(login: str, name: str) -> dict[str, object]:
    return {"name": name, "owner": {"login": login}}


# -- single_target_org (issue #2070) -----------------------------------------


def test_single_target_org_returns_common_owner() -> None:
    owner = epics.single_target_org(
        IssueRef("org", ".github", 40),
        IssueRef("org", "repo-a", 101),
    )
    assert owner == "org"


def test_single_target_org_single_ref() -> None:
    assert epics.single_target_org(IssueRef("org", "repo-a", 101)) == "org"


def test_single_target_org_rejects_cross_org() -> None:
    with pytest.raises(ValueError, match="cross-org"):
        epics.single_target_org(
            IssueRef("org-a", ".github", 40),
            IssueRef("org-b", "repo", 101),
        )


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
    search = [
        {
            "number": 41,
            "state": "OPEN",
            "repository": {"nameWithOwner": "org/.github"},
            "body": "Parent: org/.github#40",
        }
    ]
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=empty),
        patch("vergil_tooling.lib.github.read_json", return_value=search) as mock_search,
    ):
        result = epics.child_states(EPIC)
    assert result == [ChildState(IssueRef("org", ".github", 41), "OPEN")]
    # the fallback searches for the epic's Parent: marker, scoped to the org
    assert "Parent: org/.github#40" in mock_search.call_args.args
    assert "--owner" in mock_search.call_args.args
    assert "org" in mock_search.call_args.args


def test_child_states_native_carries_title() -> None:
    # The native traversal requests and propagates each child's title (issue #2538).
    data = {
        "node": {
            "subIssues": {
                "nodes": [
                    {
                        "number": 101,
                        "state": "CLOSED",
                        "title": "First task",
                        "repository": _repo_node("org", "repo-a"),
                    },
                    {
                        "number": 102,
                        "state": "OPEN",
                        "title": "Second task",
                        "repository": _repo_node("org", "repo-b"),
                    },
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
        ChildState(IssueRef("org", "repo-a", 101), "CLOSED", "First task"),
        ChildState(IssueRef("org", "repo-b", 102), "OPEN", "Second task"),
    ]
    # The GraphQL query asks GitHub for the title field.
    assert "title" in _SUBISSUES_QUERY


def test_child_states_reflink_carries_title() -> None:
    # The portable Parent: reflink fallback also requests and propagates title.
    empty = {"node": {"subIssues": {"nodes": []}}}
    search = [
        {
            "number": 41,
            "state": "OPEN",
            "title": "Fallback task",
            "repository": {"nameWithOwner": "org/.github"},
            "body": "Parent: org/.github#40",
        }
    ]
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=empty),
        patch("vergil_tooling.lib.github.read_json", return_value=search) as mock_search,
    ):
        result = epics.child_states(EPIC)
    assert result == [ChildState(IssueRef("org", ".github", 41), "OPEN", "Fallback task")]
    # The search requests the title and closedAt fields so the listing is complete.
    assert "number,state,title,closedAt,repository,body" in mock_search.call_args.args


def test_child_states_native_includes_closed_at() -> None:
    # The native traversal propagates each child's closedAt (issue #2678); an open
    # child (closedAt null) yields "".
    data = {
        "node": {
            "subIssues": {
                "nodes": [
                    {
                        "number": 101,
                        "state": "CLOSED",
                        "title": "t",
                        "closedAt": "2026-08-01T10:00:00Z",
                        "repository": _repo_node("org", "repo-a"),
                    },
                    {
                        "number": 102,
                        "state": "OPEN",
                        "title": "u",
                        "closedAt": None,
                        "repository": _repo_node("org", "repo-b"),
                    },
                ]
            }
        }
    }
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=data),
    ):
        result = epics.child_states(EPIC)
    assert result[0].closed_at == "2026-08-01T10:00:00Z"
    assert result[1].closed_at == ""
    # The GraphQL query asks GitHub for the closedAt field.
    assert "closedAt" in _SUBISSUES_QUERY


def test_child_states_reflink_includes_closed_at() -> None:
    # The portable Parent: reflink fallback also propagates closedAt.
    empty = {"node": {"subIssues": {"nodes": []}}}
    search = [
        {
            "number": 41,
            "state": "CLOSED",
            "title": "t",
            "closedAt": "2026-05-10T00:00:00Z",
            "repository": {"nameWithOwner": "org/.github"},
            "body": "Parent: org/.github#40",
        }
    ]
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=empty),
        patch("vergil_tooling.lib.github.read_json", return_value=search),
    ):
        result = epics.child_states(EPIC)
    assert result[0].closed_at == "2026-05-10T00:00:00Z"


# -- quarter helpers (issue #2678) -------------------------------------------


@pytest.mark.parametrize(
    "iso,expected",
    [
        ("2026-01-31T00:00:00Z", "2026-Q1"),
        ("2026-03-31T23:59:59Z", "2026-Q1"),
        ("2026-04-01T00:00:00Z", "2026-Q2"),
        ("2026-07-15T12:00:00Z", "2026-Q3"),
        ("2026-12-31T23:59:59Z", "2026-Q4"),
        ("2026-08-01T10:00:00+00:00", "2026-Q3"),
    ],
)
def test_quarter_of(iso: str, expected: str) -> None:
    assert epics.quarter_of(iso) == expected


def test_quarter_of_rejects_empty() -> None:
    with pytest.raises(ValueError, match="empty timestamp"):
        epics.quarter_of("")


def test_current_quarter() -> None:
    assert epics.current_quarter(datetime(2026, 8, 8, tzinfo=UTC)) == "2026-Q3"


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


def test_issue_title_reads_via_rest() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="Epic (ad hoc): repo") as m:
        assert epics._issue_title(EPIC) == "Epic (ad hoc): repo"
    assert m.call_args.args == ("api", "repos/org/.github/issues/40", "--jq", ".title")


def test_issue_closed_at_reads_via_rest() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="2026-05-01T00:00:00Z") as m:
        assert epics._issue_closed_at(TASK) == "2026-05-01T00:00:00Z"
    assert m.call_args.args == ("api", "repos/org/repo-a/issues/101", "--jq", '.closed_at // ""')


def test_reflink_skips_results_without_repo() -> None:
    search = [
        {
            "number": 41,
            "state": "OPEN",
            "repository": {"nameWithOwner": "org/.github"},
            "body": "Parent: org/.github#40",
        },
        {"number": 99, "state": "OPEN", "repository": {}, "body": "Parent: org/.github#40"},
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


def test_reflink_rejects_full_text_false_positive() -> None:
    # gh search issues is punctuation-blind full-text search: a foreign issue can
    # match on words alone. Without a real ``Parent: <epic slug>`` line in its
    # body it must not be treated as a child (issue #2259, Fix D).
    search = [
        {
            "number": 41,
            "state": "OPEN",
            "repository": {"nameWithOwner": "org/.github"},
            "body": "Parent: org/.github#40",  # genuine child
        },
        {
            "number": 3465,
            "state": "OPEN",
            "repository": {"nameWithOwner": "org/unrelated"},
            "body": "mentions Parent and org/.github#40 in prose but no real reflink line",
        },
        {
            "number": 77,
            "state": "CLOSED",
            "repository": {"nameWithOwner": "org/other"},
            "body": "Parent: org/.github#41",  # references a DIFFERENT epic
        },
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


def test_is_operational_true_when_labeled() -> None:
    labels = {"labels": [{"name": "validation"}, {"name": "task"}]}
    with patch("vergil_tooling.lib.github.read_json", return_value=labels):
        assert epics.is_operational(TASK) is True


def test_is_operational_false_without_label() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"labels": [{"name": "task"}]}):
        assert epics.is_operational(TASK) is False


def test_operational_kind_returns_the_label() -> None:
    labels = {"labels": [{"name": "validation"}, {"name": "task"}]}
    with patch("vergil_tooling.lib.github.read_json", return_value=labels):
        assert epics.operational_kind(TASK) == "validation"


def test_operational_kind_none_without_label() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"labels": [{"name": "task"}]}):
        assert epics.operational_kind(TASK) is None


def test_is_operational_task_true_for_operational() -> None:
    with patch("vergil_tooling.lib.epics.is_operational", return_value=True) as mock:
        assert epics.is_operational_task("org/repo#7", default_repo="org/repo") is True
    mock.assert_called_once_with(IssueRef("org", "repo", 7))


def test_is_operational_task_false_for_plain_task() -> None:
    with patch("vergil_tooling.lib.epics.is_operational", return_value=False):
        assert epics.is_operational_task("#42", default_repo="org/repo") is False


def test_is_operational_task_false_for_unparseable_ref() -> None:
    # No resolvable default repo -> parse fails -> never an operational task.
    with patch("vergil_tooling.lib.epics.is_operational") as mock:
        assert epics.is_operational_task("#42", default_repo="") is False
    mock.assert_not_called()


def test_is_operational_true_for_deployment() -> None:
    # The deployment label joins the operational set (epic #124).
    labels = {"labels": [{"name": "deployment"}]}
    with patch("vergil_tooling.lib.github.read_json", return_value=labels):
        assert epics.is_operational(TASK) is True
        assert epics.operational_kind(TASK) == "deployment"


def test_render_blocked_by_emits_one_line_per_dep() -> None:
    out = epics.render_blocked_by([IssueRef("o", "r", 5), IssueRef("o", "r", 8)])
    assert "Blocked-by: o/r#5" in out
    assert "Blocked-by: o/r#8" in out


def test_render_blocked_by_empty_is_empty_string() -> None:
    assert epics.render_blocked_by([]) == ""


def test_blockers_of_parses_reflink_body() -> None:
    body = "Do the thing.\nBlocked-by: o/r#5\nBlocked-by: o/r#8\n"
    with patch("vergil_tooling.lib.github.read_output", return_value=body):
        refs = epics.blockers_of(IssueRef("o", "r", 42))
    assert refs == [IssueRef("o", "r", 5), IssueRef("o", "r", 8)]


def test_blockers_of_empty_when_no_reflinks() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="no dependencies here"):
        assert epics.blockers_of(IssueRef("o", "r", 42)) == []


def test_all_blockers_closed_true_when_all_closed() -> None:
    with (
        patch("vergil_tooling.lib.epics.blockers_of", return_value=[IssueRef("o", "r", 5)]),
        patch("vergil_tooling.lib.epics._issue_state", return_value="CLOSED"),
    ):
        assert epics.all_blockers_closed(IssueRef("o", "r", 42)) is True


def test_all_blockers_closed_false_when_any_open() -> None:
    with (
        patch(
            "vergil_tooling.lib.epics.blockers_of",
            return_value=[IssueRef("o", "r", 5), IssueRef("o", "r", 8)],
        ),
        patch("vergil_tooling.lib.epics._issue_state", side_effect=["CLOSED", "OPEN"]),
    ):
        assert epics.all_blockers_closed(IssueRef("o", "r", 42)) is False


def test_all_blockers_closed_true_when_no_blockers() -> None:
    # No blockers -> nothing holds it -> runnable (vacuously all-closed).
    with patch("vergil_tooling.lib.epics.blockers_of", return_value=[]):
        assert epics.all_blockers_closed(IssueRef("o", "r", 42)) is True


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


def test_rollup_holds_epic_open_while_validation_child_open() -> None:
    # A validation task is a normal open child; the rollup must not close the epic
    # while it is open. This locks in the "validation gates epic closure" guarantee
    # that the whole post-merge-validation framework depends on.
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=EPIC),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic"}),
        patch("vergil_tooling.lib.epics.all_children_closed", return_value=False),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(TASK)
    mock_run.assert_not_called()  # epic stays open — a validation child is still open


def test_rollup_archives_closed_child_under_live_adhoc() -> None:
    task = IssueRef("org", ".github", 101)
    live = IssueRef("org", ".github", 40)
    arch = IssueRef("org", ".github", 88)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=live),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (ad hoc): .github"),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value="2026-05-01T00:00:00Z"),
        patch("vergil_tooling.lib.epics.ensure_adhoc_archive", return_value=arch) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.epics.add_child") as mock_add,
        patch("vergil_tooling.lib.epics.remove_child") as mock_rm,
    ):
        epics.rollup(task)
    mock_ens.assert_called_once_with("org/.github", "2026-Q2")
    # Atomic single-parent-safe re-parent — no separate add/remove (#2691).
    mock_reparent.assert_called_once_with(arch, task)
    mock_add.assert_not_called()
    mock_rm.assert_not_called()


def test_rollup_noop_when_parent_is_adhoc_archive() -> None:
    task = IssueRef("org", ".github", 101)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=IssueRef("org", ".github", 88)),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch(
            "vergil_tooling.lib.epics._issue_title",
            return_value="Epic (ad hoc): .github — 2026-Q2",
        ),
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(task)
    mock_reparent.assert_not_called()


def test_rollup_noop_when_closed_child_lacks_closed_at() -> None:
    # Defensive: a rollup event with no resolvable closed_at drains nothing.
    live = IssueRef("org", ".github", 40)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=live),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (ad hoc): .github"),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value=""),
        patch("vergil_tooling.lib.epics.ensure_adhoc_archive") as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(TASK)
    mock_ens.assert_not_called()
    mock_reparent.assert_not_called()


def test_rollup_skips_reparent_when_archive_is_the_live_epic() -> None:
    # Defensive guard: if the resolved archive is the live epic itself, never
    # re-parent the child into its own parent (reparent_child skipped).
    live = IssueRef("org", ".github", 40)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=live),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (ad hoc): .github"),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value="2026-05-01T00:00:00Z"),
        patch("vergil_tooling.lib.epics.ensure_adhoc_archive", return_value=live),
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(TASK)
    mock_reparent.assert_not_called()


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


# -- reparent_child (atomic single-parent-safe move, #2691) ------------------


def test_reparent_mutation_uses_replace_parent() -> None:
    # GitHub's single-parent rule makes add-before-remove impossible; the atomic
    # move rides addSubIssue with replaceParent: true (issue #2691, proven live).
    assert "replaceParent: true" in epics._REPARENT_SUBISSUE


def test_reparent_child_issues_replace_parent_mutation() -> None:
    with (
        patch("vergil_tooling.lib.epics._node_id", side_effect=["ARCHIVE_ID", "TASK_ID"]),
        patch("vergil_tooling.lib.github.graphql") as mock_graphql,
    ):
        epics.reparent_child(EPIC, TASK)
    mock_graphql.assert_called_once()
    assert "replaceParent: true" in mock_graphql.call_args.args[0]
    assert mock_graphql.call_args.kwargs == {"parent": "ARCHIVE_ID", "child": "TASK_ID"}


# -- resolve_epic_ref / ensure_adhoc_epic ------------------------------------


def _adhoc_row(number: int, repo_bare: str = "tooling") -> dict:
    """A .github issue-list row for an ad-hoc epic titled for *repo_bare*."""
    return {"number": number, "title": f"Epic (ad hoc): {repo_bare}"}


def test_resolve_epic_ref_adhoc_discovers_single_epic() -> None:
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[_adhoc_row(1972)]) as mock_list,
    ):
        assert epics.resolve_epic_ref("adhoc", repo="org/tooling") == IssueRef(
            "org", ".github", 1972
        )
    # discovery targets <org>/.github, filtering issues carrying epic + ad-hoc
    args = mock_list.call_args.args
    assert "org/.github" in args and "epic" in args and "ad-hoc" in args


def test_ensure_adhoc_epic_zero_creates_in_dotgithub() -> None:
    # When no ad-hoc epic exists, ensure creates it in <org>/.github, by title.
    created = "https://github.com/org/.github/issues/77"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        result = epics.ensure_adhoc_epic("org/tooling")
    assert result == IssueRef("org", ".github", 77)
    assert mock_create.call_args.kwargs["repo"] == "org/.github"
    assert mock_create.call_args.kwargs["labels"] == ["epic", "ad-hoc"]
    assert mock_create.call_args.kwargs["title"] == "Epic (ad hoc): tooling"


def test_ensure_adhoc_epic_private_repo_homes_in_self() -> None:
    # A private target homes its ad-hoc epic in the repo itself, not .github.
    created = "https://github.com/org/lab/issues/3"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/lab"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        result = epics.ensure_adhoc_epic("org/lab")
    assert result == IssueRef("org", "lab", 3)
    assert mock_create.call_args.kwargs["repo"] == "org/lab"
    assert mock_create.call_args.kwargs["title"] == "Epic (ad hoc): lab"


def test_ensure_adhoc_epic_for_dotgithub_itself() -> None:
    created = "https://github.com/org/.github/issues/5"
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        assert epics.ensure_adhoc_epic("org/.github") == IssueRef("org", ".github", 5)
    assert mock_create.call_args.kwargs["title"] == "Epic (ad hoc): .github"


def test_ensure_adhoc_epic_reuses_existing_by_title() -> None:
    # Idempotent and title-disambiguated: the same-title epic is reused; a
    # different repo's ad-hoc epic in the same .github list is ignored.
    rows = [_adhoc_row(1972), {"number": 40, "title": "Epic (ad hoc): actions"}]
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        assert epics.ensure_adhoc_epic("org/tooling") == IssueRef("org", ".github", 1972)
    mock_create.assert_not_called()


def test_ensure_adhoc_epic_multiple_same_title_raises() -> None:
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch(
            "vergil_tooling.lib.github.read_json",
            return_value=[_adhoc_row(1), _adhoc_row(2)],
        ),
        pytest.raises(ValueError, match="multiple ad-hoc epics"),
    ):
        epics.ensure_adhoc_epic("org/tooling")


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


def test_ensure_adhoc_epic_repo_without_owner_raises() -> None:
    with pytest.raises(ValueError, match="cannot resolve repo for ad-hoc epic"):
        epics.ensure_adhoc_epic("tooling")


# -- ad-hoc archive finders (issue #2678) ------------------------------------


def test_find_adhoc_epic_returns_none_when_absent() -> None:
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
    ):
        assert epics.find_adhoc_epic("org/tooling") is None


def test_find_adhoc_epic_reuses_existing_by_title() -> None:
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[_adhoc_row(1972)]),
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        assert epics.find_adhoc_epic("org/tooling") == IssueRef("org", ".github", 1972)
    mock_create.assert_not_called()  # find never creates


def test_find_adhoc_epic_repo_without_owner_raises() -> None:
    with pytest.raises(ValueError, match="cannot resolve repo for ad-hoc epic"):
        epics.find_adhoc_epic("tooling")


def test_find_epic_by_title_prefer_oldest_returns_lowest() -> None:
    # Two open rows share the archive title (the list-consistency race that
    # produced duplicate archives). With prefer_oldest the lowest-numbered wins;
    # without it the ambiguity still raises (live-epic finders keep that guard).
    title = "Epic (ad hoc): tooling — 2026-Q3"
    rows = [{"number": 91, "title": title}, {"number": 88, "title": title}]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows):
        assert epics._find_epic_by_title("org/.github", title, prefer_oldest=True) == IssueRef(
            "org", ".github", 88
        )
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        pytest.raises(ValueError, match="multiple ad-hoc epics"),
    ):
        epics._find_epic_by_title("org/.github", title)


def test_ensure_adhoc_archive_reuses_oldest_duplicate() -> None:
    # Duplicate archives already exist; ensure_adhoc_archive reuses the oldest
    # (lowest-numbered) and never creates another.
    title = "Epic (ad hoc): tooling — 2026-Q3"
    rows = [{"number": 91, "title": title}, {"number": 88, "title": title}]
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        assert epics.ensure_adhoc_archive("org/tooling", "2026-Q3") == IssueRef(
            "org", ".github", 88
        )
    mock_create.assert_not_called()


def test_ensure_adhoc_archive_creates_stamped_title() -> None:
    created = "https://github.com/org/.github/issues/88"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        ref = epics.ensure_adhoc_archive("org/tooling", "2026-Q3")
    assert ref == IssueRef("org", ".github", 88)
    assert mock_create.call_args.kwargs["title"] == "Epic (ad hoc): tooling — 2026-Q3"
    assert mock_create.call_args.kwargs["labels"] == ["epic", "ad-hoc"]


def test_ensure_adhoc_archive_reuses_existing_stamped() -> None:
    rows = [{"number": 88, "title": "Epic (ad hoc): tooling — 2026-Q3"}]
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        assert epics.ensure_adhoc_archive("org/tooling", "2026-Q3") == IssueRef(
            "org", ".github", 88
        )
    mock_create.assert_not_called()


def test_list_open_adhoc_archives_parses_quarter() -> None:
    rows = [
        {"number": 88, "title": "Epic (ad hoc): tooling — 2026-Q2"},
        {"number": 90, "title": "Epic (ad hoc): tooling"},  # live, not an archive
        {"number": 91, "title": "Epic (ad hoc): tooling — 2026-Q3"},
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows):
        got = epics.list_open_adhoc_archives("org/.github")
    assert (IssueRef("org", ".github", 88), "2026-Q2") in got
    assert (IssueRef("org", ".github", 91), "2026-Q3") in got
    assert all(q for _, q in got) and len(got) == 2


# -- resolve_epic_home (epic #130) -------------------------------------------
def test_resolve_epic_home_dotgithub_short_circuits() -> None:
    # A ".github" target never probes visibility.
    with patch("vergil_tooling.lib.epics.github.is_public") as pub:
        assert epics.resolve_epic_home("org", ".github") == "org/.github"
        pub.assert_not_called()


def test_resolve_epic_home_public_target_is_central() -> None:
    with patch("vergil_tooling.lib.epics.github.is_public", return_value=True):
        assert epics.resolve_epic_home("org", "tooling") == "org/.github"


def test_resolve_epic_home_private_target_public_dotgithub_is_self() -> None:
    def pub(nwo: str) -> bool:
        return {"org/lab": False, "org/.github": True}[nwo]

    with patch("vergil_tooling.lib.epics.github.is_public", side_effect=pub):
        assert epics.resolve_epic_home("org", "lab") == "org/lab"


def test_resolve_epic_home_private_org_is_central() -> None:
    def pub(nwo: str) -> bool:
        return {"org/lab": False, "org/.github": False}[nwo]

    with patch("vergil_tooling.lib.epics.github.is_public", side_effect=pub):
        assert epics.resolve_epic_home("org", "lab") == "org/.github"


def test_resolve_epic_home_fails_loud() -> None:
    with (
        patch(
            "vergil_tooling.lib.epics.github.is_public",
            side_effect=github.GitHubAPIError(1, "cmd", "boom"),
        ),
        pytest.raises(github.GitHubAPIError),
    ):
        epics.resolve_epic_home("org", "missing")


# --- Task 4: per-repo drain (plan + apply) ---

NOW = datetime(2026, 8, 8, tzinfo=UTC)  # 2026-Q3


def _child(n: int, state: str, closed_at: str = "") -> ChildState:
    return ChildState(IssueRef("org", ".github", n), state, "t", closed_at)


def test_plan_drain_moves_closed_buckets_by_quarter_and_closes_past() -> None:
    live = IssueRef("org", ".github", 40)
    kids = [
        _child(101, "CLOSED", "2026-05-10T00:00:00Z"),  # Q2
        _child(102, "CLOSED", "2026-07-02T00:00:00Z"),  # Q3
        _child(103, "OPEN"),  # stays
    ]
    with (
        patch("vergil_tooling.lib.epics.find_adhoc_epic", return_value=live),
        patch("vergil_tooling.lib.epics.child_states", return_value=kids),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch(
            "vergil_tooling.lib.epics.list_open_adhoc_archives",
            return_value=[
                (IssueRef("org", ".github", 88), "2026-Q2"),
                (IssueRef("org", ".github", 91), "2026-Q3"),
            ],
        ),
    ):
        plan = epics.plan_adhoc_drain("org/tooling", now=NOW)
    assert plan is not None
    assert (IssueRef("org", ".github", 101), "2026-Q2") in plan.moves
    assert (IssueRef("org", ".github", 102), "2026-Q3") in plan.moves
    assert all(ref.number != 103 for ref, _ in plan.moves)  # open child not moved
    assert plan.close == [IssueRef("org", ".github", 88)]  # Q2 < Q3 -> close; Q3 stays open


def test_plan_drain_none_when_no_live_epic() -> None:
    with patch("vergil_tooling.lib.epics.find_adhoc_epic", return_value=None):
        assert epics.plan_adhoc_drain("org/tooling", now=NOW) is None


def test_apply_drain_ensures_archive_moves_then_closes() -> None:
    live = IssueRef("org", ".github", 40)
    plan = epics.DrainPlan(
        live=live,
        moves=[(IssueRef("org", ".github", 101), "2026-Q2")],
        close=[IssueRef("org", ".github", 88)],
    )
    arch = IssueRef("org", ".github", 88)
    with (
        patch("vergil_tooling.lib.epics.ensure_adhoc_archive", return_value=arch) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.epics.add_child") as mock_add,
        patch("vergil_tooling.lib.epics.remove_child") as mock_rm,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    mock_ens.assert_called_once_with("org/tooling", "2026-Q2")
    # Single atomic re-parent — no add-before-remove (single-parent safe, #2691).
    mock_reparent.assert_called_once_with(arch, IssueRef("org", ".github", 101))
    mock_add.assert_not_called()
    mock_rm.assert_not_called()
    assert mock_run.call_args.args[:2] == ("issue", "close")
    assert "88" in mock_run.call_args.args


def test_apply_drain_skips_archive_equal_to_live() -> None:
    live = IssueRef("org", ".github", 40)
    plan = epics.DrainPlan(
        live=live,
        moves=[(IssueRef("org", ".github", 101), "2026-Q2")],
        close=[],
    )
    with (
        patch("vergil_tooling.lib.epics.ensure_adhoc_archive", return_value=live),
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    mock_reparent.assert_not_called()  # defensive: never re-parent into the live epic
    mock_run.assert_not_called()


def test_apply_drain_ensures_archive_once_per_quarter() -> None:
    # Many closed children in the SAME quarter must ensure that quarter's archive
    # exactly ONCE (cache by quarter), then re-parent each child — the fix for the
    # duplicate-archive race (#2698).
    live = IssueRef("org", ".github", 40)
    arch = IssueRef("org", ".github", 88)
    kids = [IssueRef("org", ".github", n) for n in (101, 102, 103)]
    plan = epics.DrainPlan(
        live=live,
        moves=[(k, "2026-Q2") for k in kids],
        close=[],
    )
    with (
        patch("vergil_tooling.lib.epics.ensure_adhoc_archive", return_value=arch) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    mock_ens.assert_called_once_with("org/tooling", "2026-Q2")
    assert mock_reparent.call_count == len(kids)
    assert [c.args for c in mock_reparent.call_args_list] == [(arch, k) for k in kids]


def test_apply_drain_two_quarters_ensures_twice() -> None:
    # Moves spanning two distinct quarters ensure once PER quarter (2 total).
    live = IssueRef("org", ".github", 40)
    archives = {
        "2026-Q2": IssueRef("org", ".github", 88),
        "2026-Q3": IssueRef("org", ".github", 91),
    }
    plan = epics.DrainPlan(
        live=live,
        moves=[
            (IssueRef("org", ".github", 101), "2026-Q2"),
            (IssueRef("org", ".github", 102), "2026-Q2"),
            (IssueRef("org", ".github", 103), "2026-Q3"),
        ],
        close=[],
    )
    with (
        patch(
            "vergil_tooling.lib.epics.ensure_adhoc_archive",
            side_effect=lambda _repo, q: archives[q],
        ) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    assert mock_ens.call_count == 2
    assert {c.args[1] for c in mock_ens.call_args_list} == {"2026-Q2", "2026-Q3"}
    assert mock_reparent.call_count == 3


def test_drain_adhoc_repo_applies_when_apply_true() -> None:
    plan = epics.DrainPlan(IssueRef("org", ".github", 40), moves=[], close=[])
    with (
        patch("vergil_tooling.lib.epics.plan_adhoc_drain", return_value=plan) as mock_plan,
        patch("vergil_tooling.lib.epics.apply_adhoc_drain") as mock_apply,
    ):
        result = epics.drain_adhoc_repo("org/tooling", apply=True, now=NOW)
    assert result is plan
    mock_apply.assert_called_once_with("org/tooling", plan)
    assert mock_plan.call_args.kwargs["now"] == NOW


def test_drain_adhoc_repo_dry_run_does_not_apply() -> None:
    plan = epics.DrainPlan(IssueRef("org", ".github", 40), moves=[], close=[])
    with (
        patch("vergil_tooling.lib.epics.plan_adhoc_drain", return_value=plan),
        patch("vergil_tooling.lib.epics.apply_adhoc_drain") as mock_apply,
    ):
        result = epics.drain_adhoc_repo("org/tooling", apply=False, now=NOW)
    assert result is plan
    mock_apply.assert_not_called()


def test_drain_adhoc_repo_none_plan_never_applies() -> None:
    with (
        patch("vergil_tooling.lib.epics.plan_adhoc_drain", return_value=None),
        patch("vergil_tooling.lib.epics.apply_adhoc_drain") as mock_apply,
    ):
        result = epics.drain_adhoc_repo("org/tooling", apply=True, now=NOW)
    assert result is None
    mock_apply.assert_not_called()


# --- Task 5: org-wide drain (visibility-aware) ---


def test_drain_adhoc_org_iterates_repos_visibility_aware() -> None:
    seen: list[tuple[str, bool]] = []

    def fake_repo(target_repo: str, *, apply: bool, now: datetime) -> None:
        seen.append((target_repo, apply))
        return None

    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tooling", "priv"]),
        patch("vergil_tooling.lib.epics.drain_adhoc_repo", side_effect=fake_repo),
    ):
        epics.drain_adhoc_org("org", apply=True, now=NOW)
    assert ("org/tooling", True) in seen and ("org/priv", True) in seen


def test_drain_adhoc_org_skips_repo_that_raises() -> None:
    good = epics.DrainPlan(IssueRef("org", ".github", 40), moves=[], close=[])
    seen: list[str] = []

    def fake(target_repo: str, *, apply: bool, now: datetime) -> epics.DrainPlan:
        seen.append(target_repo)
        if target_repo == "org/bad":
            raise ValueError("multiple ad-hoc epics — corruption")
        return good

    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["bad", "good"]),
        patch("vergil_tooling.lib.epics.drain_adhoc_repo", side_effect=fake),
    ):
        plans = epics.drain_adhoc_org("org", apply=True, now=NOW)  # must NOT raise
    assert seen == ["org/bad", "org/good"]  # continued past the failure
    assert plans == [good]  # the healthy repo still drained
