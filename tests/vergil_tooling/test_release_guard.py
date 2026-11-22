from __future__ import annotations

import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.guard import (
    GHOST,
    IssueIdentities,
    TrackingIssueBreach,
    check_tracking_issue,
    ensure_locked,
    read_identities,
)

_MOD = "vergil_tooling.lib.release.guard"
HUMANS = ("human",)


def _who(login: str | None) -> dict[str, str] | None:
    return None if login is None else {"login": login}


def _conn(nodes: list[Any], *, total: int | None = None, cursor: str | None = None) -> Any:
    return {
        "totalCount": len(nodes) if total is None else total,
        "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor or "end"},
        "nodes": nodes,
    }


# GitHub's totalCount on a type-filtered timeline counts every item of every
# type on every page, so it is above any filteredCount the read uses.
_UNFILTERED = 100


def _timeline(nodes: list[Any], *, total: int | None = None, cursor: str | None = None) -> Any:
    """A type-filtered timeline page: *total* is its filteredCount.

    As GitHub reports it, a page's filteredCount counts the items from that
    page's cursor on: this page's and every later page's, never earlier ones.
    """
    conn = _conn(nodes, total=total, cursor=cursor)
    conn["filteredCount"] = conn.pop("totalCount")
    conn["totalCount"] = _UNFILTERED
    return conn


def _edit(login: str | None) -> dict[str, Any]:
    return {"editor": _who(login)}


def _event(typename: str, login: str | None) -> dict[str, Any]:
    field = "author" if typename == "IssueComment" else "actor"
    return {"__typename": typename, field: _who(login)}


def _page(
    *,
    author: str | None = "human",
    editor: str | None = None,
    edits: Any = None,
    timeline: Any = None,
) -> dict[str, Any]:
    return {
        "repository": {
            "issue": {
                "author": _who(author),
                "editor": _who(editor),
                "userContentEdits": _conn([]) if edits is None else edits,
                "timelineItems": _timeline([]) if timeline is None else timeline,
            }
        }
    }


def _read(*pages: dict[str, Any]) -> IssueIdentities:
    with patch(_MOD + ".github.graphql", side_effect=list(pages)):
        return read_identities("owner/repo", 42)


def _identities(**overrides: Any) -> IssueIdentities:
    fields: dict[str, Any] = {
        "author": "human",
        "editors": ("human",),
        "state_actors": (("human", "closed the issue"), ("human", "reopened the issue")),
        "traces": (),
    }
    return IssueIdentities(**{**fields, **overrides})


# check_tracking_issue: the identity rule


def test_guard_passes_human_only() -> None:
    identities = _read(
        _page(
            editor="human",
            edits=_conn([_edit("human"), _edit("human")]),
            timeline=_timeline(
                [
                    _event("ClosedEvent", "human"),
                    _event("ReopenedEvent", "human"),
                    _event("IssueComment", "human"),
                ]
            ),
        )
    )
    assert identities == IssueIdentities(
        author="human",
        editors=("human",),
        state_actors=(("human", "closed the issue"), ("human", "reopened the issue")),
        traces=(("human", "commented"),),
    )
    assert check_tracking_issue(identities, HUMANS) == []


def test_guard_stops_on_agent_body_edit() -> None:
    identities = _read(_page(editor="human", edits=_conn([_edit("human"), _edit("agent")])))
    assert identities.editors == ("human", "agent")
    with pytest.raises(TrackingIssueBreach, match="agent edited the body") as info:
        check_tracking_issue(identities, HUMANS)
    assert (info.value.login, info.value.action) == ("agent", "edited the body")
    assert str(info.value).startswith("suspected security breach on the tracking issue:")
    assert isinstance(info.value, ReleaseError)


def test_guard_stops_on_agent_last_editor() -> None:
    with pytest.raises(TrackingIssueBreach, match="agent edited the body"):
        check_tracking_issue(_identities(editors=("agent",)), HUMANS)


def test_guard_stops_on_foreign_reopen() -> None:
    identities = _read(
        _page(
            timeline=_timeline([_event("ClosedEvent", "human"), _event("ReopenedEvent", "agent")])
        )
    )
    with pytest.raises(TrackingIssueBreach, match="agent reopened the issue"):
        check_tracking_issue(identities, HUMANS)


def test_guard_stops_on_foreign_close() -> None:
    with pytest.raises(TrackingIssueBreach, match="agent closed the issue"):
        check_tracking_issue(_identities(state_actors=(("agent", "closed the issue"),)), HUMANS)


def test_guard_stops_on_foreign_author() -> None:
    identities = _read(_page(author="app/agent"))
    assert identities.author == "agent"
    with pytest.raises(TrackingIssueBreach, match="agent authored the issue"):
        check_tracking_issue(identities, HUMANS)


