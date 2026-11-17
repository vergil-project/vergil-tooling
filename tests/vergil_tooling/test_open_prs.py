"""Tests for vergil_tooling.lib.open_prs."""

from __future__ import annotations

import datetime
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.open_prs import (
    OpenPR,
    OpenPRKind,
    OpenPRsError,
    classify_open,
    gather_open_prs,
)

_URL = "https://github.com/o/r.git"
_CREATED = "2026-11-14T09:00:00Z"


def _node(
    number: int,
    branch: str = "feature/1-x",
    title: str = "feat: a thing",
    *,
    base: str = "develop",
    draft: bool = False,
    cross: bool = False,
    head_owner: str | None = "o",
    author: str | None = "someone",
    labels: tuple[str, ...] = (),
    label_total: int | None = None,
    rollup: str | None = "SUCCESS",
    commits: bool = True,
) -> dict[str, Any]:
    commit_nodes = (
        [{"commit": {"statusCheckRollup": None if rollup is None else {"state": rollup}}}]
        if commits
        else []
    )
    return {
        "number": number,
        "title": title,
        "headRefName": branch,
        "baseRefName": base,
        "isDraft": draft,
        "isCrossRepository": cross,
        "createdAt": _CREATED,
        "author": None if author is None else {"login": author},
        "headRepositoryOwner": None if head_owner is None else {"login": head_owner},
        "labels": {
            "totalCount": len(labels) if label_total is None else label_total,
            "nodes": [{"name": name} for name in labels],
        },
        "commits": {"nodes": commit_nodes},
    }


def _page(
    nodes: list[dict[str, Any]], *, total: int | None = None, cursor: str | None = None
) -> dict[str, object]:
    return {
        "repository": {
            "pullRequests": {
                "totalCount": len(nodes) if total is None else total,
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }


def _gather(*pages: dict[str, object], url: str = _URL) -> tuple[tuple[OpenPR, ...], Any]:
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=url),
        patch("vergil_tooling.lib.github.graphql", side_effect=list(pages)) as graphql,
    ):
        return gather_open_prs(), graphql


def test_classify_open() -> None:
    assert classify_open("feat(versions): add rust 1.99", "lifecycle/rust-add-1.99", ()) == (
        OpenPRKind.LIFECYCLE,
        "lifecycle",
    )
    assert classify_open("chore: sync", "automation/sync/1", ()) == (
        OpenPRKind.AUTOMATION,
        "automation",
    )
    assert classify_open("chore(deps): x", "deps/update-1", ()) == (OpenPRKind.DEPS, "deps")
    assert classify_open("bump x", "dependabot/pip/x", ("dependency-update",)) == (
        OpenPRKind.DEPS,
        "deps",
    )
    assert classify_open("chore(release): x", "release/post-1.2.3", ()) == (
        OpenPRKind.RELEASE,
        "release",
    )
    assert classify_open("fix: y", "feature/1-y", ()) == (OpenPRKind.OTHER, "fix")
    assert classify_open("no type", "feature/1-y", ()) == (OpenPRKind.OTHER, "-")


def test_reads_every_field() -> None:
    nodes = [
        _node(7, "lifecycle/rust-drop-1.92", author="vergil-project-automation", rollup="PENDING"),
        _node(3, "feature/2-y", "fix: y", draft=True, base="main", rollup=None),
        _node(9, "feature/1-a", cross=True, head_owner="stranger", author=None, commits=False),
    ]
    prs, graphql = _gather(_page(nodes))
    assert [pr.number for pr in prs] == [3, 7, 9]
    fix, lifecycle, fork = prs
    assert lifecycle.kind is OpenPRKind.LIFECYCLE
    assert lifecycle.author == "vergil-project-automation"
    assert lifecycle.ci == "pending"
    assert lifecycle.head_owner is None
    assert not lifecycle.cross_repository
    assert fix.draft
    assert fix.base == "main"
    assert fix.type_label == "fix"
    assert fix.ci == "none"
    assert fork.cross_repository
    assert fork.head_owner == "stranger"
    assert fork.author == "ghost"
    assert fork.ci == "none"
    assert fix.created_ts == datetime.datetime.fromisoformat(_CREATED).timestamp()
    graphql.assert_called_once()
    assert graphql.call_args.kwargs == {"owner": "o", "name": "r"}
    assert "states: OPEN" in graphql.call_args.args[0]


