"""Tests for ``vergil_tooling.lib.rulings.comments``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import (
    IssueComment,
    fetch_comments,
    parse_issue_ref,
    trusted,
)
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Callable

Pages = list[Any]


def _node(
    comment_id: str,
    login: str | None = "h",
    body: str = "text",
    edited: str | None = None,
) -> dict[str, Any]:
    return {
        "fullDatabaseId": comment_id,
        "author": None if login is None else {"login": login},
        "body": body,
        "lastEditedAt": edited,
    }


def _page(nodes: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {
        "repository": {
            "issue": {
                "comments": {
                    "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                    "nodes": nodes,
                }
            }
        }
    }


@pytest.fixture
def fake_graphql(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Pages], list[dict[str, object]]]:
    """Install a fake ``github.graphql`` that returns ``pages`` in turn.

    Returns the list of variables each call received.
    """

    def install(pages: Pages) -> list[dict[str, object]]:
        calls: list[dict[str, object]] = []
        remaining = list(pages)

        def graphql(query: str, **variables: object) -> Any:
            assert "fullDatabaseId" in query
            assert "databaseId" not in query.replace("fullDatabaseId", "")
            calls.append(variables)
            return remaining.pop(0)

        monkeypatch.setattr("vergil_tooling.lib.github.graphql", graphql)
        return calls

    return install


# --- parse_issue_ref ---------------------------------------------------------


def test_parse_issue_ref_accepts_owner_repo_number() -> None:
    assert parse_issue_ref("vergil-project/vergil-tooling#3923") == (
        "vergil-project/vergil-tooling",
        3923,
    )
    assert parse_issue_ref("o.x/r_y-z#1") == ("o.x/r_y-z", 1)


@pytest.mark.parametrize(
    "ref",
    ["o/r", "#7", "o/r#0", "o/r#07", "o/r#-1", "r#7", "o/r/x#7", "o/r#7 ", "o/r#7\n", "o/r#٧"],
)
def test_parse_issue_ref_refuses_other_forms(ref: str) -> None:
    with pytest.raises(RulingError, match="owner/repo#N"):
        parse_issue_ref(ref)


# --- fetch_comments ----------------------------------------------------------


def test_fetch_comments_reads_every_page_and_strips_app(
    fake_graphql: Callable[[Pages], list[dict[str, object]]],
) -> None:
    first = [_node("1", login="app/h-vergil-user")] + [_node(str(i)) for i in range(2, 101)]
    last = [_node("101", edited="2026-11-05T00:00:00Z")]
    calls = fake_graphql([_page(first, cursor="c1"), _page(last)])
    comments = fetch_comments("o/r", 7)
    assert len(comments) == 101
    assert comments[0].author == "h-vergil-user"
    assert [c.id for c in comments] == list(range(1, 102))
    assert comments[-1].edited is True
    assert comments[0].edited is False
    assert calls == [
        {"owner": "o", "name": "r", "number": 7},
        {"owner": "o", "name": "r", "number": 7, "after": "c1"},
    ]


def test_fetch_comments_keeps_64_bit_ids(
    fake_graphql: Callable[[Pages], list[dict[str, object]]],
) -> None:
    fake_graphql([_page([_node("4300000001")])])
    assert fetch_comments("o/r", 7)[0].id == 4300000001


def test_fetch_comments_keeps_a_deleted_author_as_empty(
    fake_graphql: Callable[[Pages], list[dict[str, object]]],
) -> None:
    fake_graphql([_page([_node("5", login=None)])])
    assert fetch_comments("o/r", 7) == [IssueComment(5, "", "text", False)]


def test_fetch_comments_of_an_issue_with_none(
    fake_graphql: Callable[[Pages], list[dict[str, object]]],
) -> None:
    fake_graphql([_page([])])
    assert fetch_comments("o/r", 7) == []


@pytest.mark.parametrize(
    ("page", "match"),
    [
        ({}, "issue not found"),
        ({"repository": None}, "issue not found"),
        ({"repository": {"issue": None}}, "issue not found"),
        ({"repository": {"issue": {"comments": None}}}, "not shaped as expected"),
        ({"repository": {"issue": {"comments": {"nodes": None}}}}, "not shaped as expected"),
        ({"repository": {"issue": {"comments": {"nodes": []}}}}, "no pageInfo"),
        (_page([], cursor=""), "gives no cursor"),
        (_page(["not a node"]), "not an object"),
        (_page([_node("12", login="")]), "comment 12 has an author with no login"),
        (_page([{**_node("12"), "author": "h"}]), "comment 12 has an author with no login"),
        (_page([{**_node("12"), "body": None}]), "comment 12 has no body string"),
        (_page([{**_node("1"), "fullDatabaseId": 12}]), "no usable fullDatabaseId"),
        (_page([_node("0")]), "no usable fullDatabaseId"),
        (_page([_node("012")]), "no usable fullDatabaseId"),
        (_page([_node("1٢")]), "no usable fullDatabaseId"),
    ],
)
def test_fetch_comments_refuses_a_malformed_page(
    fake_graphql: Callable[[Pages], list[dict[str, object]]],
    page: Any,
    match: str,
) -> None:
    fake_graphql([page])
    with pytest.raises(RulingError, match=match):
        fetch_comments("o/r", 7)


def test_fetch_comments_refuses_a_page_with_more_and_no_cursor(
    fake_graphql: Callable[[Pages], list[dict[str, object]]],
) -> None:
    page = _page([_node("1")])
    page["repository"]["issue"]["comments"]["pageInfo"] = {"hasNextPage": True}
    fake_graphql([page])
    with pytest.raises(RulingError, match="gives no cursor"):
        fetch_comments("o/r", 7)


# --- trusted -----------------------------------------------------------------


def test_trusted_keeps_only_trusted_logins_in_order() -> None:
    authors = TrustedAuthors(human=("h",), agents=("h-vergil-user", "h-vergil-audit"))
    comments = [
        IssueComment(1, "h-vergil-user", "a", False),
        IssueComment(2, "stranger", "b", False),
        IssueComment(3, "", "c", False),
        IssueComment(4, "h", "d", False),
        IssueComment(5, "h-vergil-audit", "e", False),
        IssueComment(6, "app/h", "f", False),
    ]
    assert [c.id for c in trusted(comments, authors)] == [1, 4, 5]
