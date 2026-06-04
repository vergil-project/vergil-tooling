"""Tests for vergil_tooling.lib.await_file."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib import await_file

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.await_file"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_compute_sha256(tmp_path: Path) -> None:
    target = tmp_path / "x"
    target.write_text("hello")
    assert await_file.compute_sha256(target) == _sha("hello")


def test_atomic_write_creates_file_with_content(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "x.yml"
    await_file.atomic_write(target, "data")
    assert target.read_text() == "data"


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "x.yml"
    await_file.atomic_write(target, "data")
    assert [p.name for p in tmp_path.iterdir()] == ["x.yml"]


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "x.yml"
    target.write_text("old")
    await_file.atomic_write(target, "new")
    assert target.read_text() == "new"


def test_atomic_write_cleans_up_temp_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "x.yml"
    with (
        patch(f"{_MOD}.os.fsync", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        await_file.atomic_write(target, "data")
    # The target is never created and no temp file is left behind.
    assert list(tmp_path.iterdir()) == []


def test_wait_blocks_until_file_appears(tmp_path: Path) -> None:
    target = tmp_path / "f.yml"

    def create(_seconds: float) -> None:
        target.write_text("ready")

    with patch(f"{_MOD}.time.sleep", side_effect=create) as slept:
        result = await_file.wait_for_file(target)
    assert result == _sha("ready")
    slept.assert_called_once()


def test_wait_returns_immediately_when_file_exists(tmp_path: Path) -> None:
    target = tmp_path / "f.yml"
    target.write_text("here")
    with patch(f"{_MOD}.time.sleep") as slept:
        result = await_file.wait_for_file(target)
    assert result == _sha("here")
    slept.assert_not_called()


def test_wait_ignores_directory_at_path(tmp_path: Path) -> None:
    target = tmp_path / "f.yml"
    target.mkdir()

    def replace(_seconds: float) -> None:
        target.rmdir()
        target.write_text("real")

    with patch(f"{_MOD}.time.sleep", side_effect=replace) as slept:
        result = await_file.wait_for_file(target)
    assert result == _sha("real")
    slept.assert_called_once()


def test_wait_with_since_returns_when_content_changes(tmp_path: Path) -> None:
    target = tmp_path / "f.yml"
    target.write_text("v1")

    def change(_seconds: float) -> None:
        await_file.atomic_write(target, "v2")

    with patch(f"{_MOD}.time.sleep", side_effect=change) as slept:
        result = await_file.wait_for_file(target, since=_sha("v1"))
    assert result == _sha("v2")
    slept.assert_called_once()


def test_wait_with_since_returns_immediately_when_already_changed(tmp_path: Path) -> None:
    target = tmp_path / "f.yml"
    target.write_text("v2")
    with patch(f"{_MOD}.time.sleep") as slept:
        result = await_file.wait_for_file(target, since=_sha("v1"))
    assert result == _sha("v2")
    slept.assert_not_called()


def test_wait_with_since_does_not_wake_on_identical_rewrite(tmp_path: Path) -> None:
    # Rock-solid change detection: the checksum is authoritative. An atomic
    # rewrite with identical bytes (new inode/mtime, same content) must NOT
    # end the wait; only a genuine content change does.
    target = tmp_path / "f.yml"
    target.write_text("v1")
    since = _sha("v1")
    calls: list[int] = []

    def step(_seconds: float) -> None:
        calls.append(1)
        if len(calls) == 1:
            await_file.atomic_write(target, "v1")  # identical content
        else:
            await_file.atomic_write(target, "v2")  # real change

    with patch(f"{_MOD}.time.sleep", side_effect=step) as slept:
        result = await_file.wait_for_file(target, since=since)
    assert result == _sha("v2")
    assert slept.call_count == 2
