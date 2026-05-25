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
    )
    ctx.issue_number = 42
    ctx.issue_url = "https://github.com/owner/repo/issues/42"
    return ctx


def test_prepare_creates_branch_and_pr() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=False),
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


def test_prepare_fails_if_branch_exists() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        pytest.raises(ReleaseError, match="already exists"),
    ):
        prepare(ctx)


def test_prepare_does_not_merge_main() -> None:
    """Verify the -X ours merge of origin/main is removed."""
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

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
        patch(_MOD + ".git.ref_exists", return_value=False),
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
        patch(_MOD + ".git.ref_exists", return_value=False),
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
