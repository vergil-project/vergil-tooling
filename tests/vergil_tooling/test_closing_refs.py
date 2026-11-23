"""Tests for a PR's closing references read through the trust layer.

``trust.read_pr_closing`` and ``closing_refs.read_closing_refs``
(GHSA-g68j-q2mw-jqf9): the real layer, its GraphQL answered by stubs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import github, trust
from vergil_tooling.lib.closing_refs import read_closing_refs
from vergil_tooling.lib.github import ClosingIssue
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Iterator

AUTHORS = trust_stubs.AUTHORS


def _pr(answer: dict[str, Any]) -> dict[str, Any]:
    return answer["repository"]["pullRequest"]


@pytest.fixture
def graphql() -> Iterator[MagicMock]:
    mock = MagicMock()
    with patch("vergil_tooling.lib.github.graphql", mock):
        yield mock


# -- trust.read_pr_closing ---------------------------------------------------------


def test_read_pr_closing_reads_the_pr_by_number(graphql: MagicMock) -> None:
    graphql.return_value = trust_stubs.closing_pr("o/r", 5, ("o/r", 7, "OPEN"))
    pr = trust.read_pr_closing("o/r", 5)
    assert (pr.repo, pr.number, pr.state, pr.merged_at) == ("o/r", 5, "MERGED", trust_stubs.EDITED)
    assert pr.node["body"] == "Summary.\n\nCloses o/r#7"
    query = graphql.call_args.args[0]
    assert query == trust._PR_CLOSING_QUERY
    assert "closingIssuesReferences(first: 100, userLinkedOnly: false)" in query
    assert "timelineItems(itemTypes: [CONNECTED_EVENT], first: 100)" in query
    assert graphql.call_args.kwargs == {"owner": "o", "name": "r", "number": 5}


def test_read_pr_closing_reads_every_page_of_the_body_edits(graphql: MagicMock) -> None:
    first = trust_stubs.closing_pr("o/r", 5, editor=trust_stubs.HUMAN)
    _pr(first)["userContentEdits"] = {
        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
        "nodes": [{"editedAt": trust_stubs.EDITED, "editor": trust_stubs.HUMAN}],
    }
    page = {
        "repository": {
            "issueOrPullRequest": {
                "userContentEdits": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [{"editedAt": trust_stubs.EDITED, "editor": trust_stubs.OUTSIDER}],
                }
            }
        }
    }
    graphql.side_effect = [first, page]
    pr = trust.read_pr_closing("o/r", 5)
    assert graphql.call_args_list[1].args[0] == trust._BODY_EDITS_QUERY
    assert graphql.call_args_list[1].kwargs["after"] == "c1"
    edits = pr.node["userContentEdits"]
    assert edits["pageInfo"]["hasNextPage"] is False
    assert [e["editor"] for e in edits["nodes"]] == [trust_stubs.HUMAN, trust_stubs.OUTSIDER]
    # The edit on the second page is judged: the body is no longer trusted.
    _, skipped = trust.judge_body(pr.node, repo="o/r", number=5, kind="pr", authors=AUTHORS)
    assert skipped is not None
    assert skipped.login == "drive-by"


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ({"number": 6}, "not the one asked for"),
        ({"repository": {"nameWithOwner": "o/elsewhere"}}, "reads as one of o/elsewhere"),
        ({"state": "DRAFT"}, "no usable state"),
        ({"mergedAt": 5}, "no usable mergedAt"),
        ({"author": {"login": "x"}}, "no __typename"),
    ],
)
def test_read_pr_closing_refuses_a_bad_answer(
    graphql: MagicMock, change: dict[str, Any], detail: str
) -> None:
    answer = trust_stubs.closing_pr("o/r", 5)
    _pr(answer).update(change)
    graphql.return_value = answer
    with pytest.raises(trust.TrustReadError, match=detail):
        trust.read_pr_closing("o/r", 5)


def test_read_pr_closing_gh_failure_is_a_read_error(graphql: MagicMock) -> None:
    graphql.side_effect = github.GitHubAPIError(1, ("gh", "api"), stderr="body text\nHTTP 502")
    with pytest.raises(trust.TrustReadError, match="HTTP 502") as raised:
        trust.read_pr_closing("o/r", 5)
    assert "body text" not in str(raised.value)


# -- closing_refs.read_closing_refs -----------------------------------------------


def test_read_closing_refs_counts_only_trusted_same_repo_references(graphql: MagicMock) -> None:
    graphql.return_value = trust_stubs.closing_pr(
        "o/r",
        5,
        ("o/r", 7, "OPEN"),
        ("o/r", 8, "CLOSED"),
        ("o/r", 9, "OPEN"),
        ("o/other", 10, "OPEN"),
        body="Closes #7\nFixes #8\nCloses o/other#10",
        links=((trust_stubs.HUMAN, "o/r", 8), (trust_stubs.OUTSIDER, "o/r", 9)),
    )
    refs = read_closing_refs("o/r", 5, authors=AUTHORS)
    assert refs.ref == "o/r#5"
    assert refs.state == "MERGED"
    assert refs.counted == (ClosingIssue("o/r", 7, "OPEN"), ClosingIssue("o/r", 8, "CLOSED"))
    assert refs.uncounted == (ClosingIssue("o/r", 9, "OPEN"), ClosingIssue("o/other", 10, "OPEN"))
    assert [(s.kind, s.login, s.field) for s in refs.skipped] == [
        ("event", "drive-by", "link to #9")
    ]


def test_read_closing_refs_untrusted_body_counts_nothing_it_names(graphql: MagicMock) -> None:
    graphql.return_value = trust_stubs.closing_pr(
        "o/r", 5, ("o/r", 7, "OPEN"), author=trust_stubs.OUTSIDER, state="OPEN"
    )
    refs = read_closing_refs("o/r", 5, authors=AUTHORS)
    assert refs.state == "OPEN"
    assert refs.counted == ()
    assert refs.uncounted == (ClosingIssue("o/r", 7, "OPEN"),)
    assert [(s.kind, s.field, s.cause) for s in refs.skipped] == [("pr", "body", "untrusted")]


def test_read_closing_refs_automation_is_not_a_closing_role(graphql: MagicMock) -> None:
    automation = {"__typename": "Bot", "login": "o-automation"}
    authors = TrustedAuthors(
        human=AUTHORS.human, agents=AUTHORS.agents, orgs={"o": OrgConfig("o-automation")}
    )
    graphql.return_value = trust_stubs.closing_pr("o/r", 5, ("o/r", 7, "OPEN"), author=automation)
    assert read_closing_refs("o/r", 5, authors=authors).counted == ()


def test_read_closing_refs_truncated_list_is_a_read_error(graphql: MagicMock) -> None:
    answer = trust_stubs.closing_pr("o/r", 5, ("o/r", 7, "OPEN"))
    _pr(answer)["closingIssuesReferences"]["totalCount"] = 101
    graphql.return_value = answer
    with pytest.raises(trust.TrustReadError, match="truncated"):
        read_closing_refs("o/r", 5, authors=AUTHORS)


def test_read_closing_refs_malformed_fields_are_a_read_error(graphql: MagicMock) -> None:
    answer = trust_stubs.closing_pr("o/r", 5, ("o/r", 7, "OPEN"))
    del _pr(answer)["timelineItems"]
    graphql.return_value = answer
    with pytest.raises(trust.TrustReadError, match="closing fields are malformed"):
        read_closing_refs("o/r", 5, authors=AUTHORS)


@pytest.mark.parametrize(
    ("node", "detail"),
    [
        ({"number": 0, "state": "OPEN", "repository": {"nameWithOwner": "o/r"}}, "number"),
        ({"number": 7, "state": "GONE", "repository": {"nameWithOwner": "o/r"}}, "state"),
        ({"number": 7, "state": "OPEN", "repository": {"nameWithOwner": "o"}}, "repository"),
    ],
)
def test_read_closing_refs_bad_reference_is_a_read_error(
    graphql: MagicMock, node: dict[str, Any], detail: str
) -> None:
    answer = trust_stubs.closing_pr("o/r", 5, body="No closing line.")
    _pr(answer)["closingIssuesReferences"] = {"totalCount": 1, "nodes": [node]}
    graphql.return_value = answer
    with pytest.raises(trust.TrustReadError, match=f"closing reference has no .*{detail}"):
        read_closing_refs("o/r", 5, authors=AUTHORS)
