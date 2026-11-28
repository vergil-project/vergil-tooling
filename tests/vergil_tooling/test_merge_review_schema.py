"""Tests for the merge-review schema's shared patch-ending helper."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.merge_review.schema import patch_checker

if TYPE_CHECKING:
    from pathlib import Path


class _Unreadable(Exception):
    """A stand-in for the caller's error class, so the test sees what is raised."""


def test_patch_checker_reads_the_step_patches(tmp_path: Path) -> None:
    (tmp_path / "diff-check.patch").write_bytes(b"+a\n")
    (tmp_path / "fix-check.patch").write_bytes(b"+b")
    (tmp_path / "fix-review.patch").write_bytes(b"")
    ends = patch_checker(tmp_path, "check", _Unreadable)
    assert ends("diff") is True
    assert ends("fix") is False
    assert patch_checker(tmp_path, "review", _Unreadable)("fix") is False

    missing = tmp_path / "diff-check-2.patch"
    with pytest.raises(_Unreadable) as exc:
        patch_checker(tmp_path, "check-2", _Unreadable)("diff")
    assert str(exc.value).startswith(f"cannot read the diff patch file {missing}: ")
    assert isinstance(exc.value.__cause__, FileNotFoundError)
