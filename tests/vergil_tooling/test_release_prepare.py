from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.prepare import prepare

_MOD = "vergil_tooling.lib.release.prepare"


def _ctx(*, version_override: str | None = None) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=version_override,
        # preflight creates the worktree and sets the release branch.
        release_branch="release/2.1.0",
    )
    ctx.issue_number = 42
    ctx.issue_url = "https://github.com/owner/repo/issues/42"
    return ctx


def test_prepare_pushes_branch_and_opens_pr() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".git.run"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)
    assert ctx.release_branch == "release/2.1.0"
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"


def test_prepare_fails_without_release_branch() -> None:
    """preflight owns branch creation; prepare refuses if it did not run."""
    ctx = _ctx()
    ctx.release_branch = None
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        pytest.raises(ReleaseError, match="No release branch"),
    ):
        prepare(ctx)


def test_prepare_never_switches_head() -> None:
    """The branch is the worktree's own — prepare never checks out (#1578)."""
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

    checkout_calls = [c for c in git_run_calls if c and c[0] == "checkout"]
    assert checkout_calls == [], f"Unexpected checkout calls: {checkout_calls}"
    merge_calls = [c for c in git_run_calls if "merge" in c]
    assert merge_calls == [], f"Unexpected merge calls: {merge_calls}"


def test_prepare_with_version_override() -> None:
    """Version override bumps on the release branch before changelog."""
    ctx = _ctx(version_override="minor")
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.0"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

    commit_calls = [c for c in git_run_calls if c[0] == "commit"]
    assert len(commit_calls) == 1
    assert "bump version to 2.1.0" in commit_calls[0][2]


def test_prepare_without_version_override_skips_bump() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump") as mock_bump,
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)
    mock_bump.assert_not_called()


def test_prepare_creates_issue_when_not_yet_adopted() -> None:
    ctx = _ctx()
    ctx.issue_number = None
    with (
        patch(_MOD + ".create_tracking_issue") as m_create,
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".git.run"),
        patch(_MOD + "._generate_changelog"),
        patch(_MOD + ".github.create_pr", return_value="https://github.com/o/r/pull/1"),
    ):
        prepare(ctx)
    m_create.assert_called_once_with(ctx)


def test_prepare_skips_when_release_pr_already_exists() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"url": "https://github.com/owner/repo/pull/100"},
        ),
        patch(_MOD + ".git.run") as m_run,
        patch(_MOD + "._generate_changelog") as m_changelog,
        patch(_MOD + ".github.create_pr") as m_create_pr,
    ):
        prepare(ctx)
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"
    m_changelog.assert_not_called()
    m_create_pr.assert_not_called()
    m_run.assert_not_called()


def test_generate_changelog_skips_when_prepare_commit_present() -> None:
    from vergil_tooling.lib.release.prepare import _generate_changelog

    ctx = _ctx()
    with (
        patch(
            _MOD + ".git.read_output",
            return_value="chore(release): prepare 2.1.0",
        ),
        patch(_MOD + ".changelog.generate_changelog") as m_cl,
        patch(_MOD + ".git.run") as m_run,
    ):
        _generate_changelog(ctx)
    m_cl.assert_not_called()
    m_run.assert_not_called()


def test_generate_changelog_uses_lib() -> None:
    from vergil_tooling.lib.release.prepare import _generate_changelog

    ctx = _ctx()
    notes_path = Path("/tmp/repo/releases/v2.1.0.md")  # noqa: S108
    with (
        patch(_MOD + ".changelog.generate_changelog") as mock_cl,
        patch(
            _MOD + ".changelog.generate_release_notes",
            return_value=notes_path,
        ) as mock_rn,
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value="M CHANGELOG.md"),
    ):
        _generate_changelog(ctx)
    mock_cl.assert_called_once_with(ctx.repo_root, ctx.version)
    mock_rn.assert_called_once_with(ctx.repo_root, ctx.version)


def test_generate_changelog_fails_on_no_changes() -> None:
    from vergil_tooling.lib.release.prepare import _generate_changelog

    ctx = _ctx()
    notes_path = Path("/tmp/repo/releases/v2.1.0.md")  # noqa: S108
    with (
        patch(_MOD + ".changelog.generate_changelog"),
        patch(
            _MOD + ".changelog.generate_release_notes",
            return_value=notes_path,
        ),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=""),
        pytest.raises(ReleaseError, match="No publishable changes"),
    ):
        _generate_changelog(ctx)
