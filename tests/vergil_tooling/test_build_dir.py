"""Tests for ``build/`` and merge-review's private directory below it."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.build_dir import BuildDirError, ensure_build_dir
from vergil_tooling.lib.merge_review.state import StateDirError, ensure_private_dir

if TYPE_CHECKING:
    from pathlib import Path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _levels(root: Path, issue: int = 7) -> list[Path]:
    state = root / "build" / ".vergil-state"
    return [
        state,
        state / "issue-implement",
        state / "issue-implement" / str(issue),
        state / "issue-implement" / str(issue) / "merge-review",
    ]


# -- build ------------------------------------------------------------------------


def test_build_dir_created_when_absent(tmp_path: Path) -> None:
    build = ensure_build_dir(tmp_path)
    assert build == tmp_path / "build"
    assert build.is_dir()


def test_build_dir_kept_when_present(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "keep").write_text("x")
    assert (ensure_build_dir(tmp_path) / "keep").read_text() == "x"


def test_build_dir_refuses_symlink_or_file(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "build").symlink_to(elsewhere)
    with pytest.raises(BuildDirError, match="build is a symlink, not a directory"):
        ensure_build_dir(repo)
    (repo / "build").unlink()
    (repo / "build").write_text("a file")
    with pytest.raises(BuildDirError, match="build is not a directory"):
        ensure_build_dir(repo)


def test_build_dir_refuses_dangling_symlink(tmp_path: Path) -> None:
    (tmp_path / "build").symlink_to(tmp_path / "nowhere")
    with pytest.raises(BuildDirError, match="is a symlink"):
        ensure_build_dir(tmp_path)
    assert not (tmp_path / "nowhere").exists()


# -- the private directory ----------------------------------------------------------


def test_private_dir_created_mode_700(tmp_path: Path) -> None:
    path = ensure_private_dir(tmp_path, 7)
    levels = _levels(tmp_path)
    assert path == levels[-1]
    assert [_mode(level) for level in levels] == [0o700] * 4


def test_build_dir_created_then_the_private_dir(tmp_path: Path) -> None:
    # A fresh clone has no build at all.
    assert not (tmp_path / "build").exists()
    assert ensure_private_dir(tmp_path, 7).is_dir()


def test_private_dir_existing_levels_accepted(tmp_path: Path) -> None:
    first = ensure_private_dir(tmp_path, 7)
    (first / "request-review.json").write_text("{}")
    assert ensure_private_dir(tmp_path, 7) == first
    assert (first / "request-review.json").exists()


def test_private_dir_mode_is_700_whatever_the_umask(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    old = os.umask(0o277)  # mkdir(0o700) alone would give 0o500
    try:
        path = ensure_private_dir(tmp_path, 7)
    finally:
        os.umask(old)
    assert [_mode(level) for level in _levels(tmp_path)] == [0o700] * 4
    assert path.is_dir()


def test_private_dir_refuses_symlinked_level(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    elsewhere.chmod(0o700)
    state = _levels(tmp_path)[0]
    state.mkdir(parents=True)
    state.chmod(0o700)
    (state / "issue-implement").symlink_to(elsewhere)
    with pytest.raises(StateDirError, match="issue-implement is a symlink, not a directory"):
        ensure_private_dir(tmp_path, 7)
    assert list(elsewhere.iterdir()) == []


def test_private_dir_refuses_non_700_level(tmp_path: Path) -> None:
    ensure_private_dir(tmp_path, 7)
    issue = _levels(tmp_path)[2]
    issue.chmod(0o750)
    with pytest.raises(StateDirError, match="7 has mode 750, not 700"):
        ensure_private_dir(tmp_path, 7)


def test_private_dir_refuses_public_root(tmp_path: Path) -> None:
    state = _levels(tmp_path)[0]
    state.mkdir(parents=True)
    state.chmod(0o755)
    with pytest.raises(StateDirError, match=r"\.vergil-state has mode 755, not 700"):
        ensure_private_dir(tmp_path, 7)
    assert list(state.iterdir()) == []


def test_private_dir_refuses_a_file_level(tmp_path: Path) -> None:
    ensure_private_dir(tmp_path, 8)
    (_levels(tmp_path)[1] / "7").write_text("not a directory")
    with pytest.raises(StateDirError, match="7 is not a directory"):
        ensure_private_dir(tmp_path, 7)


def test_private_dir_refuses_symlinked_build(tmp_path: Path) -> None:
    (tmp_path / "build").symlink_to(tmp_path)
    with pytest.raises(BuildDirError):
        ensure_private_dir(tmp_path, 7)


@pytest.mark.parametrize("issue", [0, -1])
def test_private_dir_refuses_non_positive_issue(tmp_path: Path, issue: int) -> None:
    with pytest.raises(StateDirError, match="issue number must be positive"):
        ensure_private_dir(tmp_path, issue)
    assert not (tmp_path / "build").exists()
