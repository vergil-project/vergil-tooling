from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib import github
from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.lib.release.bump"


@pytest.fixture(autouse=True)
def _no_closed_pr() -> Iterator[MagicMock]:
    """No closed back-merge PR unless a test says otherwise.

    The real ``pr_for_branch`` returns open PRs only, so every fresh-path test
    also reaches the closed-PR lookup (#3328); default it to "none".
    """
    with patch(_MOD + ".github.closed_pr_for_branch", return_value=None) as m:
        yield m


@pytest.fixture(autouse=True)
def _no_post_branch() -> Iterator[None]:
    """No post branch, local or on origin, unless a test says otherwise.

    The fresh path first looks for a post branch an earlier run left behind
    (#4415); these mocked tests start with none. The resume paths are tested
    against real repositories in ``test_release_bump_resume.py``.
    """
    with (
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.read_output", return_value=""),
    ):
        yield


_MERGE_SHA = "b4c4me4ge5ha"


@pytest.fixture(autouse=True)
def _merge_commit() -> Iterator[MagicMock]:
    """The merged back-merge PR's merge commit, recorded on every path (#3329)."""
    with patch(_MOD + ".github.merge_commit_sha", return_value=_MERGE_SHA) as m:
        yield m


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
        # preflight chdir's into this worktree; the bump must land here.
        worktree_path=Path("/tmp/repo/.worktrees/release-2.1.0"),  # noqa: S108
    )
    ctx.issue_number = 42
    return ctx


def test_back_merge_creates_branch_and_pr() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/101"
    assert ctx.next_version == "2.1.1"


def test_back_merge_fetches_main_first() -> None:
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    fetch_idx = next(i for i, c in enumerate(git_run_calls) if c[0] == "fetch")
    checkout_idx = next(i for i, c in enumerate(git_run_calls) if c[:2] == ("checkout", "-b"))
    assert fetch_idx < checkout_idx


def test_back_merge_commits_version_bump() -> None:
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    commit_calls = [c for c in git_run_calls if c[0] == "commit"]
    assert len(commit_calls) == 1
    assert "bump version to 2.1.1" in commit_calls[0][2]


def test_back_merge_bumps_in_worktree() -> None:
    """The version bump writes into the worktree, not the main checkout (#1626)."""
    ctx = _ctx()
    assert ctx.work_root == ctx.worktree_path
    assert ctx.work_root != ctx.repo_root
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1") as mock_bump,
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    mock_bump.assert_called_once_with(ctx.work_root)


def test_back_merge_creates_pr_to_develop() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr") as mock_pr,
        patch(_MOD + ".wait_and_merge"),
    ):
        mock_pr.return_value = "https://github.com/owner/repo/pull/101"
        back_merge_and_bump(ctx)

    mock_pr.assert_called_once()
    call_kwargs = mock_pr.call_args
    assert call_kwargs.kwargs["base"] == "develop"


def test_back_merge_waits_and_merges() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge") as mock_wm,
    ):
        back_merge_and_bump(ctx)

    mock_wm.assert_called_once_with(
        "https://github.com/owner/repo/pull/101",
        phase="back-merge-bump",
    )


def test_back_merge_skips_when_listed_open_pr_merged_since() -> None:
    """Listed as open, then merged before its state was read: skip the merge."""
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"url": "https://github.com/owner/repo/pull/9"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".version.bump") as m_bump,
        patch(_MOD + ".github.create_pr") as m_pr,
        patch(_MOD + ".wait_and_merge") as m_wm,
    ):
        back_merge_and_bump(ctx)
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/9"
    assert ctx.next_version == "2.1.1"
    m_bump.assert_not_called()
    m_pr.assert_not_called()
    m_wm.assert_not_called()


def test_back_merge_adopts_already_merged_pr_on_resume(_no_closed_pr: MagicMock) -> None:
    """Resume after the back-merge merged: adopt it, never re-create the branch (#3328).

    The real ``pr_for_branch`` returns open PRs only, so a merged back-merge PR
    is found through the closed-PR lookup. The version is read from its pull
    ref, which outlives a deleted head branch.
    """
    ctx = _ctx()
    _no_closed_pr.return_value = {
        "number": "951",
        "url": "https://github.com/owner/repo/pull/951",
        "headRefOid": "3bd942f2",
    }
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".version.show", return_value="2.1.1") as m_show,
        patch(_MOD + ".version.bump") as m_bump,
        patch(_MOD + ".github.create_pr") as m_pr,
        patch(_MOD + ".wait_and_merge") as m_wm,
    ):
        back_merge_and_bump(ctx)

    _no_closed_pr.assert_called_once_with("release/post-2.1.0")
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/951"
    assert ctx.next_version == "2.1.1"
    assert git_run_calls == [("fetch", "origin", "pull/951/head")]
    m_show.assert_called_once_with(ctx.repo_root, ref="FETCH_HEAD")
    m_bump.assert_not_called()
    m_pr.assert_not_called()
    m_wm.assert_not_called()


