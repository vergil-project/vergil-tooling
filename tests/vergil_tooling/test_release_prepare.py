from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.prepare import prepare

_MOD = "vergil_tooling.lib.release.prepare"


def _ctx(tmp_path: Path) -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=tmp_path,
        version_override=None,
    )


def test_prepare_creates_issue_branch_and_pr(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    git_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.read_output", return_value="abc1234"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

    assert ("checkout", "-b", "release/2.1.0") in git_calls
    assert ctx.release_branch == "release/2.1.0"
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"


def test_prepare_creates_tracking_issue_first(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    call_order: list[str] = []

    def track_issue(c: ReleaseContext) -> None:
        call_order.append("create_tracking_issue")
        c.issue_number = 42
        c.issue_url = "https://github.com/owner/repo/issues/42"

    def track_git(*args: str) -> None:
        call_order.append(f"git.run:{args[0]}")

    with (
        patch(_MOD + ".create_tracking_issue", side_effect=track_issue),
        patch(_MOD + ".git.run", side_effect=track_git),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.read_output", return_value="abc1234"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)
    assert call_order[0] == "create_tracking_issue"


def test_prepare_fails_if_branch_exists(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        pytest.raises(ReleaseError, match="already exists"),
    ):
        prepare(ctx)


def test_prepare_fails_if_no_changes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(
            _MOD + "._generate_changelog",
            side_effect=ReleaseError(
                phase="prepare",
                command="git-cliff",
                message="No publishable changes.",
            ),
        ),
        pytest.raises(ReleaseError, match="publishable"),
    ):
        prepare(ctx)
