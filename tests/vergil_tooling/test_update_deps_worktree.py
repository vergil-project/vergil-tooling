from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.worktree import create_worktree, remove_worktree

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.update_deps.worktree"


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
    with pytest.raises(UpdateDepsError, match="already exists"):
        create_worktree(tmp_path, branch="chore/dep-update-20260610", base="develop")


def test_remove_worktree_force_removes(tmp_path: Path, monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    remove_worktree(tmp_path / ".worktrees" / "x")
    assert ("worktree", "remove", "--force", str(tmp_path / ".worktrees" / "x")) in runs
