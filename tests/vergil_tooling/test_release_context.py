from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError


def test_context_required_fields() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert ctx.repo == "owner/repo"
    assert ctx.version == "2.1.0"
    assert ctx.repo_root == Path("/tmp/repo")  # noqa: S108
    assert ctx.version_override is None


def test_context_optional_fields_default_none() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert ctx.issue_number is None
    assert ctx.release_pr_url is None
    assert ctx.bump_pr_url is None
    assert ctx.tag is None


def test_context_fields_are_mutable() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    ctx.release_pr_url = "https://github.com/owner/repo/pull/100"
    assert ctx.issue_number == 42
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"


def test_context_promote_defaults_true() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert ctx.promote is True


def test_context_worktree_path_defaults_none() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert ctx.worktree_path is None


def test_context_develop_cd_fields_default_none() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert ctx.develop_cd_run_id is None
    assert ctx.develop_cd_run_url is None


def test_work_root_prefers_worktree() -> None:
    """During a release every phase runs in the worktree, so artifact writes
    target it — not repo_root, which finalize chdir's back to (#1626)."""
    worktree = Path("/tmp/repo/.worktrees/release-2.1.0")  # noqa: S108
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
        worktree_path=worktree,
    )
    assert ctx.work_root == worktree
    assert ctx.work_root != ctx.repo_root


def test_work_root_falls_back_to_repo_root() -> None:
    """With no worktree set, work_root is repo_root (defensive fallback)."""
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert ctx.work_root == ctx.repo_root


def test_release_error_carries_diagnostics() -> None:
    err = ReleaseError(
        phase="merge-release",
        command="gh pr merge ...",
        message="CI check failed",
        detail="check 'lint' failed with status 'failure'",
    )
    assert err.phase == "merge-release"
    assert err.command == "gh pr merge ..."
    assert "CI check failed" in str(err)
    assert err.detail == "check 'lint' failed with status 'failure'"


def test_release_error_is_exception() -> None:
    with pytest.raises(ReleaseError, match="something broke"):
        raise ReleaseError(
            phase="prepare",
            command="git push",
            message="something broke",
        )
