"""Tests for the closing PRs the group audit counts: merged, in the task's repo, and trusted.

Epic vergil-project/.github#1672, phase 3 Task 17 (spec §5.6). The GraphQL read is stubbed at
``github.graphql``; the closing-reference trust rule at ``pending_release.trusted_closing_refs``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib import trust
from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.release_groups import closers
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Mapping

TASK = IssueRef("org", "repo", 1)
AUTHORS = TrustedAuthors(human=("human",), agents=())
_GRAPHQL = "vergil_tooling.lib.github.graphql"
_TRUSTED = "vergil_tooling.lib.pending_release.trusted_closing_refs"


def _node(number: int, *, merged: bool = True, repo: str = "org/repo") -> dict[str, object]:
    return {
        "number": number,
        "merged": merged,
        "repository": {"nameWithOwner": repo},
    }


def _data(nodes: list[object], total: object | None = None) -> dict[str, object]:
    connection = {
        "totalCount": len(nodes) if total is None else total,
        "nodes": nodes,
    }
    return {"repository": {"issue": {"closedByPullRequestsReferences": connection}}}


def _trusting(refs: Mapping[object, tuple[int, ...]]) -> object:
    """A stand-in for the trust rule: PR *n* closes the issues ``refs[n]``, with nothing skipped."""

    def judge(node: Mapping[str, object], **_: object) -> tuple[tuple[int, ...], list[object]]:
        return refs.get(node["number"], ()), []

    return judge


def test_merged_trusted_closer_counts() -> None:
    with (
        patch(_GRAPHQL, return_value=_data([_node(10)])),
        patch(_TRUSTED, side_effect=_trusting({10: (1,)})),
    ):
        assert closers.merged_closers(TASK, authors=AUTHORS) == (IssueRef("org", "repo", 10),)


def test_unmerged_pr_is_not_a_closer() -> None:
    with (
        patch(_GRAPHQL, return_value=_data([_node(10, merged=False)])),
        patch(_TRUSTED, side_effect=_trusting({10: (1,)})),
    ):
        assert closers.merged_closers(TASK, authors=AUTHORS) == ()


def test_pr_in_another_repo_is_not_a_closer() -> None:
    # A merged PR of another repo may name an issue of the same number: it does not close the
    # task, and the trust rule is not asked about it.
    with (
        patch(_GRAPHQL, return_value=_data([_node(10, repo="other/repo")])),
        patch(_TRUSTED, side_effect=AssertionError("the trust rule is not asked")),
    ):
        assert closers.merged_closers(TASK, authors=AUTHORS) == ()


def test_untrusted_closing_reference_is_not_a_closer() -> None:
    # PR 10 is merged and in the repo, but the trust rule does not count its reference to #1.
    with (
        patch(_GRAPHQL, return_value=_data([_node(10)])),
        patch(_TRUSTED, side_effect=_trusting({10: (2,)})),
    ):
        assert closers.merged_closers(TASK, authors=AUTHORS) == ()


def test_closer_repo_name_is_taken_as_github_spells_it() -> None:
    # The task's repo is typed in lower case; GitHub spells the PR's repo in its own case.
    with (
        patch(_GRAPHQL, return_value=_data([_node(10, repo="Org/Repo")])),
        patch(_TRUSTED, side_effect=_trusting({10: (1,)})),
    ):
        assert closers.merged_closers(TASK, authors=AUTHORS) == (IssueRef("Org", "Repo", 10),)


def test_truncated_connection_raises() -> None:
    # GitHub lists 21 closing PRs but returned the first 20: the list is not read in full.
    nodes: list[object] = [_node(n) for n in range(1, 21)]
    with (
        patch(_GRAPHQL, return_value=_data(nodes, total=21)),
        pytest.raises(closers.ClosersReadError, match="lists 21 but returned 20"),
    ):
        closers.merged_closers(TASK, authors=AUTHORS)


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ({"repository": None}, "the repository is None"),
        ({"repository": {"issue": None}}, "the issue is None"),
        ({"repository": {"issue": {"closedByPullRequestsReferences": None}}}, "the connection"),
        (
            {
                "repository": {
                    "issue": {"closedByPullRequestsReferences": {"totalCount": 0, "nodes": "x"}}
                }
            },
            "no totalCount or nodes",
        ),
        (_data([], total=True), "no totalCount or nodes"),
        (_data([5], total=1), "a PR is 5"),
        (_data([{"merged": True, "repository": {"nameWithOwner": "org/repo"}}]), "no number"),
        (_data([{"number": True, "merged": True}]), "no number"),
        (_data([{"number": 10, "merged": True, "repository": None}]), "PR #10"),
        (_data([{"number": 10, "merged": True, "repository": {"nameWithOwner": 5}}]), "PR #10"),
        (
            _data([{"number": 10, "merged": True, "repository": {"nameWithOwner": "orgrepo"}}]),
            "PR #10",
        ),
    ],
)
def test_malformed_read_raises(data: dict[str, object], match: str) -> None:
    with (
        patch(_GRAPHQL, return_value=data),
        patch(_TRUSTED, side_effect=_trusting({})),
        pytest.raises(closers.ClosersReadError, match=match),
    ):
        closers.merged_closers(TASK, authors=AUTHORS)


def test_trust_read_error_propagates() -> None:
    # A closing reference whose author cannot be read is a refusal of the read, never a pass.
    error = trust.TrustReadError("trusted_closing_refs", "the author could not be read")
    with (
        patch(_GRAPHQL, return_value=_data([_node(10)])),
        patch(_TRUSTED, side_effect=error),
        pytest.raises(trust.TrustReadError),
    ):
        closers.merged_closers(TASK, authors=AUTHORS)
