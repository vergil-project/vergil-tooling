"""Tests for vergil_tooling.lib.release.conflict (#3526).

Real git repositories reproduce the conflict: a release branch whose
CHANGELOG.md was regenerated, and a main that moved underneath it.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.conflict import merge_main_into_release
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from pathlib import Path

_PR = "https://github.com/o/r/pull/5"
_BRANCH = "release/1.2.3"
_HEAD_SHA = "vergil_tooling.lib.release.merge.github.head_sha"
_RELEASE_CHANGELOG = "# Changelog\n\n## [1.2.3]\n\n- release entry\n\n## [1.2.2]\n"
_MAIN_CHANGELOG = "# Changelog\n\n## [1.2.2] - edited on main\n"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ("git", *args), cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (repo / name).write_text(text)
        _git(repo, "add", name)
    _git(repo, "commit", "-qm", message)


@pytest.fixture
def repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    """An origin, a release clone on _BRANCH, and a second clone that moves main."""
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    release = tmp_path / "release"
    _git(tmp_path, "clone", "-q", str(origin), str(release))
    _git(release, "checkout", "-q", "-b", "main")
    _commit(release, {"CHANGELOG.md": "# Changelog\n\n## [1.2.2]\n", "src.txt": "v1\n"}, "base")
    _git(release, "push", "-q", "origin", "main")
    _git(release, "checkout", "-q", "-b", _BRANCH)
    _commit(release, {"CHANGELOG.md": _RELEASE_CHANGELOG}, "chore(release): prepare 1.2.3")
    _git(release, "push", "-q", "-u", "origin", _BRANCH)
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(origin), str(other))
    _git(other, "checkout", "-q", "main")
    monkeypatch.chdir(release)
    return release, other


def _advance_main(other: Path, files: dict[str, str]) -> None:
    _commit(other, files, "main moved")
    _git(other, "push", "-q", "origin", "main")


def _heal(release: Path) -> None:
    merge_main_into_release(_PR, branch=_BRANCH, worktree=release, phase="merge-release")


def _remote_tip(release: Path) -> str:
    return _git(release, "ls-remote", "origin", f"refs/heads/{_BRANCH}").split()[0]


def _merge_in_progress(release: Path) -> bool:
    return (release / ".git" / "MERGE_HEAD").exists()


def test_changelog_only_conflict_resolves_and_pushes(repos: tuple[Path, Path]) -> None:
    """Regression (#3526): main moved with a CHANGELOG-only conflict."""
    release, other = repos
    _advance_main(other, {"CHANGELOG.md": _MAIN_CHANGELOG})
    _heal(release)
    head = _git(release, "rev-parse", "HEAD")
    assert len(_git(release, "rev-list", "--parents", "-n1", "HEAD").split()) == 3
    assert _git(release, "log", "-1", "--format=%s") == f"chore(release): merge main into {_BRANCH}"
    assert (release / "CHANGELOG.md").read_text() == _RELEASE_CHANGELOG
    assert _git(release, "merge-base", "--is-ancestor", "origin/main", "HEAD") == ""
    assert _remote_tip(release) == head
    assert not _merge_in_progress(release)


def test_other_conflict_aborts_with_recovery_steps(repos: tuple[Path, Path]) -> None:
    release, other = repos
    _commit(release, {"src.txt": "release\n"}, "release edit")
    _git(release, "push", "-q", "origin", _BRANCH)
    before = _git(release, "rev-parse", "HEAD")
    _advance_main(other, {"CHANGELOG.md": _MAIN_CHANGELOG, "src.txt": "main\n"})
    with pytest.raises(ReleaseError) as excinfo:
        _heal(release)
    message = str(excinfo.value)
    assert "  CHANGELOG.md\n  src.txt\n" in message
    assert f"cd {release}" in message
    assert f"git push origin {_BRANCH}" in message
    assert "vrg-release --resume" in message
    assert excinfo.value.phase == "merge-release"
    assert not _merge_in_progress(release)
    assert _git(release, "rev-parse", "HEAD") == before
    assert _remote_tip(release) == before
    assert _git(release, "status", "--porcelain") == ""


def test_clean_merge_is_pushed(repos: tuple[Path, Path]) -> None:
    release, other = repos
    _advance_main(other, {"hotfix.txt": "hotfix\n"})
    _heal(release)
    assert (release / "hotfix.txt").read_text() == "hotfix\n"
    assert _remote_tip(release) == _git(release, "rev-parse", "HEAD")


def test_rerun_is_idempotent(repos: tuple[Path, Path]) -> None:
    release, other = repos
    _advance_main(other, {"CHANGELOG.md": _MAIN_CHANGELOG})
    _heal(release)
    head = _git(release, "rev-parse", "HEAD")
    _heal(release)
    assert _git(release, "rev-parse", "HEAD") == head
    assert _remote_tip(release) == head


def test_rerun_pushes_a_local_merge_a_prior_run_did_not_push(repos: tuple[Path, Path]) -> None:
    release, other = repos
    _advance_main(other, {"hotfix.txt": "hotfix\n"})
    _git(release, "fetch", "-q", "origin", "main")
    _git(release, "merge", "-q", "--no-ff", "--no-edit", "origin/main")
    local = _git(release, "rev-parse", "HEAD")
    with patch(_HEAD_SHA) as head_sha:
        _heal(release)
    head_sha.assert_not_called()
    assert _git(release, "rev-parse", "HEAD") == local
    assert _remote_tip(release) == local


def test_syncs_a_server_updated_branch_before_merging(repos: tuple[Path, Path]) -> None:
    release, other = repos
    # GitHub's update-branch added a commit to the PR branch the release
    # clone never saw; then main moved again with a CHANGELOG conflict.
    _git(other, "fetch", "-q", "origin", _BRANCH)
    _git(other, "checkout", "-q", "-b", _BRANCH, f"origin/{_BRANCH}")
    _commit(other, {"server.txt": "server\n"}, "server update")
    _git(other, "push", "-q", "origin", _BRANCH)
    server_head = _git(other, "rev-parse", "HEAD")
    _git(other, "checkout", "-q", "main")
    _advance_main(other, {"CHANGELOG.md": _MAIN_CHANGELOG})
    with patch(_HEAD_SHA, return_value=server_head):
        _heal(release)
    assert (release / "server.txt").read_text() == "server\n"
    assert (release / "CHANGELOG.md").read_text() == _RELEASE_CHANGELOG
    assert _git(release, "merge-base", "--is-ancestor", server_head, "HEAD") == ""
    assert _remote_tip(release) == _git(release, "rev-parse", "HEAD")


def test_refuses_when_a_merge_is_already_in_progress(repos: tuple[Path, Path]) -> None:
    release, other = repos
    _advance_main(other, {"CHANGELOG.md": _MAIN_CHANGELOG})
    _git(release, "fetch", "-q", "origin", "main")
    subprocess.run(("git", "merge", "origin/main"), cwd=release, check=False, capture_output=True)
    assert _merge_in_progress(release)
    with pytest.raises(ReleaseError, match="merge is already in progress"):
        _heal(release)
    assert _merge_in_progress(release)


def test_merge_failing_without_conflict_raises(repos: tuple[Path, Path]) -> None:
    release, other = repos
    _advance_main(other, {"src.txt": "main\n"})
    # An uncommitted local edit the merge would overwrite: git refuses up front.
    (release / "src.txt").write_text("dirty\n")
    with pytest.raises(ReleaseError, match="failed without a conflict") as excinfo:
        _heal(release)
    assert excinfo.value.detail
    assert not _merge_in_progress(release)
    assert (release / "src.txt").read_text() == "dirty\n"