def test_guard_treats_a_deleted_account_as_foreign() -> None:
    identities = _read(_page(edits=_conn([_edit(None)])))
    assert identities.editors == (GHOST,)
    with pytest.raises(TrackingIssueBreach, match=f"{GHOST} edited the body"):
        check_tracking_issue(identities, HUMANS)


def test_guard_strips_app_prefix_from_humans() -> None:
    assert check_tracking_issue(_identities(), ("app/human",)) == []


def test_guard_breach_names_the_issue_number() -> None:
    breach = TrackingIssueBreach("agent", "edited the body", number=42)
    assert str(breach).startswith("suspected security breach on tracking issue #42: agent")
    assert "can never pass this check again" in str(breach)
    assert "vrg-release --supersede <version>" in str(breach)


def test_guard_warns_on_foreign_comment_and_crossref() -> None:
    identities = _read(
        _page(
            timeline=_timeline(
                [
                    _event("IssueComment", "agent"),
                    _event("IssueComment", "human"),
                    _event("CrossReferencedEvent", "other"),
                ]
            )
        )
    )
    assert check_tracking_issue(identities, HUMANS) == [
        "agent commented",
        "other cross-referenced the issue",
    ]


@pytest.mark.parametrize(
    ("typename", "action"),
    [
        ("MentionedEvent", "mentioned the issue"),
        ("SubscribedEvent", "subscribed to the issue"),
        ("LabeledEvent", "added a label"),
        ("UnlabeledEvent", "removed a label"),
    ],
)
def test_guard_warns_on_every_trace_kind(typename: str, action: str) -> None:
    identities = _read(_page(timeline=_timeline([_event(typename, "agent")])))
    assert check_tracking_issue(identities, HUMANS) == [f"agent {action}"]


# read_identities: pagination and loud failure


def test_guard_reads_every_page_of_both_connections() -> None:
    first = _page(
        edits=_conn([_edit("human")], total=2, cursor="e1"),
        timeline=_timeline([_event("IssueComment", "human")], total=3, cursor="t1"),
    )
    second = _page(
        edits=_conn([_edit("agent")], total=2),
        timeline=_timeline([_event("IssueComment", "human")], total=2, cursor="t2"),
    )
    third = _page(
        edits=_conn([], total=2),
        timeline=_timeline([_event("ReopenedEvent", "agent")], total=1),
    )
    with patch(_MOD + ".github.graphql", side_effect=[first, second, third]) as gql:
        identities = read_identities("owner/repo", 42)
    assert identities.editors == ("human", "agent")
    assert identities.state_actors == (("agent", "reopened the issue"),)
    assert gql.call_count == 3
    assert "edits" not in gql.call_args_list[0].kwargs
    assert gql.call_args_list[1].kwargs["edits"] == "e1"
    assert gql.call_args_list[1].kwargs["timeline"] == "t1"
    # The finished edit history is re-read at its last cursor, and must stay finished.
    assert gql.call_args_list[2].kwargs["edits"] == "end"
    assert gql.call_args_list[2].kwargs["timeline"] == "t2"
    assert gql.call_args_list[0].kwargs["number"] == 42


