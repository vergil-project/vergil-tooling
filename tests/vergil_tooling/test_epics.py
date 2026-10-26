"""Tests for vergil_tooling.lib.epics (umbrella relationship)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.epics import _SUBISSUES_QUERY, ChildState, IssueRef
from vergil_tooling.lib.labels import load_labels

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


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["epic", "enhancement"], "epic"),
        (["epic", "adhoc"], "epic"),  # a live standing epic is still an epic
        (["archive", "adhoc"], "archive"),  # a per-quarter standing-epic archive
        (["archive", "epic"], "epic"),  # both: the epic it still is
        (["bug"], None),
        ([], None),
    ],
)
def test_non_task_kind(labels: list[str], expected: str | None) -> None:
    raw = {"labels": [{"name": name} for name in labels]}
    with patch("vergil_tooling.lib.github.read_json", return_value=raw):
        assert epics.non_task_kind(TASK) == expected
    # The lookup-free form gives the same answer from the labels alone (#3592).
    assert epics.non_task_kind_of_labels(set(labels)) == expected


def test_non_task_ref_kind_resolves_ref() -> None:
    with patch("vergil_tooling.lib.epics.non_task_kind", return_value="archive") as mock:
        assert epics.non_task_ref_kind("org/.github#40", default_repo="org/repo") == "archive"
    mock.assert_called_once_with(IssueRef("org", ".github", 40))


def test_non_task_ref_kind_bare_ref_uses_default_repo() -> None:
    with patch("vergil_tooling.lib.epics.non_task_kind", return_value=None) as mock:
        assert epics.non_task_ref_kind("#42", default_repo="org/repo") is None
    mock.assert_called_once_with(IssueRef("org", "repo", 42))


def test_non_task_ref_kind_none_for_unparseable_ref() -> None:
    # No resolvable default repo -> parse fails -> never a non-task (no lookup).
    with patch("vergil_tooling.lib.epics.non_task_kind") as mock:
        assert epics.non_task_ref_kind("#42", default_repo="") is None
    mock.assert_not_called()


def test_non_task_ref_kind_propagates_lookup_failure() -> None:
    # Fail closed: a failed label read is never taken as "a task".
    with (
        patch(
            "vergil_tooling.lib.github.read_json",
            side_effect=subprocess.CalledProcessError(1, ["gh"]),
        ),
        pytest.raises(subprocess.CalledProcessError),
    ):
        epics.non_task_ref_kind("#42", default_repo="org/repo")


def test_non_task_refusal_message_epic() -> None:
    msg = epics.non_task_refusal_message("--task", "org/.github#40", "epic")
    assert msg.startswith("--task (org/.github#40) is an epic, not a task; ")
    assert "Create the task under the epic first" in msg


def test_non_task_refusal_message_archive() -> None:
    msg = epics.non_task_refusal_message("--issue", "#7", "archive")
    assert msg.startswith("--issue (#7) is an archive, not a task; ")
    assert "closed tasks" in msg


def test_non_task_refusal_message_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="not a non-task kind"):
        epics.non_task_refusal_message("--issue", "#7", "intake")


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


def test_rollup_public_repo_epic_archives_into_own_bucket() -> None:
    # A public repo's ad-hoc epic lives in <org>/.github (so parent.repo is
    # ".github"), but the archive target must come from the epic TITLE's bare
    # name — the child is filed into <org>/some-repo, NOT the ".github" bucket
    # (#2709). ``some-repo`` is a real repo, so the drain proceeds.
    task = IssueRef("org", ".github", 101)
    live = IssueRef("org", ".github", 40)
    arch = IssueRef("org", "some-repo", 88)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=live),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (ad hoc): some-repo"),
        patch("vergil_tooling.lib.github.repo_exists", return_value=True) as mock_exists,
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value="2026-05-01T00:00:00Z"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch(
            "vergil_tooling.lib.epics.ensure_writable_standing_archive", return_value=arch
        ) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(task)
    mock_exists.assert_called_once_with("org", "some-repo")
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", "some-repo", "2026-Q2")
    mock_reparent.assert_called_once_with(arch, task)


def test_rollup_dotgithub_own_epic_unchanged() -> None:
    # Regression guard: the .github repo's OWN ad-hoc epic has bare ".github",
    # so the target resolves to "org/.github" exactly as before — the one case
    # the old parent.repo-based path got right.
    task = IssueRef("org", ".github", 101)
    live = IssueRef("org", ".github", 40)
    arch = IssueRef("org", ".github", 88)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=live),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (ad hoc): .github"),
        patch("vergil_tooling.lib.github.repo_exists", return_value=True) as mock_exists,
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value="2026-05-01T00:00:00Z"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch(
            "vergil_tooling.lib.epics.ensure_writable_standing_archive", return_value=arch
        ) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.epics.add_child") as mock_add,
        patch("vergil_tooling.lib.epics.remove_child") as mock_rm,
    ):
        epics.rollup(task)
    mock_exists.assert_called_once_with("org", ".github")
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", ".github", "2026-Q2")
    # Atomic single-parent-safe re-parent — no separate add/remove (#2691).
    mock_reparent.assert_called_once_with(arch, task)
    mock_add.assert_not_called()
    mock_rm.assert_not_called()


def test_rollup_skips_non_repo_adhoc_epic() -> None:
    # A non-repo special epic (its bare name is a prose description, not a repo)
    # is skipped: the space in the bare name fast-rejects before any API call,
    # so nothing is archived or re-parented (#2709).
    task = IssueRef("org", ".github", 101)
    live = IssueRef("org", ".github", 40)
    title = "Epic (ad hoc): Lab lifecycle reliability — cold rebuild / boot / repoint / deploy"
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=live),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value=title),
        patch("vergil_tooling.lib.github.repo_exists") as mock_exists,
        patch("vergil_tooling.lib.epics.ensure_writable_standing_archive") as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(task)
    mock_exists.assert_not_called()  # fast-rejected on the space, no API probe
    mock_ens.assert_not_called()
    mock_reparent.assert_not_called()


def test_rollup_noop_when_parent_is_adhoc_archive() -> None:
    # A stamped archive (new-form title) is terminal: the archive_re(ADHOC) guard
    # short-circuits the drain even if the archive still carries the epic label.
    task = IssueRef("org", ".github", 101)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=IssueRef("org", ".github", 88)),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch(
            "vergil_tooling.lib.epics._issue_title",
            return_value="Archive (ad hoc): .github — 2026-Q2",
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
        patch("vergil_tooling.lib.github.repo_exists", return_value=True),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value=""),
        patch("vergil_tooling.lib.epics.ensure_writable_standing_archive") as mock_ens,
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
        patch("vergil_tooling.lib.github.repo_exists", return_value=True),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value="2026-05-01T00:00:00Z"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics.ensure_writable_standing_archive", return_value=live),
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
        patch("vergil_tooling.lib.epics.ensure_labels"),
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
        patch("vergil_tooling.lib.epics.ensure_labels"),
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
        patch("vergil_tooling.lib.epics.ensure_labels"),
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
        epics.resolve_epic_ref("org/.github#123", repo="org/repo")


def _visibility(public: set[str]) -> object:
    """An is_public fake: True only for the repos in *public*."""
    return lambda nwo: nwo in public


def test_resolve_epic_ref_bare_ref_public_task_homes_in_dotgithub() -> None:
    # A bare '#N' is read against the *task repo's* epic home, never the repo
    # itself: a public task's epics live in <org>/.github (#3411).
    with (
        patch(
            "vergil_tooling.lib.github.is_public",
            side_effect=_visibility({"org/tooling", "org/.github"}),
        ),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True) as mock_is_epic,
    ):
        epic = epics.resolve_epic_ref("#407", repo="org/tooling")
    assert epic == IssueRef("org", ".github", 407)
    mock_is_epic.assert_called_once_with(IssueRef("org", ".github", 407))


def test_resolve_epic_ref_bare_ref_private_task_homes_in_itself() -> None:
    # A private task with a public .github self-homes its epics, so a bare
    # '#N' resolves to the repo itself (#3411).
    with (
        patch("vergil_tooling.lib.github.is_public", side_effect=_visibility({"org/.github"})),
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
    ):
        assert epics.resolve_epic_ref("#12", repo="org/lab") == IssueRef("org", "lab", 12)


def test_resolve_epic_ref_qualified_ref_skips_home_resolution() -> None:
    # A fully qualified ref is taken as written: no visibility probe at all.
    with (
        patch("vergil_tooling.lib.github.is_public") as mock_public,
        patch("vergil_tooling.lib.epics.is_epic", return_value=True),
    ):
        assert epics.resolve_epic_ref("org/tooling#9", repo="org/lab") == IssueRef(
            "org", "tooling", 9
        )
    mock_public.assert_not_called()


def test_qualify_epic_ref_visibility_error_propagates() -> None:
    # Fail loud: an unreadable visibility never falls back to a guessed home.
    err = github.GitHubAPIError(1, "gh repo view", "boom")
    with (
        patch("vergil_tooling.lib.github.is_public", side_effect=err),
        pytest.raises(epics.EpicHomeError) as excinfo,
    ):
        epics.qualify_epic_ref("#12", repo="org/lab")
    assert excinfo.value.__cause__ is err


def test_qualify_epic_ref_rejects_malformed_ref() -> None:
    with pytest.raises(ValueError, match="not an issue ref"):
        epics.qualify_epic_ref("not-a-ref", repo="org/lab")


def test_ensure_adhoc_epic_repo_without_owner_raises() -> None:
    with pytest.raises(ValueError, match="cannot resolve repo for ad-hoc epic"):
        epics.ensure_adhoc_epic("tooling")


# -- ad-hoc archive finders (issue #2678) ------------------------------------


def test_find_standing_epic_adhoc_returns_none_when_absent() -> None:
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
    ):
        assert epics.find_standing_epic(epics.ADHOC, "org/.github", "tooling") is None


def test_find_standing_epic_adhoc_reuses_existing_by_title() -> None:
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[_adhoc_row(1972)]),
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        assert epics.find_standing_epic(epics.ADHOC, "org/.github", "tooling") == IssueRef(
            "org", ".github", 1972
        )
    mock_create.assert_not_called()  # find never creates


def test_adhoc_home_repo_without_owner_raises() -> None:
    with pytest.raises(ValueError, match="cannot resolve repo for ad-hoc epic"):
        epics._adhoc_home("tooling")


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
    title = "Archive (ad hoc): tooling — 2026-Q3"
    rows = [{"number": 91, "title": title}, {"number": 88, "title": title}]
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        assert epics.ensure_standing_archive(
            epics.ADHOC, "org/.github", "tooling", "2026-Q3"
        ) == IssueRef("org", ".github", 88)
    mock_create.assert_not_called()


def test_ensure_adhoc_archive_creates_stamped_title() -> None:
    created = "https://github.com/org/.github/issues/88"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels"),
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        ref = epics.ensure_standing_archive(epics.ADHOC, "org/.github", "tooling", "2026-Q3")
    assert ref == IssueRef("org", ".github", 88)
    assert mock_create.call_args.kwargs["title"] == "Archive (ad hoc): tooling — 2026-Q3"
    assert mock_create.call_args.kwargs["labels"] == ["archive", "ad-hoc"]


def test_ensure_adhoc_archive_reuses_existing_stamped() -> None:
    rows = [{"number": 88, "title": "Archive (ad hoc): tooling — 2026-Q3"}]
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        assert epics.ensure_standing_archive(
            epics.ADHOC, "org/.github", "tooling", "2026-Q3"
        ) == IssueRef("org", ".github", 88)
    mock_create.assert_not_called()


def test_list_open_adhoc_archives_parses_quarter() -> None:
    rows = [
        {"number": 88, "title": "Archive (ad hoc): tooling — 2026-Q2"},
        {"number": 90, "title": "Epic (ad hoc): tooling"},  # live, not an archive
        {"number": 91, "title": "Archive (ad hoc): tooling — 2026-Q3"},
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows):
        got = epics.list_open_standing_archives(epics.ADHOC, "org/.github")
    assert (IssueRef("org", ".github", 88), "2026-Q2") in got
    assert (IssueRef("org", ".github", 91), "2026-Q3") in got
    assert all(q for _, q in got) and len(got) == 2


# -- archive rename: recognizers and parameterized title search (Task 2) ------


def test_archive_regex_matches_new_form_only() -> None:
    assert epics.archive_re(epics.ADHOC).match("Archive (ad hoc): tooling — 2026-Q3")
    # Live epic and legacy archive titles must NOT match the new-form regex.
    assert epics.archive_re(epics.ADHOC).match("Epic (ad hoc): tooling") is None
    assert epics.archive_re(epics.ADHOC).match("Epic (ad hoc): tooling — 2026-Q3") is None


def test_legacy_archive_regex_matches_old_form_only() -> None:
    m = epics._LEGACY_ADHOC_ARCHIVE_RE.match("Epic (ad hoc): tooling — 2026-Q3")
    assert m and m.group("bare") == "tooling" and m.group("quarter") == "2026-Q3"
    assert epics._LEGACY_ADHOC_ARCHIVE_RE.match("Archive (ad hoc): tooling — 2026-Q3") is None
    assert epics._LEGACY_ADHOC_ARCHIVE_RE.match("Epic (ad hoc): tooling") is None


def test_find_epic_by_title_uses_supplied_labels() -> None:
    title = "Archive (ad hoc): tooling — 2026-Q3"
    rows = [{"number": 42, "title": title}]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows) as mock_list:
        got = epics._find_epic_by_title("org/.github", title, labels=("archive", "ad-hoc"))
    assert got == IssueRef("org", ".github", 42)
    # The label filters passed to gh reflect the supplied labels, not epic+ad-hoc.
    args = list(mock_list.call_args.args)
    assert "archive" in args and "ad-hoc" in args and "epic" not in args


# -- archive rename: rollup no-ops under a converted archive parent (Task 3) ---


def test_rollup_noop_for_child_under_archive_parent() -> None:
    # A converted archive is not epic-labelled, so rollup returns without draining.
    task = IssueRef("org", ".github", 200)
    archive = IssueRef("org", ".github", 88)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=archive),
        patch("vergil_tooling.lib.epics._labels", return_value={"archive", "ad-hoc"}),
        patch("vergil_tooling.lib.epics.ensure_standing_archive") as mock_ensure,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(task)
    mock_ensure.assert_not_called()
    mock_reparent.assert_not_called()


# -- archive rename: self-healing creation (Task 4) ---------------------------


def test_normalize_archive_in_place_edits_title_and_labels() -> None:
    ref = IssueRef("org", ".github", 88)
    with (
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics._normalize_archive_in_place(ref, "Archive (ad hoc): tooling — 2026-Q3")
    args = list(mock_run.call_args.args)
    assert args[:2] == ["issue", "edit"] and "88" in args
    assert "--title" in args and "Archive (ad hoc): tooling — 2026-Q3" in args
    assert args[args.index("--add-label") + 1] == "archive"
    assert args[args.index("--remove-label") + 1] == "epic"
    # The 'archive' label is self-healed in the target repo before it is applied,
    # so a not-yet-migrated org's .github does not fail with 'archive' not found.
    mock_ensure.assert_called_once_with("org/.github", ("archive",))


def test_ensure_adhoc_archive_create_self_heals_labels() -> None:
    # The create branch ensures 'archive'+'ad-hoc' exist before creating the
    # archive issue, so the org's .github self-heals rather than failing.
    created = "https://github.com/org/.github/issues/88"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch("vergil_tooling.lib.github.create_issue", return_value=created),
    ):
        epics.ensure_standing_archive(epics.ADHOC, "org/.github", "tooling", "2026-Q3")
    mock_ensure.assert_called_once_with("org/.github", ("archive", "ad-hoc"))


def test_ensure_adhoc_epic_create_self_heals_labels() -> None:
    # The create branch ensures 'epic'+'ad-hoc' exist before creating the epic.
    created = "https://github.com/org/.github/issues/77"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch("vergil_tooling.lib.github.create_issue", return_value=created),
    ):
        epics.ensure_adhoc_epic("org/tooling")
    mock_ensure.assert_called_once_with("org/.github", ("epic", "ad-hoc"))


def test_ensure_adhoc_archive_heals_legacy_in_place() -> None:
    # No new-form archive exists, but a legacy one does: heal it, don't create.
    def fake_find(home, title, *, prefer_oldest=False, labels=("epic", "ad-hoc")):
        if tuple(labels) == ("archive", "ad-hoc"):
            return None  # no new-form archive yet
        if title == "Epic (ad hoc): tooling — 2026-Q3":
            return IssueRef("org", ".github", 88)  # legacy archive found
        return None

    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._find_epic_by_title", side_effect=fake_find),
        patch("vergil_tooling.lib.epics._normalize_archive_in_place") as mock_heal,
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        got = epics.ensure_standing_archive(epics.ADHOC, "org/.github", "tooling", "2026-Q3")
    assert got == IssueRef("org", ".github", 88)
    mock_heal.assert_called_once_with(
        IssueRef("org", ".github", 88), "Archive (ad hoc): tooling — 2026-Q3"
    )
    mock_create.assert_not_called()


# -- archive rename: org-wide normalize sweep (Task 5) ------------------------


def test_plan_normalize_dedupes_homes_and_finds_legacy_all_states() -> None:
    # Two public repos share the .github home; one private repo self-homes.
    def fake_home(org, bare):
        return "org/.github" if bare in {"tooling", "actions"} else f"org/{bare}"

    def fake_list(*args, **kwargs):
        home = args[args.index("--repo") + 1]
        if home == "org/.github":
            return [
                # legacy archive (closed) -> converts
                {"number": 88, "title": "Epic (ad hoc): tooling — 2026-Q2", "state": "CLOSED"},
                # live epic (unstamped) -> never matches
                {"number": 90, "title": "Epic (ad hoc): tooling", "state": "OPEN"},
                # already migrated (new form) -> skipped
                {"number": 91, "title": "Archive (ad hoc): actions — 2026-Q3", "state": "OPEN"},
            ]
        if home == "org/private":
            return [
                {"number": 5, "title": "Epic (ad hoc): private — 2026-Q1", "state": "CLOSED"},
            ]
        return []

    repos = ["tooling", "actions", "private"]
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=repos),
        patch("vergil_tooling.lib.epics.resolve_epic_home", side_effect=fake_home),
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_list),
    ):
        plan = epics.plan_normalize_adhoc("org")
    refs = {(c.ref.repo, c.ref.number): c for c in plan}
    # Legacy archives (public-homed + private self-homed) are converted; the live
    # epic and the already-migrated archive are not.
    assert (".github", 88) in refs and ("private", 5) in refs
    assert refs[(".github", 88)].new_title == "Archive (ad hoc): tooling — 2026-Q2"
    assert (".github", 90) not in refs and (".github", 91) not in refs
    assert len(plan) == 2


def test_apply_normalize_calls_in_place_for_each() -> None:
    ref = IssueRef("org", ".github", 88)
    new_title = "Archive (ad hoc): tooling — 2026-Q2"
    conv = [epics.ArchiveConversion(ref, "old", new_title)]
    with patch("vergil_tooling.lib.epics._normalize_archive_in_place") as mock_heal:
        epics.apply_normalize(conv)
    mock_heal.assert_called_once_with(ref, new_title)


def test_normalize_adhoc_archives_dry_run_does_not_mutate() -> None:
    with (
        patch("vergil_tooling.lib.epics.plan_normalize_adhoc", return_value=[]) as mock_plan,
        patch("vergil_tooling.lib.epics.apply_normalize") as mock_apply,
    ):
        out = epics.normalize_adhoc_archives("org", apply=False)
    assert out == []
    mock_plan.assert_called_once_with("org")
    mock_apply.assert_not_called()


def test_normalize_adhoc_archives_apply_executes_conversions() -> None:
    conv = [
        epics.ArchiveConversion(
            IssueRef("org", ".github", 88), "old", "Archive (ad hoc): tooling — 2026-Q2"
        )
    ]
    with (
        patch("vergil_tooling.lib.epics.plan_normalize_adhoc", return_value=conv),
        patch("vergil_tooling.lib.epics.apply_normalize") as mock_apply,
    ):
        out = epics.normalize_adhoc_archives("org", apply=True)
    assert out == conv
    mock_apply.assert_called_once_with(conv)


def test_plan_normalize_tolerates_non_list_and_non_dict_rows() -> None:
    # A non-dict row is skipped; a home whose listing is not a list yields nothing.
    def fake_home(org, bare):
        return "org/.github" if bare == "tooling" else f"org/{bare}"

    def fake_list(*args, **kwargs):
        home = args[args.index("--repo") + 1]
        if home == "org/.github":
            return [None, {"number": 88, "title": "Epic (ad hoc): tooling — 2026-Q2"}]
        return {}  # non-list result for the private home

    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tooling", "private"]),
        patch("vergil_tooling.lib.epics.resolve_epic_home", side_effect=fake_home),
        patch("vergil_tooling.lib.github.read_json", side_effect=fake_list),
    ):
        plan = epics.plan_normalize_adhoc("org")
    assert [(c.ref.repo, c.ref.number) for c in plan] == [(".github", 88)]


# -- resolve_epic_home (epic #130) -------------------------------------------
def test_resolve_epic_home_dotgithub_short_circuits() -> None:
    # A ".github" target never probes visibility.
    with patch("vergil_tooling.lib.epics.github.is_public") as pub:
        assert epics.resolve_epic_home("org", ".github") == "org/.github"
        pub.assert_not_called()


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
    # A failed probe of the target is a clear EpicHomeError, never a traceback.
    with (
        patch(
            "vergil_tooling.lib.epics.github.is_public",
            side_effect=github.GitHubAPIError(1, "cmd", "", "HTTP 502: boom"),
        ),
        pytest.raises(epics.EpicHomeError, match=r"(?s)org/missing:.*HTTP 502: boom"),
    ):
        epics.resolve_epic_home("org", "missing")


_NO_REPO = "GraphQL: Could not resolve to a Repository with the name 'owner/.github'."


def test_resolve_epic_home_missing_dotgithub_self_homes() -> None:
    # A personal account with no <owner>/.github has no central home, so a
    # private repo self-homes its epics (#3603).
    def pub(nwo: str) -> bool:
        if nwo == "owner/.github":
            raise github.GitHubAPIError(1, "cmd", "", _NO_REPO)
        return False

    with patch("vergil_tooling.lib.epics.github.is_public", side_effect=pub):
        assert epics.resolve_epic_home("owner", "notes") == "owner/notes"


def test_resolve_epic_home_public_target_with_dotgithub_probes_it() -> None:
    # A public target homes centrally only once <org>/.github is confirmed (#3643).
    with patch("vergil_tooling.lib.epics.github.is_public", return_value=True) as pub:
        assert epics.resolve_epic_home("org", "tooling") == "org/.github"
    assert [c.args[0] for c in pub.call_args_list] == ["org/tooling", "org/.github"]


def test_resolve_epic_home_public_target_missing_dotgithub_is_refused() -> None:
    # A public repo whose owner has no .github is refused up front, naming the
    # repo to create, instead of failing later on GitHub "not found" (#3643).
    def pub(nwo: str) -> bool:
        if nwo == "owner/.github":
            raise github.GitHubAPIError(1, "cmd", "", _NO_REPO)
        return True

    with (
        patch("vergil_tooling.lib.epics.github.is_public", side_effect=pub),
        pytest.raises(epics.EpicHomeError) as excinfo,
    ):
        epics.resolve_epic_home("owner", "site")
    assert str(excinfo.value) == (
        "owner/site is public, so its epics and queues live in owner/.github, "
        "which does not exist; create the owner/.github repository and try again"
    )


def test_resolve_epic_home_dotgithub_probe_error_fails_loud() -> None:
    # Only "does not exist" self-homes; any other .github probe failure is a
    # clear EpicHomeError naming the repo it could not resolve (#3603).
    def pub(nwo: str) -> bool:
        if nwo == "owner/.github":
            raise github.GitHubAPIError(1, "cmd", "", "HTTP 401: Bad credentials")
        return False

    with (
        patch("vergil_tooling.lib.epics.github.is_public", side_effect=pub),
        pytest.raises(epics.EpicHomeError, match=r"(?s)owner/notes:.*Bad credentials"),
    ):
        epics.resolve_epic_home("owner", "notes")


def test_resolve_epic_home_missing_token_is_not_rewrapped() -> None:
    # A missing token keeps its own error so cli_main prints the token fix.
    missing = github.MissingGitHubTokenError(4, "cmd", "", "set GH_TOKEN")
    with (
        patch("vergil_tooling.lib.epics.github.is_public", side_effect=missing),
        pytest.raises(github.MissingGitHubTokenError),
    ):
        epics.resolve_epic_home("owner", "notes")


def test_require_org_home_returns_existing_dotgithub() -> None:
    with patch("vergil_tooling.lib.epics.github.repo_exists", return_value=True) as exists:
        assert epics.require_org_home("org") == "org/.github"
    exists.assert_called_once_with("org", ".github")


def test_require_org_home_missing_dotgithub_is_clear() -> None:
    # An owner with no .github has no org-wide home; the message says so and
    # points at --repo for a self-homed repo (#3613).
    with (
        patch(
            "vergil_tooling.lib.epics.github.repo_visibility",
            side_effect=github.GitHubAPIError(1, "cmd", "", _NO_REPO),
        ),
        pytest.raises(epics.EpicHomeError) as excinfo,
    ):
        epics.require_org_home("owner")
    assert str(excinfo.value) == (
        "owner has no .github repository, so there is no org-wide queue or epic "
        "home; create the owner/.github repository to give it one, or pass --repo "
        "owner/<repo> to reach a private, self-homed repo's own queues and epics"
    )


def test_require_org_home_other_probe_error_fails_loud() -> None:
    # Only "does not exist" is the no-.github message; any other failure is a
    # clear EpicHomeError carrying the API error.
    with (
        patch(
            "vergil_tooling.lib.epics.github.repo_visibility",
            side_effect=github.GitHubAPIError(1, "cmd", "", "HTTP 502: boom"),
        ),
        pytest.raises(epics.EpicHomeError, match=r"(?s)cannot check for org/\.github,.*HTTP 502"),
    ):
        epics.require_org_home("org")


def test_require_org_home_missing_token_is_not_rewrapped() -> None:
    missing = github.MissingGitHubTokenError(4, "cmd", "", "set GH_TOKEN")
    with (
        patch("vergil_tooling.lib.epics.github.repo_visibility", side_effect=missing),
        pytest.raises(github.MissingGitHubTokenError),
    ):
        epics.require_org_home("org")


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
        patch("vergil_tooling.lib.epics.find_standing_epic", return_value=live),
        patch("vergil_tooling.lib.epics.child_states", return_value=kids),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch(
            "vergil_tooling.lib.epics.list_open_standing_archives",
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
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics.find_standing_epic", return_value=None) as mock_find,
    ):
        assert epics.plan_adhoc_drain("org/tooling", now=NOW) is None
    mock_find.assert_called_once_with(epics.ADHOC, "org/.github", "tooling")


def test_apply_drain_ensures_archive_moves_then_closes() -> None:
    live = IssueRef("org", ".github", 40)
    plan = epics.DrainPlan(
        live=live,
        moves=[(IssueRef("org", ".github", 101), "2026-Q2")],
        close=[IssueRef("org", ".github", 88)],
    )
    arch = IssueRef("org", ".github", 88)
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._standing_archive_segments", return_value=[]),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=0),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=arch) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.epics.add_child") as mock_add,
        patch("vergil_tooling.lib.epics.remove_child") as mock_rm,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", "tooling", "2026-Q2", segment=1)
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
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._standing_archive_segments", return_value=[]),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=0),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=live),
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    mock_reparent.assert_not_called()  # defensive: never re-parent into the live epic
    mock_run.assert_not_called()


def test_apply_drain_ensures_archive_once_per_quarter() -> None:
    # Many closed children in the SAME quarter, all fitting under the cap, ensure
    # that quarter's segment 1 exactly ONCE, then re-parent each child.
    live = IssueRef("org", ".github", 40)
    arch = IssueRef("org", ".github", 88)
    kids = [IssueRef("org", ".github", n) for n in (101, 102, 103)]
    plan = epics.DrainPlan(
        live=live,
        moves=[(k, "2026-Q2") for k in kids],
        close=[],
    )
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._standing_archive_segments", return_value=[]),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=0),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=arch) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", "tooling", "2026-Q2", segment=1)
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
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._standing_archive_segments", return_value=[]),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=0),
        patch(
            "vergil_tooling.lib.epics.ensure_standing_archive",
            side_effect=lambda _kind, _home, _name, q, segment=1: archives[q],
        ) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    assert mock_ens.call_count == 2
    assert {c.args[3] for c in mock_ens.call_args_list} == {"2026-Q2", "2026-Q3"}
    assert mock_reparent.call_count == 3


def test_archive_child_count_returns_native_child_count() -> None:
    arch = IssueRef("org", ".github", 88)
    kids = [ChildState(IssueRef("org", ".github", n), "CLOSED", "t", "") for n in (1, 2, 3)]
    with patch("vergil_tooling.lib.epics._native_child_states", return_value=kids):
        assert epics._archive_child_count(arch) == 3


def test_apply_drain_rolls_to_new_segment_when_head_full() -> None:
    # The bug case: the quarter's segment 1 already holds 100 children (GitHub's
    # cap), so the next child must roll into a freshly created segment 2 — never
    # re-parented into the full segment 1.
    live = IssueRef("org", ".github", 40)
    seg1 = IssueRef("org", ".github", 249)  # full
    seg2 = IssueRef("org", ".github", 250)  # created on roll
    child = IssueRef("org", ".github", 2860)
    plan = epics.DrainPlan(live=live, moves=[(child, "2026-Q2")], close=[])
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._standing_archive_segments", return_value=[(1, seg1)]),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=100),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=seg2) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", "tooling", "2026-Q2", segment=2)
    mock_reparent.assert_called_once_with(seg2, child)


def test_apply_drain_fills_head_then_rolls() -> None:
    # Segment 1 has room for one more (occupancy 94, cap 95); the batch fills it,
    # then rolls the remainder into a newly ensured segment 2.
    live = IssueRef("org", ".github", 40)
    seg1 = IssueRef("org", ".github", 249)
    seg2 = IssueRef("org", ".github", 250)
    kids = [IssueRef("org", ".github", n) for n in (301, 302, 303)]
    plan = epics.DrainPlan(live=live, moves=[(k, "2026-Q2") for k in kids], close=[])
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._standing_archive_segments", return_value=[(1, seg1)]),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=94),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=seg2) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    # seg1 came from discovery (never ensured); only the rolled segment 2 is ensured, once.
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", "tooling", "2026-Q2", segment=2)
    assert [c.args for c in mock_reparent.call_args_list] == [
        (seg1, kids[0]),
        (seg2, kids[1]),
        (seg2, kids[2]),
    ]


def test_apply_drain_writes_to_highest_existing_segment_without_rolling() -> None:
    # Two segments already exist; the head is the highest with room, and no new
    # segment is created when the batch fits.
    live = IssueRef("org", ".github", 40)
    seg1 = IssueRef("org", ".github", 249)
    seg2 = IssueRef("org", ".github", 250)
    child = IssueRef("org", ".github", 400)
    plan = epics.DrainPlan(live=live, moves=[(child, "2026-Q2")], close=[])
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch(
            "vergil_tooling.lib.epics._standing_archive_segments",
            return_value=[(1, seg1), (2, seg2)],
        ),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=10),
        patch("vergil_tooling.lib.epics.ensure_standing_archive") as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.apply_adhoc_drain("org/tooling", plan)
    mock_ens.assert_not_called()  # head resolved from discovery; batch fits, no roll
    mock_reparent.assert_called_once_with(seg2, child)


def test_adhoc_archive_head_returns_highest_existing_segment() -> None:
    seg1 = IssueRef("org", ".github", 249)
    seg2 = IssueRef("org", ".github", 250)
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch(
            "vergil_tooling.lib.epics._standing_archive_segments",
            return_value=[(1, seg1), (2, seg2)],
        ),
        patch("vergil_tooling.lib.epics.ensure_standing_archive") as mock_ens,
    ):
        assert epics._standing_archive_head(epics.ADHOC, "org/.github", "tooling", "2026-Q3") == (
            2,
            seg2,
        )
    mock_ens.assert_not_called()  # existing segment reused, never created


def test_adhoc_archive_head_ensures_segment_one_when_none() -> None:
    seg1 = IssueRef("org", ".github", 88)
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics._standing_archive_segments", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=seg1) as mock_ens,
    ):
        assert epics._standing_archive_head(epics.ADHOC, "org/.github", "tooling", "2026-Q3") == (
            1,
            seg1,
        )
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", "tooling", "2026-Q3", segment=1)


def test_ensure_writable_adhoc_archive_returns_head_when_room() -> None:
    seg2 = IssueRef("org", ".github", 250)
    with (
        patch("vergil_tooling.lib.epics._standing_archive_head", return_value=(2, seg2)),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=10),
        patch("vergil_tooling.lib.epics.ensure_standing_archive") as mock_ens,
    ):
        assert (
            epics.ensure_writable_standing_archive(epics.ADHOC, "org/.github", "tooling", "2026-Q3")
            == seg2
        )
    mock_ens.assert_not_called()  # head has room; no roll


def test_ensure_writable_adhoc_archive_rolls_when_head_full() -> None:
    seg2 = IssueRef("org", ".github", 250)
    seg3 = IssueRef("org", ".github", 251)
    with (
        patch("vergil_tooling.lib.epics._standing_archive_head", return_value=(2, seg2)),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=95),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=seg3) as mock_ens,
    ):
        assert (
            epics.ensure_writable_standing_archive(epics.ADHOC, "org/.github", "tooling", "2026-Q3")
            == seg3
        )
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", "tooling", "2026-Q3", segment=3)


def test_rollup_rolls_into_new_segment_when_head_full() -> None:
    # The steady-state on:issues.closed path must also spill past the 100-cap: a
    # single closed child whose quarter's head segment is full re-parents into a
    # freshly rolled segment, not the full one (issue #2872).
    task = IssueRef("org", ".github", 2860)
    live = IssueRef("org", ".github", 40)
    seg2 = IssueRef("org", ".github", 250)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=live),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "ad-hoc"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (ad hoc): tooling"),
        patch("vergil_tooling.lib.github.repo_exists", return_value=True),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value="2026-05-01T00:00:00Z"),
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch(
            "vergil_tooling.lib.epics.ensure_writable_standing_archive", return_value=seg2
        ) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(task)
    mock_ens.assert_called_once_with(epics.ADHOC, "org/.github", "tooling", "2026-Q2")
    mock_reparent.assert_called_once_with(seg2, task)


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


# -- in-quarter archive segments (issue #2872): overflow past 100 sub-issues --


def test_segment_placements_stays_in_segment_below_cap() -> None:
    # Room in the head segment: every child lands there, none roll.
    assert epics._segment_placements(1, 0, 3, 95) == [1, 1, 1]


def test_segment_placements_rolls_when_head_reaches_cap() -> None:
    # Head at occupancy 94, cap 95: one more fills it, the rest roll to segment 2.
    assert epics._segment_placements(1, 94, 3, 95) == [1, 2, 2]


def test_segment_placements_rolls_immediately_when_head_already_full() -> None:
    # The bug case: head is a full (100/100) segment 1; every child rolls forward.
    assert epics._segment_placements(1, 100, 2, 95) == [2, 2]


def test_segment_placements_spans_multiple_new_segments() -> None:
    # A large batch cascades across several fresh segments, each capped.
    assert epics._segment_placements(2, 0, 5, 2) == [2, 2, 3, 3, 4]


def test_segment_placements_empty_batch_places_nothing() -> None:
    assert epics._segment_placements(1, 0, 0, 95) == []


def test_archive_regex_parses_segment_suffix() -> None:
    m = epics.archive_re(epics.ADHOC).match("Archive (ad hoc): tooling — 2026-Q3 (2)")
    assert m and m.group("name") == "tooling"
    assert m.group("quarter") == "2026-Q3" and m.group("segment") == "2"


def test_archive_regex_segment_absent_on_unsuffixed_title() -> None:
    m = epics.archive_re(epics.ADHOC).match("Archive (ad hoc): tooling — 2026-Q3")
    assert m and m.group("segment") is None


def test_archive_title_segment_one_is_unsuffixed() -> None:
    assert (
        epics.archive_title(epics.ADHOC, "tooling", "2026-Q3", 1)
        == "Archive (ad hoc): tooling — 2026-Q3"
    )


def test_archive_title_later_segments_carry_suffix() -> None:
    assert (
        epics.archive_title(epics.ADHOC, "tooling", "2026-Q3", 2)
        == "Archive (ad hoc): tooling — 2026-Q3 (2)"
    )


def test_ensure_adhoc_archive_creates_segment_two_with_suffix() -> None:
    created = "https://github.com/org/.github/issues/92"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels"),
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        ref = epics.ensure_standing_archive(
            epics.ADHOC, "org/.github", "tooling", "2026-Q3", segment=2
        )
    assert ref == IssueRef("org", ".github", 92)
    assert mock_create.call_args.kwargs["title"] == "Archive (ad hoc): tooling — 2026-Q3 (2)"


def test_ensure_adhoc_archive_segment_two_skips_legacy_normalize() -> None:
    # Only segment 1 has a legacy pre-rename form; a >1 segment must never adopt
    # a legacy issue — it creates its own suffixed archive.
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels"),
        patch("vergil_tooling.lib.epics._normalize_archive_in_place") as mock_norm,
        patch(
            "vergil_tooling.lib.github.create_issue",
            return_value="https://github.com/org/.github/issues/92",
        ),
    ):
        epics.ensure_standing_archive(epics.ADHOC, "org/.github", "tooling", "2026-Q3", segment=2)
    mock_norm.assert_not_called()


def test_adhoc_archive_segments_lists_sorted_for_quarter() -> None:
    rows = [
        {"number": 91, "title": "Archive (ad hoc): tooling — 2026-Q3 (2)"},
        {"number": 88, "title": "Archive (ad hoc): tooling — 2026-Q3"},
        {"number": 70, "title": "Archive (ad hoc): tooling — 2026-Q2"},  # other quarter
        {"number": 71, "title": "Archive (ad hoc): other — 2026-Q3"},  # other repo
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows):
        got = epics._standing_archive_segments(epics.ADHOC, "org/.github", "tooling", "2026-Q3")
    assert got == [(1, IssueRef("org", ".github", 88)), (2, IssueRef("org", ".github", 91))]


def test_adhoc_archive_segments_empty_when_none_for_quarter() -> None:
    rows = [{"number": 70, "title": "Archive (ad hoc): tooling — 2026-Q2"}]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows):
        assert (
            epics._standing_archive_segments(epics.ADHOC, "org/.github", "tooling", "2026-Q3") == []
        )


def test_list_open_adhoc_archives_includes_segmented_titles() -> None:
    rows = [
        {"number": 88, "title": "Archive (ad hoc): tooling — 2026-Q3"},
        {"number": 91, "title": "Archive (ad hoc): tooling — 2026-Q3 (2)"},
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows):
        got = epics.list_open_standing_archives(epics.ADHOC, "org/.github")
    assert (IssueRef("org", ".github", 88), "2026-Q3") in got
    assert (IssueRef("org", ".github", 91), "2026-Q3") in got


# -- standing kinds: intake / planning / deferred queues (epic .github#176, #508) --


def test_standing_kinds_table() -> None:
    assert epics.STANDING_KINDS == (epics.ADHOC, epics.INTAKE, epics.PLANNING, epics.DEFERRED)
    assert epics.ADHOC.title_prefix == "Epic (ad hoc): "
    assert epics.ADHOC.labels == ("epic", "ad-hoc")
    assert epics.ADHOC.archive_prefix == "Archive (ad hoc): "
    assert epics.ADHOC.archive_labels == ("archive", "ad-hoc")
    assert epics.INTAKE.title_prefix == "Epic (intake): "
    assert epics.INTAKE.labels == ("epic", "intake")
    assert epics.INTAKE.archive_prefix == "Archive (intake): "
    assert epics.INTAKE.archive_labels == ("archive", "intake")
    assert epics.PLANNING.title_prefix == "Epic (planning): "
    assert epics.PLANNING.labels == ("epic", "planning")
    assert epics.PLANNING.archive_prefix == "Archive (planning): "
    assert epics.PLANNING.archive_labels == ("archive", "planning")
    assert [k.key for k in epics.STANDING_KINDS] == ["ad-hoc", "intake", "planning", "deferred"]


def test_standing_kind_labels_are_in_registry() -> None:
    # ensure_standing_epic self-heals its labels from the canonical registry, so
    # every standing and archive label (and the planning in-progress label) must
    # be registered, or ensure_labels raises UnknownLabelError.
    names = {label["name"] for label in load_labels()["labels"]}
    for kind in epics.STANDING_KINDS:
        assert set(kind.labels) <= names
        assert set(kind.archive_labels) <= names
    assert "in-progress" in names


def test_queue_home_public_repo_uses_org_dotgithub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epics, "resolve_epic_home", lambda org, bare: "org/.github")
    assert epics.queue_home("org/tool") == ("org/.github", "org")


def test_queue_home_private_repo_self_homes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epics, "resolve_epic_home", lambda org, bare: "org/secret")
    assert epics.queue_home("org/secret") == ("org/secret", "secret")


def test_queue_home_dotgithub_itself_names_org(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(epics, "resolve_epic_home", lambda org, bare: "org/.github")
    assert epics.queue_home("org/.github") == ("org/.github", "org")


def test_queue_home_repo_without_owner_raises() -> None:
    with pytest.raises(ValueError, match="cannot resolve repo"):
        epics.queue_home("tool")


def test_ensure_intake_epic_reuses_by_title_and_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_find(
        home: str,
        title: str,
        *,
        prefer_oldest: bool = False,
        labels: tuple[str, ...] = ("epic", "ad-hoc"),
    ) -> IssueRef:
        seen.update(home=home, title=title, labels=labels)
        return IssueRef("org", ".github", 7)

    monkeypatch.setattr(epics, "queue_home", lambda r: ("org/.github", "org"))
    monkeypatch.setattr(epics, "_find_epic_by_title", fake_find)
    with patch("vergil_tooling.lib.github.create_issue") as mock_create:
        assert epics.ensure_intake_epic("org/tool") == IssueRef("org", ".github", 7)
    mock_create.assert_not_called()
    assert seen == {
        "home": "org/.github",
        "title": "Epic (intake): org",
        "labels": ("epic", "intake"),
    }


def test_ensure_intake_epic_private_repo_creates_in_self() -> None:
    created = "https://github.com/org/secret/issues/4"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/secret"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        assert epics.ensure_intake_epic("org/secret") == IssueRef("org", "secret", 4)
    mock_ensure.assert_called_once_with("org/secret", ("epic", "intake"))
    kwargs = mock_create.call_args.kwargs
    assert kwargs["repo"] == "org/secret"
    assert kwargs["title"] == "Epic (intake): secret"
    assert kwargs["labels"] == ["epic", "intake"]
    assert kwargs["body"].startswith("Perpetual intake queue for secret:")


def test_ensure_planning_epic_creates_when_absent() -> None:
    created = "https://github.com/org/.github/issues/90"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        assert epics.ensure_planning_epic("org/tool") == IssueRef("org", ".github", 90)
    kwargs = mock_create.call_args.kwargs
    assert kwargs["repo"] == "org/.github"
    assert kwargs["title"] == "Epic (planning): org"
    assert kwargs["labels"] == ["epic", "planning"]
    assert kwargs["body"].startswith("Perpetual planning queue for org:")
    ensured = [c.args for c in mock_ensure.call_args_list]
    assert ("org/.github", ("epic", "planning")) in ensured
    assert ("org/.github", ("in-progress",)) in ensured


def test_ensure_planning_epic_ensures_in_progress_label() -> None:
    # The per-item planning label is ensured in the planning home even when the
    # planning epic already exists, so epic-create can always apply it.
    rows = [{"number": 90, "title": "Epic (planning): org"}]
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch("vergil_tooling.lib.github.create_issue") as mock_create,
    ):
        assert epics.ensure_planning_epic("org/tool") == IssueRef("org", ".github", 90)
    mock_create.assert_not_called()
    assert [c.args for c in mock_ensure.call_args_list] == [
        ("org/.github", ("in-progress",)),
        ("org/tool", ("in-progress",)),
    ]


def test_ensure_planning_epic_ensures_in_progress_label_in_item_repo() -> None:
    # A planning item can live in a member repo while the queue homes in
    # <org>/.github; epic-create labels the item itself, so the member repo
    # needs the label too, not just the home (#3520).
    created = "https://github.com/org/.github/issues/90"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch("vergil_tooling.lib.github.create_issue", return_value=created),
    ):
        epics.ensure_planning_epic("org/tool")
    ensured = [c.args for c in mock_ensure.call_args_list]
    assert ("org/tool", ("in-progress",)) in ensured


def test_ensure_planning_epic_self_homed_ensures_label_once() -> None:
    # A self-homed repo is both the home and the item's repo: one ensure, not two.
    rows = [{"number": 3, "title": "Epic (planning): secret"}]
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/secret"),
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
    ):
        assert epics.ensure_planning_epic("org/secret") == IssueRef("org", "secret", 3)
    mock_ensure.assert_called_once_with("org/secret", ("in-progress",))


def test_ensure_standing_epic_duplicate_title_raises_naming_kind() -> None:
    rows = [
        {"number": 1, "title": "Epic (intake): org"},
        {"number": 2, "title": "Epic (intake): org"},
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=rows),
        pytest.raises(ValueError, match="multiple intake epics"),
    ):
        epics.ensure_standing_epic(epics.INTAKE, "org/.github", "org")


def test_is_standing() -> None:
    assert epics.is_standing({"epic", "intake"})
    assert epics.is_standing({"epic", "planning"})
    assert epics.is_standing({"epic", "ad-hoc"})
    assert not epics.is_standing({"epic"})
    assert not epics.is_standing({"archive", "intake"})
    assert not epics.is_standing(set())


def test_standing_kind_of() -> None:
    assert epics.standing_kind_of({"epic", "intake"}) is epics.INTAKE
    assert epics.standing_kind_of({"epic", "planning", "bug"}) is epics.PLANNING
    assert epics.standing_kind_of({"epic", "ad-hoc"}) is epics.ADHOC
    assert epics.standing_kind_of({"epic"}) is None
    assert epics.standing_kind_of({"archive", "planning"}) is None


def test_is_legitimate_top_level() -> None:
    assert epics.is_legitimate_top_level({"epic"}, "x", "")
    archive_title = "Archive (ad hoc): t — 2026-Q4"
    assert epics.is_legitimate_top_level({"archive", "ad-hoc"}, archive_title, "")
    assert epics.is_legitimate_top_level(set(), "release: 2.1.258", "")
    assert epics.is_legitimate_top_level(set(), "anything", "<!-- vrg-release:progress -->")
    assert not epics.is_legitimate_top_level({"bug"}, "fix: thing", "")
    assert not epics.is_legitimate_top_level(set(), "fix: thing", "plain body")


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({"epic", "intake"}, "intake"),
        ({"epic", "planning"}, "planning"),
        ({"epic", "ad-hoc"}, None),
        ({"archive", "intake"}, None),  # archives are not queues
        ({"epic"}, None),
    ],
)
def test_queue_kind_reads_epic_labels(labels: set[str], expected: str | None) -> None:
    with patch("vergil_tooling.lib.epics._labels", return_value=labels) as mock_labels:
        assert epics.queue_kind(EPIC) == expected
    mock_labels.assert_called_once_with(EPIC)


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ({"epic", "intake"}, "intake"),
        ({"epic", "planning"}, "planning"),
        ({"epic", "ad-hoc"}, None),  # a routed task under ad-hoc is buildable
        ({"epic"}, None),  # a task under a finite epic is buildable
        ({"archive", "intake"}, None),  # a drained, closed item is not queued
    ],
)
def test_queue_item_kind_reads_native_parent_labels(labels: set[str], expected: str | None) -> None:
    with (
        patch("vergil_tooling.lib.epics.native_parent_of", return_value=EPIC) as mock_parent,
        patch("vergil_tooling.lib.epics._labels", return_value=labels) as mock_labels,
    ):
        assert epics.queue_item_kind("org/repo-a#101", default_repo="org/x") == expected
    mock_parent.assert_called_once_with(IssueRef("org", "repo-a", 101))
    mock_labels.assert_called_once_with(EPIC)


def test_queue_item_kind_bare_ref_uses_default_repo() -> None:
    with (
        patch("vergil_tooling.lib.epics.native_parent_of", return_value=EPIC) as mock_parent,
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "intake"}),
    ):
        assert epics.queue_item_kind("#101", default_repo="org/repo-a") == "intake"
    mock_parent.assert_called_once_with(TASK)


def test_queue_item_kind_unparented_is_none() -> None:
    with (
        patch("vergil_tooling.lib.epics.native_parent_of", return_value=None),
        patch("vergil_tooling.lib.epics._labels") as mock_labels,
    ):
        assert epics.queue_item_kind("#101", default_repo="org/repo-a") is None
    mock_labels.assert_not_called()


def test_queue_item_kind_unparseable_ref_is_none() -> None:
    with patch("vergil_tooling.lib.epics.native_parent_of") as mock_parent:
        assert epics.queue_item_kind("#101", default_repo="") is None
        assert epics.queue_item_kind("not-a-ref", default_repo="org/repo") is None
    mock_parent.assert_not_called()


def test_queue_item_kind_propagates_lookup_failure() -> None:
    # A failed lookup is never read as "not queued": it raises, and the caller
    # decides (report-ready defers loudly; vrg-submit-pr fails).
    with (
        patch("vergil_tooling.lib.epics.native_parent_of", side_effect=OSError("gh")),
        pytest.raises(OSError, match="gh"),
    ):
        epics.queue_item_kind("#101", default_repo="org/repo-a")


def test_queue_refusal_message_intake() -> None:
    assert epics.queue_refusal_message("#7", "intake") == (
        "--issue (#7) is in the intake queue; it is not buildable until it is routed. "
        "Route it with intake-review first."
    )


def test_queue_refusal_message_planning() -> None:
    assert epics.queue_refusal_message("org/repo#7", "planning") == (
        "--issue (org/repo#7) is in the planning queue; it is not buildable until its "
        "epic is expanded. Expand it with epic-create first."
    )


def test_queue_refusal_message_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="not a queue kind: 'ad-hoc'"):
        epics.queue_refusal_message("#7", "ad-hoc")


def test_is_managed_repo_true_when_vergil_toml_present() -> None:
    with patch(
        "vergil_tooling.lib.github.read_json", return_value={"name": "vergil.toml"}
    ) as mock_read:
        assert epics.is_managed_repo("org/tool") is True
    assert mock_read.call_args.args == ("api", "repos/org/tool/contents/vergil.toml")


def test_is_managed_repo_false_on_404() -> None:
    err = github.GitHubAPIError(1, ["gh", "api"], "", "gh: Not Found (HTTP 404)")
    with patch("vergil_tooling.lib.github.read_json", side_effect=err):
        assert epics.is_managed_repo("org/tool") is False


def test_is_managed_repo_propagates_other_api_errors() -> None:
    err = github.GitHubAPIError(1, ["gh", "api"], "", "HTTP 502: bad gateway")
    with (
        patch("vergil_tooling.lib.github.read_json", side_effect=err),
        pytest.raises(github.GitHubAPIError),
    ):
        epics.is_managed_repo("org/tool")


def test_native_parent_of_returns_native_parent() -> None:
    data = {"node": {"parent": {"number": 40, "repository": _repo_node("org", ".github")}}}
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=data),
    ):
        assert epics.native_parent_of(TASK) == IssueRef("org", ".github", 40)


def test_native_parent_of_ignores_body_parent_line() -> None:
    no_parent = {"node": {"parent": None}}
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=no_parent),
        patch(
            "vergil_tooling.lib.github.read_output", return_value="Parent: org/.github#5\n"
        ) as mock_body,
    ):
        assert epics.native_parent_of(TASK) is None
    mock_body.assert_not_called()


def test_native_parent_of_none_when_graphql_returns_nothing() -> None:
    with (
        patch("vergil_tooling.lib.epics._node_id", return_value="NODE"),
        patch("vergil_tooling.lib.github.graphql", return_value=None),
    ):
        assert epics.native_parent_of(TASK) is None


def test_resolve_orphan_epic_is_the_intake_epic() -> None:
    intake = IssueRef("org", ".github", 7)
    with patch("vergil_tooling.lib.epics.ensure_intake_epic", return_value=intake) as mock_in:
        assert epics.resolve_orphan_epic("org/tool") == intake
    mock_in.assert_called_once_with("org/tool")


def test_resolve_epic_ref_intake_and_planning_sentinels() -> None:
    intake = IssueRef("org", ".github", 7)
    planning = IssueRef("org", ".github", 8)
    with (
        patch("vergil_tooling.lib.epics.ensure_intake_epic", return_value=intake) as mock_in,
        patch("vergil_tooling.lib.epics.ensure_planning_epic", return_value=planning) as mock_pl,
        patch("vergil_tooling.lib.epics.is_epic") as mock_is_epic,
    ):
        assert epics.resolve_epic_ref("intake", repo="org/tool") == intake
        assert epics.resolve_epic_ref("planning", repo="org/tool") == planning
    mock_in.assert_called_once_with("org/tool")
    mock_pl.assert_called_once_with("org/tool")
    mock_is_epic.assert_not_called()


@pytest.mark.parametrize("kind_label", ["intake", "planning"])
def test_rollup_never_closes_intake_or_planning_epic(kind_label: str) -> None:
    live = IssueRef("org", ".github", 7)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=live),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", kind_label}),
        patch("vergil_tooling.lib.epics.all_children_closed", return_value=True) as mock_all,
        patch("vergil_tooling.lib.epics.drain_standing_child") as mock_drain,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(TASK)
    mock_run.assert_not_called()
    mock_all.assert_not_called()
    # Perpetual: never closed — the closed child drains into an archive instead.
    kind = epics.INTAKE if kind_label == "intake" else epics.PLANNING
    mock_drain.assert_called_once_with(kind, live, TASK)


# -- generalized archive drain: intake / planning (epic vergil-project/.github#176, Task 2) --

Q4_NOW = datetime(2026, 10, 23, tzinfo=UTC)  # 2026-Q4


def test_archive_title_intake() -> None:
    assert epics.archive_title(epics.INTAKE, "org", "2026-Q4") == "Archive (intake): org — 2026-Q4"
    assert (
        epics.archive_title(epics.INTAKE, "org", "2026-Q4", segment=2)
        == "Archive (intake): org — 2026-Q4 (2)"
    )


def test_archive_title_planning() -> None:
    assert (
        epics.archive_title(epics.PLANNING, "secret", "2026-Q4")
        == "Archive (planning): secret — 2026-Q4"
    )


def test_archive_title_adhoc_unchanged() -> None:
    # The ad-hoc output is byte-identical to the pre-generalization form.
    assert epics.archive_title(epics.ADHOC, "tool", "2026-Q4") == "Archive (ad hoc): tool — 2026-Q4"
    assert (
        epics.archive_title(epics.ADHOC, "tool", "2026-Q4", 3)
        == "Archive (ad hoc): tool — 2026-Q4 (3)"
    )


def test_archive_re_is_per_kind() -> None:
    m = epics.archive_re(epics.INTAKE).match("Archive (intake): org — 2026-Q4 (2)")
    assert m and m.group("name") == "org"
    assert m.group("quarter") == "2026-Q4" and m.group("segment") == "2"
    # Each kind recognizes only its own archives, never another kind's or a live epic.
    assert epics.archive_re(epics.INTAKE).match("Archive (planning): org — 2026-Q4") is None
    assert epics.archive_re(epics.PLANNING).match("Archive (intake): org — 2026-Q4") is None
    assert epics.archive_re(epics.ADHOC).match("Archive (intake): org — 2026-Q4") is None
    assert epics.archive_re(epics.INTAKE).match("Epic (intake): org") is None


def test_ensure_standing_archive_intake_creates_with_kind_labels_and_body() -> None:
    created = "https://github.com/org/.github/issues/501"
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=[]) as mock_list,
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_labels,
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        ref = epics.ensure_standing_archive(epics.INTAKE, "org/.github", "org", "2026-Q4")
    assert ref == IssueRef("org", ".github", 501)
    kwargs = mock_create.call_args.kwargs
    assert kwargs["repo"] == "org/.github"
    assert kwargs["title"] == "Archive (intake): org — 2026-Q4"
    assert kwargs["labels"] == ["archive", "intake"]
    assert kwargs["body"] == "Intake items for org closed in 2026-Q4. Managed automatically.\n"
    mock_labels.assert_called_once_with("org/.github", ("archive", "intake"))
    # Only the new-form lookup ran: intake has no legacy archive form to heal.
    assert mock_list.call_count == 1
    args = list(mock_list.call_args.args)
    assert "intake" in args and "ad-hoc" not in args


def test_ensure_standing_archive_adhoc_body_unchanged() -> None:
    created = "https://github.com/org/.github/issues/88"
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels"),
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        epics.ensure_standing_archive(epics.ADHOC, "org/.github", "tooling", "2026-Q3")
    assert (
        mock_create.call_args.kwargs["body"]
        == "Ad-hoc work in org/tooling finished in 2026-Q3. Managed automatically.\n"
    )


def test_standing_archive_segments_intake_filters_by_name_and_quarter() -> None:
    rows = [
        {"number": 12, "title": "Archive (intake): org — 2026-Q4 (2)"},
        {"number": 11, "title": "Archive (intake): org — 2026-Q4"},
        {"number": 10, "title": "Archive (intake): org — 2026-Q3"},  # other quarter
        {"number": 9, "title": "Epic (intake): org"},  # live epic, not an archive
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows) as mock_list:
        got = epics._standing_archive_segments(epics.INTAKE, "org/.github", "org", "2026-Q4")
    assert got == [(1, IssueRef("org", ".github", 11)), (2, IssueRef("org", ".github", 12))]
    args = list(mock_list.call_args.args)
    assert "archive" in args and "intake" in args


def test_list_open_standing_archives_planning() -> None:
    rows = [
        {"number": 20, "title": "Archive (planning): org — 2026-Q3"},
        {"number": 21, "title": "Epic (planning): org"},
    ]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows):
        got = epics.list_open_standing_archives(epics.PLANNING, "org/.github")
    assert got == [(IssueRef("org", ".github", 20), "2026-Q3")]


def test_plan_standing_drain_intake() -> None:
    live = IssueRef("org", ".github", 9)
    kids = [
        ChildState(IssueRef("org", "tool", 1), "CLOSED", "t", "2026-10-23T00:00:00Z"),
        ChildState(IssueRef("org", "tool", 2), "OPEN", "t", ""),
    ]
    with (
        patch("vergil_tooling.lib.epics.find_standing_epic", return_value=live) as mock_find,
        patch("vergil_tooling.lib.epics.child_states", return_value=kids),
        patch(
            "vergil_tooling.lib.epics.list_open_standing_archives",
            return_value=[
                (IssueRef("org", ".github", 30), "2026-Q3"),
                (IssueRef("org", ".github", 31), "2026-Q4"),
            ],
        ) as mock_archives,
    ):
        plan = epics.plan_standing_drain(epics.INTAKE, "org/.github", "org", now=Q4_NOW)
    assert plan == epics.DrainPlan(
        live=live,
        moves=[(IssueRef("org", "tool", 1), "2026-Q4")],
        close=[IssueRef("org", ".github", 30)],
    )
    mock_find.assert_called_once_with(epics.INTAKE, "org/.github", "org")
    mock_archives.assert_called_once_with(epics.INTAKE, "org/.github")


def test_find_standing_epic_uses_kind_title_and_labels() -> None:
    rows = [{"number": 9, "title": "Epic (planning): org"}]
    with patch("vergil_tooling.lib.github.read_json", return_value=rows) as mock_list:
        got = epics.find_standing_epic(epics.PLANNING, "org/.github", "org")
    assert got == IssueRef("org", ".github", 9)
    args = list(mock_list.call_args.args)
    assert "epic" in args and "planning" in args


_INTAKE_PARENT = IssueRef("org", ".github", 9)


def test_rollup_drains_closed_intake_child_into_quarter_archive() -> None:
    task = IssueRef("org", "tool", 1)
    arch = IssueRef("org", ".github", 501)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=_INTAKE_PARENT),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "intake"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (intake): org"),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value="2026-10-23T00:00:00Z"),
        patch("vergil_tooling.lib.epics.resolve_epic_home") as mock_home,
        patch(
            "vergil_tooling.lib.epics.ensure_writable_standing_archive", return_value=arch
        ) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(task)
    mock_ens.assert_called_once_with(epics.INTAKE, "org/.github", "org", "2026-Q4")
    mock_reparent.assert_called_once_with(arch, task)
    mock_run.assert_not_called()  # the parent queue epic is never closed
    mock_home.assert_not_called()  # a queue archive is the sibling of its queue epic


def test_rollup_drains_closed_planning_child_in_private_self_home() -> None:
    # A private repo's self-homed planning epic is named for the bare repo and its
    # archive lands beside it, in that repo — never in .github.
    parent = IssueRef("org", "secret", 5)
    task = IssueRef("org", "secret", 6)
    arch = IssueRef("org", "secret", 77)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=parent),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "planning"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (planning): secret"),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value="2026-08-01T00:00:00Z"),
        patch(
            "vergil_tooling.lib.epics.ensure_writable_standing_archive", return_value=arch
        ) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(task)
    mock_ens.assert_called_once_with(epics.PLANNING, "org/secret", "secret", "2026-Q3")
    mock_reparent.assert_called_once_with(arch, task)


def test_rollup_skips_noncanonical_intake_epic_loudly(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=_INTAKE_PARENT),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "intake"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (intake): elsewhere"),
        patch("vergil_tooling.lib.epics._issue_closed_at") as mock_closed,
        patch("vergil_tooling.lib.epics.ensure_writable_standing_archive") as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(TASK)
    mock_ens.assert_not_called()
    mock_reparent.assert_not_called()
    mock_closed.assert_not_called()
    err = capsys.readouterr().err
    assert "skipped intake drain" in err and "'Epic (intake): org'" in err


def test_rollup_intake_child_without_closed_at_is_noop() -> None:
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=_INTAKE_PARENT),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "intake"}),
        patch("vergil_tooling.lib.epics._issue_title", return_value="Epic (intake): org"),
        patch("vergil_tooling.lib.epics._issue_closed_at", return_value=""),
        patch("vergil_tooling.lib.epics.ensure_writable_standing_archive") as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
    ):
        epics.rollup(TASK)
    mock_ens.assert_not_called()
    mock_reparent.assert_not_called()


@pytest.mark.parametrize("kind_label", ["intake", "planning"])
def test_rollup_leaves_intake_archive_terminal(kind_label: str) -> None:
    # An archive carries 'archive' + the kind key and no 'epic', so it is terminal:
    # no drain, no close.
    archive = IssueRef("org", ".github", 501)
    with (
        patch("vergil_tooling.lib.epics.parent_of", return_value=archive),
        patch("vergil_tooling.lib.epics._labels", return_value={"archive", kind_label}),
        patch("vergil_tooling.lib.epics.drain_standing_child") as mock_drain,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.rollup(TASK)
    mock_drain.assert_not_called()
    mock_reparent.assert_not_called()
    mock_run.assert_not_called()


def test_drain_respects_segment_cap() -> None:
    # Single-child path: the intake head archive is at the 95-child cap, so the
    # next child goes to segment 2 of the same quarter.
    seg1 = IssueRef("org", ".github", 501)
    seg2 = IssueRef("org", ".github", 502)
    with (
        patch(
            "vergil_tooling.lib.epics._standing_archive_segments", return_value=[(1, seg1)]
        ) as mock_segments,
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=95),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=seg2) as mock_ens,
    ):
        got = epics.ensure_writable_standing_archive(epics.INTAKE, "org/.github", "org", "2026-Q4")
    assert got == seg2
    mock_segments.assert_called_once_with(epics.INTAKE, "org/.github", "org", "2026-Q4")
    mock_ens.assert_called_once_with(epics.INTAKE, "org/.github", "org", "2026-Q4", segment=2)


def test_apply_standing_drain_intake_fills_then_rolls() -> None:
    # Batch path: head at 94 takes one child, the rest roll into segment 2; the
    # past archive is closed.
    live = IssueRef("org", ".github", 9)
    seg1 = IssueRef("org", ".github", 501)
    seg2 = IssueRef("org", ".github", 502)
    past = IssueRef("org", ".github", 400)
    kids = [IssueRef("org", "tool", n) for n in (1, 2)]
    plan = epics.DrainPlan(live=live, moves=[(k, "2026-Q4") for k in kids], close=[past])
    with (
        patch("vergil_tooling.lib.epics._standing_archive_segments", return_value=[(1, seg1)]),
        patch("vergil_tooling.lib.epics._archive_child_count", return_value=94),
        patch("vergil_tooling.lib.epics.ensure_standing_archive", return_value=seg2) as mock_ens,
        patch("vergil_tooling.lib.epics.reparent_child") as mock_reparent,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        epics.apply_standing_drain(epics.INTAKE, "org/.github", "org", plan)
    mock_ens.assert_called_once_with(epics.INTAKE, "org/.github", "org", "2026-Q4", segment=2)
    assert [c.args for c in mock_reparent.call_args_list] == [(seg1, kids[0]), (seg2, kids[1])]
    assert mock_run.call_args.args == ("issue", "close", "400", "--repo", "org/.github")


def test_drain_standing_org_covers_dotgithub_and_private_self_homes() -> None:
    # list_org_repos -> [".github", "tool", "secret"]; secret is private while
    # .github is public, so it self-homes its own intake/planning pair.
    seen: list[tuple[str, str, str]] = []

    def fake_plan(
        kind: epics.StandingKind, home: str, name: str, *, now: datetime
    ) -> epics.DrainPlan:
        seen.append((kind.key, home, name))
        owner, repo = home.split("/", 1)
        return epics.DrainPlan(IssueRef(owner, repo, 1), moves=[], close=[])

    repos = [".github", "tool", "secret"]
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=repos),
        patch(
            "vergil_tooling.lib.github.is_public",
            side_effect=_visibility({"org/.github", "org/tool"}),
        ),
        patch("vergil_tooling.lib.epics.plan_standing_drain", side_effect=fake_plan),
        patch("vergil_tooling.lib.epics.apply_standing_drain") as mock_apply,
    ):
        plans = epics.drain_standing_org("org", apply=True, now=Q4_NOW)
    assert seen == [
        ("intake", "org/.github", "org"),
        ("planning", "org/.github", "org"),
        ("deferred", "org/.github", "org"),
        ("intake", "org/secret", "secret"),
        ("planning", "org/secret", "secret"),
        ("deferred", "org/secret", "secret"),
    ]
    assert len(plans) == 6
    assert [c.args[:3] for c in mock_apply.call_args_list] == [
        (epics.INTAKE, "org/.github", "org"),
        (epics.PLANNING, "org/.github", "org"),
        (epics.DEFERRED, "org/.github", "org"),
        (epics.INTAKE, "org/secret", "secret"),
        (epics.PLANNING, "org/secret", "secret"),
        (epics.DEFERRED, "org/secret", "secret"),
    ]


def test_drain_standing_org_dry_run_never_applies_and_drops_absent_epics() -> None:
    live = IssueRef("org", ".github", 9)
    plan = epics.DrainPlan(live, moves=[], close=[])
    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=["tool"]),
        patch("vergil_tooling.lib.github.is_public", return_value=True),
        patch(
            "vergil_tooling.lib.epics.plan_standing_drain",
            side_effect=lambda kind, *_a, **_k: plan if kind is epics.INTAKE else None,
        ),
        patch("vergil_tooling.lib.epics.apply_standing_drain") as mock_apply,
    ):
        plans = epics.drain_standing_org("org", apply=False, now=Q4_NOW)
    assert plans == [plan]  # no planning epic yet -> no plan for it
    mock_apply.assert_not_called()


def test_drain_standing_org_skips_home_that_raises(capsys: pytest.CaptureFixture[str]) -> None:
    good = epics.DrainPlan(IssueRef("org", ".github", 10), moves=[], close=[])

    def fake_plan(
        kind: epics.StandingKind, home: str, name: str, *, now: datetime
    ) -> epics.DrainPlan:
        if kind is epics.INTAKE:
            raise ValueError("multiple intake epics — corruption")
        return good

    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=[]),
        patch("vergil_tooling.lib.epics.plan_standing_drain", side_effect=fake_plan),
        patch("vergil_tooling.lib.epics.apply_standing_drain") as mock_apply,
    ):
        plans = epics.drain_standing_org("org", apply=True, now=Q4_NOW)  # must NOT raise
    assert plans == [good, good]
    assert [c.args for c in mock_apply.call_args_list] == [
        (epics.PLANNING, "org/.github", "org", good),
        (epics.DEFERRED, "org/.github", "org", good),
    ]
    assert "skipped intake drain in org/.github: multiple intake epics" in capsys.readouterr().err


# -- EPIC_SENTINELS (#3514) ---------------------------------------------------


def test_epic_sentinels_are_the_standing_kinds_sentinels() -> None:
    assert epics.EPIC_SENTINELS == ("adhoc", "intake", "planning", "deferred")
    assert tuple(k.sentinel for k in epics.STANDING_KINDS) == epics.EPIC_SENTINELS


@pytest.mark.parametrize(
    ("sentinel", "ensure"),
    [
        ("adhoc", "ensure_adhoc_epic"),
        ("intake", "ensure_intake_epic"),
        ("planning", "ensure_planning_epic"),
        ("deferred", "ensure_deferred_epic"),
    ],
)
def test_resolve_epic_ref_handles_every_sentinel(sentinel: str, ensure: str) -> None:
    """Every EPIC_SENTINELS entry is resolved by its ensure_* helper, never parsed."""
    epic = IssueRef("org", ".github", 9)
    with (
        patch(f"vergil_tooling.lib.epics.{ensure}", return_value=epic) as mock_ensure,
        patch("vergil_tooling.lib.epics.qualify_epic_ref") as mock_qualify,
    ):
        assert epics.resolve_epic_ref(sentinel, repo="org/tool") == epic
    mock_ensure.assert_called_once_with("org/tool")
    mock_qualify.assert_not_called()
    assert sentinel in epics.EPIC_SENTINELS


# -- DEFERRED standing queue kind (epic .github#508) ---------------------------


def test_deferred_is_a_standing_queue_kind() -> None:
    assert epics.DEFERRED in epics.STANDING_KINDS
    assert epics.QUEUE_KINDS == (epics.INTAKE, epics.PLANNING, epics.DEFERRED)
    assert "deferred" in epics.EPIC_SENTINELS
    assert epics.standing_kind_of({"epic", "deferred"}) is epics.DEFERRED
    assert epics.standing_kind_of({"archive", "deferred"}) is None


def test_deferred_kind_fields() -> None:
    kind = epics.DEFERRED
    assert kind.key == "deferred"
    assert kind.sentinel == "deferred"
    assert kind.title_prefix == "Epic (deferred): "
    assert kind.labels == ("epic", "deferred")
    assert kind.archive_prefix == "Archive (deferred): "
    assert kind.archive_labels == ("archive", "deferred")
    assert kind.body.format(owner="org", name="org") == (
        "Perpetual deferred queue for org: evaluated items parked until their revival "
        "conditions are met. Reviewed by deferred-review.\n"
    )


def test_deferred_label_is_defined() -> None:
    names = {label["name"] for label in load_labels()["labels"]}
    assert "deferred" in names


def test_queue_kind_by_key() -> None:
    assert epics.QUEUE_KIND_BY_KEY == {
        "intake": epics.INTAKE,
        "planning": epics.PLANNING,
        "deferred": epics.DEFERRED,
    }


def test_archive_re_matches_deferred_archive() -> None:
    m = epics.archive_re(epics.DEFERRED).match("Archive (deferred): org — 2026-Q4")
    assert m and m["name"] == "org" and m["quarter"] == "2026-Q4"
    assert epics.archive_re(epics.DEFERRED).match("Archive (planning): org — 2026-Q4") is None
    assert epics.archive_re(epics.DEFERRED).match("Epic (deferred): org") is None


@pytest.mark.parametrize(
    ("labels", "queue", "container"),
    [
        ({"epic", "deferred"}, "deferred", "deferred"),
        ({"epic", "intake"}, "intake", "intake"),
        ({"epic", "planning"}, "planning", "planning"),
        ({"epic", "ad-hoc"}, None, "ad-hoc"),
        ({"epic"}, None, None),  # a development epic is not a container
        ({"archive", "deferred"}, None, None),  # archives are not live containers
    ],
)
def test_queue_kind_and_container_kind_deferred_and_adhoc(
    labels: set[str], queue: str | None, container: str | None
) -> None:
    with patch("vergil_tooling.lib.epics._labels", return_value=labels):
        assert epics.queue_kind(EPIC) == queue
        assert epics.container_kind(EPIC) == container


def test_queue_item_kind_deferred() -> None:
    with (
        patch("vergil_tooling.lib.epics.native_parent_of", return_value=EPIC),
        patch("vergil_tooling.lib.epics._labels", return_value={"epic", "deferred"}),
    ):
        assert epics.queue_item_kind("#101", default_repo="org/repo-a") == "deferred"


def test_ensure_deferred_epic_uses_queue_home() -> None:
    epic = IssueRef("org", ".github", 10)
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.epics.ensure_standing_epic", return_value=epic) as mock_ens,
    ):
        assert epics.ensure_deferred_epic("org/tool") == epic
    mock_ens.assert_called_once_with(epics.DEFERRED, "org/.github", "org")

    private = IssueRef("org", "secret", 3)
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/secret"),
        patch("vergil_tooling.lib.epics.ensure_standing_epic", return_value=private) as mock_ens,
    ):
        assert epics.ensure_deferred_epic("org/secret") == private
    mock_ens.assert_called_once_with(epics.DEFERRED, "org/secret", "secret")


def test_ensure_deferred_epic_creates_with_kind_labels() -> None:
    # ensure_standing_epic ensures the kind's labels at the point of use (#3378).
    created = "https://github.com/org/.github/issues/10"
    with (
        patch("vergil_tooling.lib.epics.resolve_epic_home", return_value="org/.github"),
        patch("vergil_tooling.lib.github.read_json", return_value=[]),
        patch("vergil_tooling.lib.epics.ensure_labels") as mock_ensure,
        patch("vergil_tooling.lib.github.create_issue", return_value=created) as mock_create,
    ):
        assert epics.ensure_deferred_epic("org/tool") == IssueRef("org", ".github", 10)
    mock_ensure.assert_called_once_with("org/.github", ("epic", "deferred"))
    kwargs = mock_create.call_args.kwargs
    assert kwargs["title"] == "Epic (deferred): org"
    assert kwargs["labels"] == ["epic", "deferred"]
    assert kwargs["body"].startswith("Perpetual deferred queue for org:")


def test_queue_refusal_message_deferred() -> None:
    msg = epics.queue_refusal_message("org/tool#5", "deferred")
    assert msg == (
        "--issue (org/tool#5) is in the deferred queue; it is deferred until its "
        "revival conditions are met. Revive it with deferred-review first."
    )


def test_deferred_route_only_points_at_intake_route() -> None:
    assert "vrg-intake-route" in epics.DEFERRED_ROUTE_ONLY


def test_drain_standing_org_covers_deferred() -> None:
    seen: list[epics.StandingKind] = []

    def fake_plan(
        kind: epics.StandingKind, home: str, name: str, *, now: datetime
    ) -> epics.DrainPlan | None:
        seen.append(kind)
        return None

    with (
        patch("vergil_tooling.lib.github.list_org_repos", return_value=[]),
        patch("vergil_tooling.lib.epics.plan_standing_drain", side_effect=fake_plan),
    ):
        epics.drain_standing_org("org", apply=False, now=Q4_NOW)
    assert seen == [epics.INTAKE, epics.PLANNING, epics.DEFERRED]
