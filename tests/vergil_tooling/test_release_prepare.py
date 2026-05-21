from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.prepare import (
    _create_pr,
    _generate_changelog,
    _normalize_trailing_newline,
    prepare,
)

if TYPE_CHECKING:
    from pathlib import Path

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


def test_normalize_trailing_newline(tmp_path: Path) -> None:
    path = tmp_path / "test.md"
    path.write_text("hello\n\n\n", encoding="utf-8")
    _normalize_trailing_newline(path)
    assert path.read_text(encoding="utf-8") == "hello\n"


def test_generate_changelog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as _sp

    monkeypatch.chdir(tmp_path)
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=tmp_path,
        version_override=None,
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n")
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / "v2.1.0.md").write_text("notes\n\n")
    cp = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with (
        patch(_MOD + ".subprocess.run", return_value=cp),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value="M CHANGELOG.md"),
    ):
        _generate_changelog(ctx)


def test_generate_changelog_no_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=tmp_path,
        version_override=None,
    )
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n")
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / "v2.1.0.md").write_text("notes\n\n")
    with (
        patch(_MOD + ".subprocess.run"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=""),
        pytest.raises(ReleaseError, match="No publishable changes"),
    ):
        _generate_changelog(ctx)


def test_create_pr(tmp_path: Path) -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=tmp_path,
        version_override=None,
    )
    ctx.issue_number = 42
    with patch(_MOD + ".github.create_pr", return_value="https://github.com/o/r/pull/100"):
        url = _create_pr(ctx)
    assert url == "https://github.com/o/r/pull/100"