def test_deleted_fork_owner_is_ghost() -> None:
    prs, _ = _gather(_page([_node(1, cross=True, head_owner=None)]))
    assert prs[0].head_owner == "ghost"


def test_pages_follow_the_cursor() -> None:
    first = _page([_node(1)], total=2, cursor="c1")
    second = _page([_node(2)], total=2)
    prs, graphql = _gather(first, second)
    assert [pr.number for pr in prs] == [1, 2]
    assert graphql.call_count == 2
    assert "cursor" not in graphql.call_args_list[0].kwargs
    assert graphql.call_args_list[1].kwargs["cursor"] == "c1"


def test_short_read_raises() -> None:
    with pytest.raises(OpenPRsError, match="read 1 of 2 open PRs"):
        _gather(_page([_node(1)], total=2))


def test_truncated_labels_raise() -> None:
    labels = tuple(f"l{n}" for n in range(20))
    with pytest.raises(OpenPRsError, match="^labels of #4 truncated$"):
        _gather(_page([_node(4, labels=labels, label_total=21)]))


def test_non_github_remote_raises() -> None:
    url = "https://user:secret@gitlab.example/o/r.git"
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=url),
        patch("vergil_tooling.lib.github.graphql") as graphql,
        pytest.raises(OpenPRsError) as info,
    ):
        gather_open_prs()
    assert str(info.value) == "origin is not a github.com remote"
    graphql.assert_not_called()


def test_api_failure_raises_with_cause() -> None:
    boom = RuntimeError("GraphQL errors: [{'message': 'nope'}]")
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.github.graphql", side_effect=boom),
        pytest.raises(OpenPRsError, match="GraphQL errors") as info,
    ):
        gather_open_prs()
    assert info.value.__cause__ is boom


def test_process_failure_reason_strips_userinfo() -> None:
    exc = subprocess.CalledProcessError(
        128, ["git"], stderr="fatal: unable to access 'https://u:tok123@github.com/o/r.git/'"
    )
    with (
        patch("vergil_tooling.lib.git.remote_url", side_effect=exc),
        pytest.raises(OpenPRsError) as info,
    ):
        gather_open_prs()
    assert "tok123" not in str(info.value)
    assert "https://github.com/o/r.git/" in str(info.value)


def test_missing_token_uses_standard_message() -> None:
    missing = github.MissingGitHubTokenError(1, ["gh"], stderr="GH_TOKEN environment variable")
    with (
        patch("vergil_tooling.lib.git.remote_url", return_value=_URL),
        patch("vergil_tooling.lib.github.graphql", side_effect=missing),
        pytest.raises(OpenPRsError) as info,
    ):
        gather_open_prs()
    assert str(info.value) == github.missing_token_message("vrg-repo-status")


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ({"repository": None}, "repository is not an object"),
        (_page([{**_node(1), "isDraft": "no"}]), "isDraft of #1 is not a boolean"),
        (_page([{**_node(1), "number": True}]), "PR number is not an integer"),
        (_page([{**_node(1), "title": None}]), "title of #1 is not a string"),
        (_page([{**_node(1), "commits": {"nodes": {}}}]), "commits of #1 is not a list"),
        ({"repository": {}}, "KeyError"),
    ],
)
def test_malformed_response_raises(page: dict[str, object], message: str) -> None:
    with pytest.raises(OpenPRsError, match=message):
        _gather(page)