def test_back_merge_ignores_closed_unmerged_pr(_no_closed_pr: MagicMock) -> None:
    """A closed but unmerged back-merge PR is not adopted: the fresh path runs."""
    ctx = _ctx()
    _no_closed_pr.return_value = {
        "number": "950",
        "url": "https://github.com/owner/repo/pull/950",
        "headRefOid": "abc123",
    }
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.pr_state", return_value="CLOSED"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ) as m_pr,
        patch(_MOD + ".wait_and_merge") as m_wm,
    ):
        back_merge_and_bump(ctx)

    m_pr.assert_called_once()
    m_wm.assert_called_once_with("https://github.com/owner/repo/pull/101", phase="back-merge-bump")
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/101"
    assert ctx.next_version == "2.1.1"


def test_back_merge_open_pr_skips_closed_lookup(_no_closed_pr: MagicMock) -> None:
    """An open back-merge PR is adopted directly; the closed lookup never runs."""
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"url": "https://github.com/owner/repo/pull/9"},
        ),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)
    _no_closed_pr.assert_not_called()


def test_back_merge_merges_existing_open_pr() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"url": "https://github.com/owner/repo/pull/9"},
        ),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr") as m_pr,
        patch(_MOD + ".wait_and_merge") as m_wm,
    ):
        back_merge_and_bump(ctx)
    m_wm.assert_called_once_with("https://github.com/owner/repo/pull/9", phase="back-merge-bump")
    m_pr.assert_not_called()
    assert ctx.next_version == "2.1.1"


def test_back_merge_existing_pr_state_retries_404() -> None:
    """The PR URL was just listed by GitHub, so a 404 on reading it is lag (#3137)."""
    ctx = _ctx()
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404: Not Found")
    with (
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"url": "https://github.com/owner/repo/pull/9"},
        ),
        patch(_MOD + ".github.pr_state", side_effect=[err, "MERGED"]) as m_state,
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".wait_and_merge") as m_wm,
        patch("vergil_tooling.lib.retry.time.sleep"),
    ):
        back_merge_and_bump(ctx)
    assert m_state.call_count == 2
    m_wm.assert_not_called()


def test_post_branch_version_reads_from_fetch_head() -> None:
    from vergil_tooling.lib.release.bump import _post_branch_version

    ctx = _ctx()
    with (
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + ".version.show", return_value="2.1.1") as m_show,
    ):
        assert _post_branch_version(ctx, "release/post-2.1.0") == "2.1.1"
    m_run.assert_called_once_with("fetch", "origin", "release/post-2.1.0")
    m_show.assert_called_once_with(ctx.repo_root, ref="FETCH_HEAD")


def test_back_merge_never_returns_to_develop() -> None:
    """Work happens in the release worktree, so back-merge never checks out
    develop in the root checkout (#1578). The root's develop is synced later
    by the finalize cleanup stage. The only checkout is the worktree's own
    `checkout -b release/post-<v>`."""
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    checkout_calls = [c for c in git_run_calls if c and c[0] == "checkout"]
    assert checkout_calls == [("checkout", "-b", "release/post-2.1.0", "origin/main")]
    assert ("checkout", "develop") not in git_run_calls
    assert not any("merge" in c for c in git_run_calls)
    assert not any(c[0] == "pull" for c in git_run_calls)


# --- #3329: back-merge-bump records the back-merge's merge commit SHA ---