def test_guard_rereads_an_empty_connection_from_the_start() -> None:
    """An empty connection has no cursor, so it is re-read from the start, and stays empty."""
    empty = {"totalCount": 0, "pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
    first = _page(
        edits=empty, timeline=_timeline([_event("IssueComment", "x")], total=2, cursor="t1")
    )
    second = _page(edits=empty, timeline=_timeline([_event("IssueComment", "y")], total=1))
    with patch(_MOD + ".github.graphql", side_effect=[first, second]) as gql:
        identities = read_identities("owner/repo", 42)
    assert identities.editors == ()
    assert identities.traces == (("x", "commented"), ("y", "commented"))
    assert "edits" not in gql.call_args_list[1].kwargs


def test_guard_read_failure_raises() -> None:
    err = github.GitHubAPIError(1, ("gh", "api", "graphql"), stderr="HTTP 502")
    with (
        patch(_MOD + ".github.graphql", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        read_identities("owner/repo", 42)


def test_guard_graphql_errors_raise() -> None:
    with (
        patch(_MOD + ".github.graphql", side_effect=RuntimeError("GraphQL errors: [...]")),
        pytest.raises(RuntimeError, match="GraphQL errors"),
    ):
        read_identities("owner/repo", 42)


def test_guard_truncated_connection_raises() -> None:
    page = _page(edits=_conn([_edit("human")], total=2))
    with pytest.raises(github.GitHubAPIError) as info:
        _read(page)
    assert "GitHub lists 2 userContentEdits but returned 1" in str(info.value.stderr)


def test_guard_timeline_counts_filtered_items_only() -> None:
    """The timeline is complete at its filteredCount, though its totalCount is higher."""
    page = _page(timeline=_timeline([_event("ClosedEvent", "human")]))
    assert page["repository"]["issue"]["timelineItems"]["totalCount"] > 1
    assert _read(page).state_actors == (("human", "closed the issue"),)


def test_guard_timeline_truncated_by_filtered_count_raises() -> None:
    page = _page(timeline=_timeline([_event("ClosedEvent", "human")], total=2))
    with pytest.raises(github.GitHubAPIError) as info:
        _read(page)
    assert "GitHub lists 2 timelineItems but returned 1" in str(info.value.stderr)


def test_guard_timeline_short_on_a_later_page_raises() -> None:
    """A later page's filteredCount counts from its cursor on; the read adds the earlier pages."""
    first = _page(timeline=_timeline([_event("IssueComment", "x")], total=3, cursor="t1"))
    second = _page(timeline=_timeline([_event("IssueComment", "y")], total=3))
    with pytest.raises(github.GitHubAPIError) as info:
        _read(first, second)
    assert "GitHub lists 4 timelineItems but returned 2" in str(info.value.stderr)


def test_guard_query_reads_timeline_filtered_count() -> None:
    with patch(_MOD + ".github.graphql", return_value=_page()) as graphql:
        read_identities("owner/repo", 42)
    query = graphql.call_args.args[0]
    timeline = query[query.index("timelineItems(") :]
    assert "filteredCount" in timeline
    assert "totalCount" not in timeline


def test_guard_timeline_without_filtered_count_raises() -> None:
    timeline = _conn([])
    with pytest.raises(github.GitHubAPIError) as info:
        _read(_page(timeline=timeline))
    assert "timelineItems has no filteredCount or nodes" in str(info.value.stderr)


def test_guard_next_page_without_cursor_raises() -> None:
    timeline = _timeline([], total=1)
    timeline["pageInfo"] = {"hasNextPage": True, "endCursor": None}
    with pytest.raises(github.GitHubAPIError) as info:
        _read(_page(timeline=timeline))
    assert "timelineItems has a next page but no cursor" in str(info.value.stderr)


def test_guard_connection_growing_mid_read_raises() -> None:
    first = _page(timeline=_timeline([], total=1, cursor="t1"))
    second = _page(edits=_conn([_edit("agent")]), timeline=_timeline([_event("IssueComment", "x")]))
    with pytest.raises(github.GitHubAPIError) as info:
        _read(first, second)
    assert "grew while it was read" in str(info.value.stderr)


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"repository": None},
        {"repository": {"issue": None}},
        {"repository": {"issue": {"author": {"login": ""}}}},
        {"repository": {"issue": {"author": None, "editor": None, "userContentEdits": None}}},
        _page(timeline={"filteredCount": 0, "nodes": []}),
        _page(timeline={"filteredCount": "0", "pageInfo": {}, "nodes": []}),
        _page(timeline={"filteredCount": 0, "pageInfo": {"endCursor": None}, "nodes": []}),
        _page(timeline=_timeline([{"__typename": "AssignedEvent", "actor": None}])),
        _page(timeline=_timeline(["not an object"])),
        _page(edits=_conn([{"editor": "agent"}])),
    ],
)
def test_guard_malformed_response_raises(data: dict[str, Any]) -> None:
    with pytest.raises(github.GitHubAPIError) as info:
        _read(data)
    assert "cannot read the identities on tracking issue owner/repo#42" in str(info.value.stderr)


# ensure_locked


def _locked(value: object) -> dict[str, Any]:
    return {"repository": {"issue": {"locked": value}}}


def test_ensure_locked_locks_unlocked_issue() -> None:
    with (
        patch(_MOD + ".github.graphql", return_value=_locked(False)) as gql,
        patch(_MOD + ".github.run") as run,
    ):
        ensure_locked("owner/repo", 42)
    assert gql.call_args.kwargs == {"owner": "owner", "name": "repo", "number": 42}
    run.assert_called_once_with("issue", "lock", "42", "--repo", "owner/repo")


def test_ensure_locked_is_noop_when_locked() -> None:
    with (
        patch(_MOD + ".github.graphql", return_value=_locked(True)),
        patch(_MOD + ".github.run") as run,
    ):
        ensure_locked("owner/repo", 42)
    run.assert_not_called()


@pytest.mark.parametrize("data", [{}, _locked(None), _locked("false")])
def test_ensure_locked_unreadable_flag_raises(data: dict[str, Any]) -> None:
    with (
        patch(_MOD + ".github.graphql", return_value=data),
        patch(_MOD + ".github.run") as run,
        pytest.raises(github.GitHubAPIError),
    ):
        ensure_locked("owner/repo", 42)
    run.assert_not_called()


def test_ensure_locked_lock_failure_raises() -> None:
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403")
    with (
        patch(_MOD + ".github.graphql", return_value=_locked(False)),
        patch(_MOD + ".github.run", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        ensure_locked("owner/repo", 42)
