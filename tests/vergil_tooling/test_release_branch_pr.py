"""The release's PR lookups by branch, through the real trust layer (GHSA-r4j2-cwcp-xg56)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from tests.vergil_tooling.trust_stubs import AGENT, AUTHORS, CREATED, OUTSIDER, SPAM_TEXT
from vergil_tooling.lib import trust
from vergil_tooling.lib.release.branch_pr import branch_prs
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO = "owner/repo"
_BRANCH = "release/2.1.0"


def _pr(number: int, *, author: Any = AGENT, fork: bool = False, state: str = "OPEN") -> Any:
    """A PR node as the branch query returns it, a fork's titled with synthetic spam."""
    return {
        "number": number,
        "url": f"https://github.com/{_REPO}/pull/{number}",
        "title": SPAM_TEXT if fork or author is OUTSIDER else f"release {number}",
        "headRefName": _BRANCH,
        "headRefOid": f"{number:040x}",
        "baseRefName": "main",
        "state": state,
        "createdAt": CREATED,
        "author": author,
        "mergeCommit": None,
        "isCrossRepository": fork,
        "headRepositoryOwner": {"login": "mallory" if fork else "owner"},
        "headRepository": {"name": "repo"},
    }


@pytest.fixture
def graphql() -> Iterator[list[Any]]:
    """Answer the branch query with one page of the nodes a test appends."""
    nodes: list[Any] = []

    def fake(query: str, **variables: object) -> dict[str, Any]:
        assert query == trust._BRANCH_QUERIES["all"]
        assert variables == {"owner": "owner", "name": "repo", "branch": _BRANCH}
        page = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}
        return {"repository": {"pullRequests": page}}

    with (
        patch("vergil_tooling.lib.github.graphql", side_effect=fake),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=AUTHORS),
    ):
        yield nodes


def test_pr_for_branch_reports_fork(
    graphql: list[Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)  # per-item report, not counts
    graphql.extend([_pr(7, fork=True), _pr(5)])
    prs = branch_prs(_REPO, _BRANCH, phase="prepare")
    assert prs.open_pr is not None
    assert prs.open_pr.number == 5
    assert prs.latest_closed is None
    out = capsys.readouterr()
    assert out.err == (
        f"skipped untrusted content: {_REPO}#7 pr {AGENT['login']} at {CREATED} on head"
        f" ({trust.CROSS_REPOSITORY})\n"
    )
    assert "SPAM-MARKER-TEXT" not in out.err + out.out


def test_pr_for_branch_reports_fork_under_actions_as_counts(
    graphql: list[Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    graphql.extend([_pr(7, fork=True)])
    prs = branch_prs(_REPO, _BRANCH, phase="prepare")
    assert prs.open_pr is None
    err = capsys.readouterr().err
    assert err == f"skipped 1 untrusted item(s) ({trust.CROSS_REPOSITORY}: 1)\n"


def test_pr_for_branch_skips_untrusted_author(
    graphql: list[Any], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    graphql.extend([_pr(8, author=OUTSIDER, state="MERGED")])
    prs = branch_prs(_REPO, _BRANCH, phase="prepare")
    assert prs == type(prs)(open_pr=None, latest_closed=None)
    out = capsys.readouterr()
    assert f"{_REPO}#8 pr {OUTSIDER['login']}" in out.err
    assert "SPAM-MARKER-TEXT" not in out.err + out.out


def test_pr_for_branch_latest_closed_is_the_highest_numbered(graphql: list[Any]) -> None:
    graphql.extend([_pr(3, state="MERGED"), _pr(9, state="CLOSED"), _pr(6, state="MERGED")])
    prs = branch_prs(_REPO, _BRANCH, phase="prepare")
    assert prs.open_pr is None
    assert prs.latest_closed is not None
    assert (prs.latest_closed.number, prs.latest_closed.state) == (9, "CLOSED")


def test_pr_for_branch_refuses_several_open(graphql: list[Any]) -> None:
    graphql.extend([_pr(4), _pr(5)])
    with pytest.raises(ReleaseError, match=r"2 open PRs on 'release/2\.1\.0' \(#4, #5\)") as info:
        branch_prs(_REPO, _BRANCH, phase="back-merge-bump")
    assert info.value.phase == "back-merge-bump"


def test_pr_for_branch_truncated_read_is_a_release_error() -> None:
    def truncated(_query: str, **_variables: object) -> dict[str, Any]:
        page = {"pageInfo": {"hasNextPage": True, "endCursor": None}, "nodes": []}
        return {"repository": {"pullRequests": page}}

    with (
        patch("vergil_tooling.lib.github.graphql", side_effect=truncated),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=AUTHORS),
        pytest.raises(ReleaseError, match="Could not read the PRs") as info,
    ):
        branch_prs(_REPO, _BRANCH, phase="prepare")
    assert info.value.command == f"prs_for_branch {_REPO} {_BRANCH}"
    assert info.value.detail


def test_pr_for_branch_missing_configuration_is_a_release_error() -> None:
    with (
        patch(
            "vergil_tooling.lib.trust.load_trusted_authors",
            side_effect=TrustedAuthorsNotConfiguredError("no configuration"),
        ),
        pytest.raises(ReleaseError, match="Could not read the PRs"),
    ):
        branch_prs(_REPO, _BRANCH, phase="prepare")