def test_back_merge_create_path_records_merge_sha(_merge_commit: MagicMock) -> None:
    """The fresh path reads the merge commit only after the PR merged."""
    ctx = _ctx()
    order: list[str] = []
    _merge_commit.side_effect = lambda _url: order.append("sha") or _MERGE_SHA
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr", return_value="https://github.com/owner/repo/pull/101"),
        patch(_MOD + ".wait_and_merge", side_effect=lambda *_a, **_k: order.append("merge")),
    ):
        back_merge_and_bump(ctx)
    _merge_commit.assert_called_once_with("https://github.com/owner/repo/pull/101")
    assert order == ["merge", "sha"]
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_adopt_open_path_records_merge_sha(_merge_commit: MagicMock) -> None:
    """Adopting a still-open PR merges it, then records its merge commit."""
    ctx = _ctx()
    order: list[str] = []
    _merge_commit.side_effect = lambda _url: order.append("sha") or _MERGE_SHA
    with (
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"url": "https://github.com/owner/repo/pull/9"},
        ),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".wait_and_merge", side_effect=lambda *_a, **_k: order.append("merge")),
    ):
        back_merge_and_bump(ctx)
    _merge_commit.assert_called_once_with("https://github.com/owner/repo/pull/9")
    assert order == ["merge", "sha"]
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_adopt_listed_open_but_merged_records_merge_sha(
    _merge_commit: MagicMock,
) -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"url": "https://github.com/owner/repo/pull/9"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + "._post_branch_version", return_value="2.1.1"),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)
    _merge_commit.assert_called_once_with("https://github.com/owner/repo/pull/9")
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_adopt_merged_path_records_merge_sha(
    _no_closed_pr: MagicMock, _merge_commit: MagicMock
) -> None:
    """Resume after the back-merge merged: the adopted PR's merge commit is recorded."""
    ctx = _ctx()
    _no_closed_pr.return_value = {
        "number": "951",
        "url": "https://github.com/owner/repo/pull/951",
        "headRefOid": "3bd942f2",
    }
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".version.show", return_value="2.1.1"),
    ):
        back_merge_and_bump(ctx)
    _merge_commit.assert_called_once_with("https://github.com/owner/repo/pull/951")
    assert ctx.bump_merge_sha == _MERGE_SHA


def test_back_merge_records_none_when_no_merge_commit(_merge_commit: MagicMock) -> None:
    """An empty mergeCommit is recorded as None, never as an empty SHA."""
    ctx = _ctx()
    _merge_commit.return_value = ""
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr", return_value="https://github.com/owner/repo/pull/101"),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)
    assert ctx.bump_merge_sha is None


# --- a gh pr create that failed: adopt the PR it opened, or fail (#4699) ---

_BUMP_PR = "https://github.com/owner/repo/pull/101"
_BUMP_BRANCH = "release/post-2.1.0"
_BUMP_HEAD = "b0mp"
_TIMEOUT = github.GitHubAPIError(1, ("gh", "pr", "create"), "", "HTTP 504: Gateway Timeout")
_OPEN_BUMP_PR = github.OpenPR(
    url=_BUMP_PR,
    title="chore(release): back-merge 2.1.0 and bump to 2.1.1",
    head_ref=_BUMP_BRANCH,
    base_ref="develop",
    head_oid=_BUMP_HEAD,
    cross_repo=False,
)


def _bump_with_create(
    create: MagicMock, open_prs: list[object]
) -> tuple[ReleaseContext, MagicMock, MagicMock, MagicMock]:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=_BUMP_HEAD),
        patch(_MOD + "._checkout_post_branch", return_value=None),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr", create),
        patch(_MOD + ".github.open_prs_for_head", side_effect=open_prs) as lookup,
        patch("vergil_tooling.lib.pr_create.time.sleep") as sleep,
        patch(_MOD + ".wait_and_merge") as wait,
    ):
        back_merge_and_bump(ctx)
    return ctx, lookup, sleep, wait


def test_back_merge_opens_the_pr_with_one_gh_attempt_per_try() -> None:
    create = MagicMock(return_value=_BUMP_PR)
    _ctx_, lookup, _sleep, _wait = _bump_with_create(create, [])
    assert create.call_args.kwargs["retry"] is False
    lookup.assert_not_called()


def test_back_merge_adopts_the_pr_a_failed_create_opened() -> None:
    create = MagicMock(side_effect=[_TIMEOUT])
    ctx, lookup, sleep, wait = _bump_with_create(create, [[_OPEN_BUMP_PR]])
    assert ctx.bump_pr_url == _BUMP_PR
    create.assert_called_once()
    lookup.assert_called_once_with(_BUMP_BRANCH, base="develop")
    sleep.assert_not_called()
    wait.assert_called_once_with(_BUMP_PR, phase="back-merge-bump")


def test_back_merge_refuses_an_open_pr_it_did_not_open() -> None:
    other = dataclasses.replace(_OPEN_BUMP_PR, title="something else")
    with pytest.raises(ReleaseError, match="was not adopted") as exc:
        _bump_with_create(MagicMock(side_effect=[_TIMEOUT]), [[other]])
    assert exc.value.phase == "back-merge-bump"
    assert _BUMP_PR in str(exc.value)
    assert "its title is 'something else'" in str(exc.value)


def test_back_merge_create_that_really_failed_is_retried() -> None:
    create = MagicMock(side_effect=[_TIMEOUT, _BUMP_PR])
    ctx, lookup, sleep, wait = _bump_with_create(create, [[]])
    assert ctx.bump_pr_url == _BUMP_PR
    assert create.call_count == 2
    lookup.assert_called_once()
    sleep.assert_called_once()
    wait.assert_called_once_with(_BUMP_PR, phase="back-merge-bump")
