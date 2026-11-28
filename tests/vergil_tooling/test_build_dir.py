"""Tests for ``build/`` and merge-review's private directory below it."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.build_dir import BuildDirError, ensure_build_dir
from vergil_tooling.lib.merge_review.state import (
    StateDirError,
    ensure_design_review_dir,
    ensure_hoshin_dir,
    ensure_issue_dir,
    ensure_private_dir,
    ensure_task_dir,
    existing_issue_dir,
)

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


# -- the issue's own directory ----------------------------------------------------


def test_issue_dir_created_mode_700_without_merge_review(tmp_path: Path) -> None:
    path = ensure_issue_dir(tmp_path, 7)
    assert path == _levels(tmp_path)[2]
    assert [_mode(level) for level in _levels(tmp_path)[:3]] == [0o700] * 3
    assert not _levels(tmp_path)[3].exists()


def test_existing_issue_dir_creates_nothing(tmp_path: Path) -> None:
    assert existing_issue_dir(tmp_path, 7) is None
    assert not (tmp_path / "build").exists()
    (tmp_path / "build").mkdir()
    assert existing_issue_dir(tmp_path, 7) is None
    ensure_issue_dir(tmp_path, 8)
    assert existing_issue_dir(tmp_path, 7) is None
    assert not _levels(tmp_path)[2].exists()


def test_existing_issue_dir_returns_a_private_dir(tmp_path: Path) -> None:
    ensure_issue_dir(tmp_path, 7)
    assert existing_issue_dir(tmp_path, 7) == _levels(tmp_path)[2]


def test_existing_issue_dir_refuses_a_public_level(tmp_path: Path) -> None:
    ensure_issue_dir(tmp_path, 7)
    _levels(tmp_path)[1].chmod(0o755)
    with pytest.raises(StateDirError, match="issue-implement has mode 755, not 700"):
        existing_issue_dir(tmp_path, 7)


def test_existing_issue_dir_refuses_a_dangling_symlink_level(tmp_path: Path) -> None:
    ensure_issue_dir(tmp_path, 8)
    _levels(tmp_path)[2].symlink_to(tmp_path / "missing")
    with pytest.raises(StateDirError, match="7 is a symlink, not a directory"):
        existing_issue_dir(tmp_path, 7)


def test_existing_issue_dir_refuses_a_symlinked_build(tmp_path: Path) -> None:
    (tmp_path / "build").symlink_to(tmp_path)
    with pytest.raises(StateDirError, match="not a real directory"):
        existing_issue_dir(tmp_path, 7)


def test_existing_issue_dir_refuses_non_positive_issue(tmp_path: Path) -> None:
    with pytest.raises(StateDirError, match="issue number must be positive"):
        existing_issue_dir(tmp_path, 0)


# -- any kind's task directory ------------------------------------------------------


def _task_levels(root: Path, kind: str = "operational", issue: int = 7) -> list[Path]:
    state = root / "build" / ".vergil-state"
    return [state, state / kind, state / kind / str(issue)]


@pytest.mark.parametrize("kind", ["operational", "issue-implement", "upstream-report"])
def test_task_dir_created_mode_700(tmp_path: Path, kind: str) -> None:
    path = ensure_task_dir(tmp_path, kind, 7)
    levels = _task_levels(tmp_path, kind)
    assert path == levels[-1]
    assert [_mode(level) for level in levels] == [0o700] * 3
    assert list(path.iterdir()) == []


def test_task_dir_issue_implement_is_the_issue_dir(tmp_path: Path) -> None:
    assert ensure_task_dir(tmp_path, "issue-implement", 7) == ensure_issue_dir(tmp_path, 7)


def test_task_dir_existing_levels_accepted(tmp_path: Path) -> None:
    first = ensure_task_dir(tmp_path, "operational", 7)
    (first / "security-1.md").write_text("x")
    assert ensure_task_dir(tmp_path, "operational", 7) == first
    assert (first / "security-1.md").read_text() == "x"


def test_task_dir_mode_is_700_whatever_the_umask(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    old = os.umask(0o277)
    try:
        ensure_task_dir(tmp_path, "operational", 7)
    finally:
        os.umask(old)
    assert [_mode(level) for level in _task_levels(tmp_path)] == [0o700] * 3


def test_task_dir_refuses_symlinked_kind(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    elsewhere.chmod(0o700)
    state = _task_levels(tmp_path)[0]
    state.mkdir(parents=True)
    state.chmod(0o700)
    (state / "operational").symlink_to(elsewhere)
    with pytest.raises(StateDirError, match="operational is a symlink, not a directory"):
        ensure_task_dir(tmp_path, "operational", 7)
    assert list(elsewhere.iterdir()) == []


def test_task_dir_refuses_a_dangling_symlinked_issue_level(tmp_path: Path) -> None:
    ensure_task_dir(tmp_path, "operational", 8)
    _task_levels(tmp_path)[2].symlink_to(tmp_path / "missing")
    with pytest.raises(StateDirError, match="7 is a symlink, not a directory"):
        ensure_task_dir(tmp_path, "operational", 7)
    assert not (tmp_path / "missing").exists()


def test_task_dir_refuses_a_file_level(tmp_path: Path) -> None:
    ensure_task_dir(tmp_path, "operational", 8)
    _task_levels(tmp_path)[2].write_text("not a directory")
    with pytest.raises(StateDirError, match="7 is not a directory"):
        ensure_task_dir(tmp_path, "operational", 7)


@pytest.mark.parametrize("level", [0, 1, 2])
def test_task_dir_refuses_a_wrong_mode_at_any_level(tmp_path: Path, level: int) -> None:
    ensure_task_dir(tmp_path, "operational", 7)
    _task_levels(tmp_path)[level].chmod(0o750)
    with pytest.raises(StateDirError, match="has mode 750, not 700"):
        ensure_task_dir(tmp_path, "operational", 7)


def test_task_dir_refuses_public_root_creating_nothing(tmp_path: Path) -> None:
    state = _task_levels(tmp_path)[0]
    state.mkdir(parents=True)
    state.chmod(0o755)
    with pytest.raises(StateDirError, match=r"\.vergil-state has mode 755, not 700"):
        ensure_task_dir(tmp_path, "operational", 7)
    assert list(state.iterdir()) == []


@pytest.mark.parametrize("kind", ["design-review", "merge-review", "", "../operational"])
def test_task_dir_refuses_an_unknown_kind(tmp_path: Path, kind: str) -> None:
    with pytest.raises(StateDirError, match="unknown state directory kind"):
        ensure_task_dir(tmp_path, kind, 7)
    assert not (tmp_path / "build").exists()


def test_task_dir_refuses_non_positive_issue(tmp_path: Path) -> None:
    with pytest.raises(StateDirError, match="issue number must be positive"):
        ensure_task_dir(tmp_path, "operational", 0)
    assert not (tmp_path / "build").exists()


# -- design-review and hoshin directories ------------------------------------------


def _design_levels(root: Path, issue: int = 7, round_id: str = "S1") -> list[Path]:
    state = root / "build" / ".vergil-state"
    design = state / "design-review"
    return [state, design, design / str(issue), design / str(issue) / round_id]


def _hoshin_levels(root: Path, date: str = "2026-11-28", mode: str = "start") -> list[Path]:
    state = root / "build" / ".vergil-state"
    return [state, state / "hoshin", state / "hoshin" / f"{date}-{mode}"]


@pytest.mark.parametrize("round_id", ["S1", "P12", "A3"])
def test_design_review_created_mode_700(tmp_path: Path, round_id: str) -> None:
    path = ensure_design_review_dir(tmp_path, 7, round_id)
    levels = _design_levels(tmp_path, 7, round_id)
    assert path == levels[-1]
    assert [_mode(level) for level in levels] == [0o700] * 4
    assert list(path.iterdir()) == []


def test_design_review_existing_round_accepted(tmp_path: Path) -> None:
    first = ensure_design_review_dir(tmp_path, 7, "S1")
    (first / "security.md").write_text("kept")
    assert ensure_design_review_dir(tmp_path, 7, "S1") == first
    assert (first / "security.md").read_text() == "kept"


def test_design_review_rounds_share_the_issue_dir(tmp_path: Path) -> None:
    ensure_design_review_dir(tmp_path, 7, "S1")
    second = ensure_design_review_dir(tmp_path, 7, "P2")
    assert second.parent == _design_levels(tmp_path)[2]
    assert sorted(p.name for p in second.parent.iterdir()) == ["P2", "S1"]


def test_design_review_mode_is_700_whatever_the_umask(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    old = os.umask(0o277)
    try:
        ensure_design_review_dir(tmp_path, 7, "S1")
    finally:
        os.umask(old)
    assert [_mode(level) for level in _design_levels(tmp_path)] == [0o700] * 4


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_design_review_refuses_a_wrong_mode_at_any_level(tmp_path: Path, level: int) -> None:
    ensure_design_review_dir(tmp_path, 7, "S1")
    _design_levels(tmp_path)[level].chmod(0o750)
    with pytest.raises(StateDirError, match="has mode 750, not 700"):
        ensure_design_review_dir(tmp_path, 7, "S1")


def test_design_review_refuses_a_symlinked_design_review_level(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    state = _design_levels(tmp_path)[0]
    state.mkdir(parents=True, mode=0o700)
    (state / "design-review").symlink_to(elsewhere)
    with pytest.raises(StateDirError, match="design-review is a symlink, not a directory"):
        ensure_design_review_dir(tmp_path, 7, "S1")
    assert list(elsewhere.iterdir()) == []


def test_design_review_refuses_a_symlinked_round_creating_nothing(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    ensure_design_review_dir(tmp_path, 7, "P1")
    _design_levels(tmp_path)[2].joinpath("S1").symlink_to(elsewhere)
    with pytest.raises(StateDirError, match="S1 is a symlink, not a directory"):
        ensure_design_review_dir(tmp_path, 7, "S1")
    assert list(elsewhere.iterdir()) == []


def test_design_review_refuses_a_dangling_symlinked_round(tmp_path: Path) -> None:
    ensure_design_review_dir(tmp_path, 7, "P1")
    _design_levels(tmp_path)[2].joinpath("S1").symlink_to(tmp_path / "missing")
    with pytest.raises(StateDirError, match="S1 is a symlink, not a directory"):
        ensure_design_review_dir(tmp_path, 7, "S1")
    assert not (tmp_path / "missing").exists()


def test_design_review_refuses_a_file_at_the_issue_level(tmp_path: Path) -> None:
    ensure_design_review_dir(tmp_path, 8, "S1")
    _design_levels(tmp_path)[2].write_text("not a directory")
    with pytest.raises(StateDirError, match="7 is not a directory"):
        ensure_design_review_dir(tmp_path, 7, "S1")


def test_design_review_stops_before_creating_below_a_public_level(tmp_path: Path) -> None:
    levels = _design_levels(tmp_path)
    levels[0].mkdir(parents=True, mode=0o700)
    levels[1].mkdir(mode=0o755)
    with pytest.raises(StateDirError, match="design-review has mode 755, not 700"):
        ensure_design_review_dir(tmp_path, 7, "S1")
    assert list(levels[1].iterdir()) == []


@pytest.mark.parametrize("round_id", ["X1", "S", "S1x", "", "s1", "1S", "SS1", "S-1", "S1\n"])
def test_design_review_refuses_a_bad_round_creating_nothing(tmp_path: Path, round_id: str) -> None:
    with pytest.raises(StateDirError, match="round must be S, P or A then digits"):
        ensure_design_review_dir(tmp_path, 7, round_id)
    assert not (tmp_path / "build").exists()


def test_design_review_refuses_non_positive_issue(tmp_path: Path) -> None:
    with pytest.raises(StateDirError, match="issue number must be positive"):
        ensure_design_review_dir(tmp_path, 0, "S1")
    assert not (tmp_path / "build").exists()


@pytest.mark.parametrize(("date", "mode"), [("2026-11-28", "start"), ("2028-02-29", "end")])
def test_hoshin_created_mode_700(tmp_path: Path, date: str, mode: str) -> None:
    path = ensure_hoshin_dir(tmp_path, date, mode)
    levels = _hoshin_levels(tmp_path, date, mode)
    assert path == levels[-1]
    assert [_mode(level) for level in levels] == [0o700] * 3
    assert list(path.iterdir()) == []


def test_hoshin_existing_directory_accepted(tmp_path: Path) -> None:
    first = ensure_hoshin_dir(tmp_path, "2026-11-28", "end")
    (first / "screen-terms.md").write_text("kept")
    assert ensure_hoshin_dir(tmp_path, "2026-11-28", "end") == first
    assert (first / "screen-terms.md").read_text() == "kept"


def test_hoshin_mode_is_700_whatever_the_umask(tmp_path: Path) -> None:
    (tmp_path / "build").mkdir()
    old = os.umask(0o277)
    try:
        ensure_hoshin_dir(tmp_path, "2026-11-28", "start")
    finally:
        os.umask(old)
    assert [_mode(level) for level in _hoshin_levels(tmp_path)] == [0o700] * 3


@pytest.mark.parametrize("level", [0, 1, 2])
def test_hoshin_refuses_a_wrong_mode_at_any_level(tmp_path: Path, level: int) -> None:
    ensure_hoshin_dir(tmp_path, "2026-11-28", "start")
    _hoshin_levels(tmp_path)[level].chmod(0o750)
    with pytest.raises(StateDirError, match="has mode 750, not 700"):
        ensure_hoshin_dir(tmp_path, "2026-11-28", "start")


def test_hoshin_refuses_a_symlinked_date_mode_level(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    levels = _hoshin_levels(tmp_path)
    levels[0].mkdir(parents=True, mode=0o700)
    levels[1].mkdir(mode=0o700)
    levels[1].joinpath("2026-11-28-start").symlink_to(elsewhere)
    with pytest.raises(StateDirError, match="2026-11-28-start is a symlink, not a directory"):
        ensure_hoshin_dir(tmp_path, "2026-11-28", "start")
    assert list(elsewhere.iterdir()) == []


def test_hoshin_refuses_a_file_at_the_date_mode_level(tmp_path: Path) -> None:
    levels = _hoshin_levels(tmp_path)
    levels[0].mkdir(parents=True, mode=0o700)
    levels[1].mkdir(mode=0o700)
    levels[2].write_text("not a directory")
    with pytest.raises(StateDirError, match="2026-11-28-start is not a directory"):
        ensure_hoshin_dir(tmp_path, "2026-11-28", "start")


@pytest.mark.parametrize(
    "date",
    ["2026-13-40", "2026-02-29", "2026-1-01", "20261128", "2026-11-28x", "", "2026-11-28\n"],
)
def test_hoshin_refuses_a_bad_date_creating_nothing(tmp_path: Path, date: str) -> None:
    with pytest.raises(StateDirError, match="date must be"):
        ensure_hoshin_dir(tmp_path, date, "start")
    assert not (tmp_path / "build").exists()


@pytest.mark.parametrize("mode", ["middle", "Start", "", "start\n", "../start"])
def test_hoshin_refuses_a_bad_mode_creating_nothing(tmp_path: Path, mode: str) -> None:
    with pytest.raises(StateDirError, match="mode must be one of start, end"):
        ensure_hoshin_dir(tmp_path, "2026-11-28", mode)
    assert not (tmp_path / "build").exists()
