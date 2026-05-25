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
    )
    ctx.issue_number = 42
    return ctx


def test_back_merge_creates_branch_and_pr() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
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


def test_back_merge_creates_pr_to_develop() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
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
        verbose=False,
    )


def test_back_merge_pulls_develop_after_merge() -> None:
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    develop_checkout_idx = next(
        i for i, c in enumerate(git_run_calls) if c == ("checkout", "develop")
    )
    pull_idx = next(i for i, c in enumerate(git_run_calls) if c[:2] == ("pull", "origin"))
    assert pull_idx > develop_checkout_idx
