"""The stray-merge check (spec §5.4 of epic vergil-project/.github#1664)."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from tests.vergil_tooling.release_stubs import branch_pr
from vergil_tooling.lib import trust
from vergil_tooling.lib.release import stray
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.state import initial_state

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.release.stray"
_CUT = "c" * 40
_ROOT = Path("/tmp/repo")  # noqa: S108
_BACK_MERGE_SHA = "b" * 40
_DEPS_SHA = "d" * 40
_STRAY_SHA = "e" * 40
_NO_PR_SHA = "f" * 40


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(repo="owner/repo", version="2.1.0", repo_root=_ROOT, version_override=None)
    ctx.state = initial_state(version="2.1.0", cut=_CUT, issues=(), without_deps=False)
    return ctx


def _pulls(*nodes: tuple[str, bool, str]) -> dict[str, object]:
    """A GraphQL answer: the commit's associated PRs, as ``(url, merged, base)``."""
    return {
        "repository": {
            "object": {
                "associatedPullRequests": {
                    "nodes": [
                        {"url": url, "merged": merged, "baseRefName": base}
                        for url, merged, base in nodes
                    ]
                }
            }
        }
    }


_STRAY_URL = "https://github.com/owner/repo/pull/77"


class _World:
    """The git log after the cut, the holder's merged PRs and each commit's PRs."""

    def __init__(self, shas: list[str]) -> None:
        self.shas = shas
        self.pulls: dict[str, dict[str, object]] = {
            _STRAY_SHA: _pulls((_STRAY_URL, True, "develop")),
            _NO_PR_SHA: _pulls(),
        }
        self.git: list[tuple[str, ...]] = []
        self.branches: list[str] = []

    def read_output(self, *args: str) -> str:
        self.git.append(args)
        if "rev-list" in args:
            return "\n".join(self.shas)
        return ""

    def prs_for_branch(self, repo: str, branch: str, *, state: str) -> tuple[list[object], list]:
        assert repo == "owner/repo"
        assert state == "merged"
        self.branches.append(branch)
        if branch == "release/post-2.1.0":
            return [replace(branch_pr(5, "MERGED", branch=branch), merge_oid=_BACK_MERGE_SHA)], []
        if branch == "deps/update-2.1.1":
            return [replace(branch_pr(6, "MERGED", branch=branch), merge_oid=_DEPS_SHA)], []
        return [], []

    def graphql(self, query: str, **variables: object) -> dict[str, object]:
        assert "associatedPullRequests" in query
        return self.pulls[str(variables["oid"])]


@pytest.fixture
def world() -> Iterator[_World]:
    found = _World([])
    with (
        patch(_MOD + ".git.read_output", side_effect=found.read_output),
        patch(_MOD + ".trust.prs_for_branch", side_effect=found.prs_for_branch),
        patch(_MOD + ".github.graphql", side_effect=found.graphql),
    ):
        yield found


def test_nothing_after_the_cut_is_no_stray(world: _World) -> None:
    assert stray.stray_merges(_ctx()) == []
    assert ("-C", str(_ROOT), "fetch", "origin", "develop") in world.git
    assert (
        "-C",
        str(_ROOT),
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{_CUT}..origin/develop",
    ) in world.git


def test_holder_back_merge_and_deps_prs_are_not_strays(world: _World) -> None:
    world.shas = [_BACK_MERGE_SHA, _DEPS_SHA]
    assert stray.stray_merges(_ctx()) == []
    assert world.branches == ["release/post-2.1.0", "deps/update-2.1.1"]


def test_another_pr_merged_after_the_cut_is_named(world: _World) -> None:
    world.shas = [_BACK_MERGE_SHA, _STRAY_SHA, _DEPS_SHA]
    assert stray.stray_merges(_ctx()) == [_STRAY_URL]


def test_a_batch_pr_rebased_in_is_named_once(world: _World) -> None:
    other = "a" * 40
    world.pulls[other] = _pulls((_STRAY_URL, True, "develop"))
    world.shas = [_STRAY_SHA, other]
    assert stray.stray_merges(_ctx()) == [_STRAY_URL]


def test_a_commit_with_no_merged_pr_is_named_by_its_sha(world: _World) -> None:
    world.pulls[_NO_PR_SHA] = _pulls(
        ("https://github.com/owner/repo/pull/3", False, "develop"),
        ("https://github.com/owner/repo/pull/4", True, "main"),
    )
    world.shas = [_NO_PR_SHA]
    assert stray.stray_merges(_ctx()) == [f"commit {_NO_PR_SHA[:12]} (no merged PR into develop)"]


def test_cut_read_from_the_state_block_on_a_resume(world: _World) -> None:
    ctx = _ctx()
    ctx.cut = None
    stray.stray_merges(ctx)
    assert any(f"{_CUT}..origin/develop" in args for args in world.git)


def test_a_fresh_run_walks_from_its_own_cut(world: _World) -> None:
    ctx = _ctx()
    ctx.cut = "9" * 40
    stray.stray_merges(ctx)
    assert any(f"{'9' * 40}..origin/develop" in args for args in world.git)


def test_a_failed_holder_lookup_by_gh_raises(world: _World) -> None:
    world.shas = [_BACK_MERGE_SHA]
    failure = subprocess.CalledProcessError(1, ("gh", "api", "graphql"), stderr="HTTP 502")
    with (
        patch(_MOD + ".trust.prs_for_branch", side_effect=failure),
        pytest.raises(ReleaseError, match="release/post-2.1.0") as info,
    ):
        stray.stray_merges(_ctx())
    assert "HTTP 502" in (info.value.detail or "")


def test_an_unexpected_pr_answer_raises(world: _World) -> None:
    world.shas = [_STRAY_SHA]
    world.pulls[_STRAY_SHA] = {"repository": {"object": None}}
    with pytest.raises(ReleaseError, match="not the expected shape"):
        stray.stray_merges(_ctx())


def test_no_cut_known_raises() -> None:
    ctx = _ctx()
    ctx.state = None
    with pytest.raises(ReleaseError, match="cut"):
        stray.stray_merges(ctx)


def test_a_failed_fetch_raises_never_reads_as_no_stray() -> None:
    failure = subprocess.CalledProcessError(1, ("git", "fetch"), stderr="network down")
    with (
        patch(_MOD + ".git.read_output", side_effect=failure),
        pytest.raises(ReleaseError, match="stray-merge check") as info,
    ):
        stray.stray_merges(_ctx())
    assert "network down" in (info.value.detail or "")


def test_a_failed_pr_read_raises(world: _World) -> None:
    world.shas = [_STRAY_SHA]
    with (
        patch(_MOD + ".github.graphql", side_effect=RuntimeError("GraphQL errors: boom")),
        pytest.raises(ReleaseError, match="boom"),
    ):
        stray.stray_merges(_ctx())


def test_a_failed_holder_lookup_raises(world: _World) -> None:
    world.shas = [_BACK_MERGE_SHA]
    lookup = MagicMock(side_effect=trust.TrustReadError("prs_for_branch", "lookup failed"))
    with (
        patch(_MOD + ".trust.prs_for_branch", lookup),
        pytest.raises(ReleaseError, match="lookup failed"),
    ):
        stray.stray_merges(_ctx())
