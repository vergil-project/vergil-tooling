from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.managed_worktree import (
    ManagedWorktreeError,
    adopt_worktree,
    create_worktree,
    remove_worktree,
    worktree_path,
)

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.managed_worktree"


def test_worktree_path_slugifies_branch(tmp_path: Path) -> None:
    path = worktree_path(tmp_path, "release/2.1.0")
    assert path == tmp_path / ".worktrees" / "release-2.1.0"


def test_create_worktree_adds_off_base(tmp_path: Path, monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    path = create_worktree(tmp_path, branch="chore/dep-update-20260610", base="develop")
    expected = tmp_path / ".worktrees" / "chore-dep-update-20260610"
    assert path == expected
    assert (
        "worktree",
        "add",
        "-b",
        "chore/dep-update-20260610",
        str(expected),
        "develop",
    ) in runs


def test_create_worktree_rejects_existing_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    target = tmp_path / ".worktrees" / "chore-dep-update-20260610"
    target.mkdir(parents=True)
    with pytest.raises(ManagedWorktreeError, match="already exists"):
        create_worktree(tmp_path, branch="chore/dep-update-20260610", base="develop")


def test_adopt_worktree_attaches_to_existing_branch(tmp_path: Path, monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    path = adopt_worktree(tmp_path, branch="release/2.1.0")
    expected = tmp_path / ".worktrees" / "release-2.1.0"
    assert path == expected
    # No -b: attaches to the existing branch rather than creating one.
    assert ("worktree", "add", str(expected), "release/2.1.0") in runs


def test_adopt_worktree_reuses_existing_path(tmp_path: Path, monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    target = tmp_path / ".worktrees" / "release-2.1.0"
    target.mkdir(parents=True)
    path = adopt_worktree(tmp_path, branch="release/2.1.0")
    assert path == target
    assert runs == []  # path already there — nothing to add


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S607


def test_adopt_worktree_names_a_branch_checked_out_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A real repo: the release branch is checked out in another worktree, so
    # git refuses the add. The caller gets a ManagedWorktreeError naming the
    # branch, the path and the recovery, with git's refusal as its detail (#4442).
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "init",
    )
    _git(repo, "branch", "release/2.1.0")
    _git(repo, "worktree", "add", str(tmp_path / "elsewhere"), "release/2.1.0")
    monkeypatch.chdir(repo)
    with pytest.raises(ManagedWorktreeError) as info:
        adopt_worktree(repo, branch="release/2.1.0")
    message = str(info.value)
    assert "release/2.1.0" in message
    assert str(repo / ".worktrees" / "release-2.1.0") in message
    assert "git worktree list" in message
    assert info.value.detail is not None
    assert "returned non-zero exit status" in info.value.detail
    assert "stderr:" in info.value.detail
    assert not (repo / ".worktrees" / "release-2.1.0").exists()


def test_adopt_worktree_wraps_any_add_failure(tmp_path: Path, monkeypatch) -> None:
    def _fail(*args: str) -> None:
        raise subprocess.CalledProcessError(128, ("git", *args), stderr="fatal: boom")

    monkeypatch.setattr(_MOD + ".git.run", _fail)
    with pytest.raises(ManagedWorktreeError, match="Could not attach") as info:
        adopt_worktree(tmp_path, branch="release/2.1.0")
    assert info.value.detail is not None
    assert "stderr: fatal: boom" in info.value.detail
    assert isinstance(info.value.__cause__, subprocess.CalledProcessError)


def test_remove_worktree_force_removes(tmp_path: Path, monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    remove_worktree(tmp_path / ".worktrees" / "x")
    assert ("worktree", "remove", "--force", str(tmp_path / ".worktrees" / "x")) in runs
