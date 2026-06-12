from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.context import ReleaseContext

_MOD = "vergil_tooling.lib.release.bump"


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


def test_back_merge_skips_when_pr_already_merged() -> None:
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
