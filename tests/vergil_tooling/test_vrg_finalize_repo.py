"""Tests for the deprecated vrg-finalize-repo alias."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_finalize_repo import main

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture(autouse=True)
def _main_worktree() -> Iterator[None]:
    with patch("vergil_tooling.bin.vrg_finalize_pr.git.is_main_worktree", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _clean_working_tree() -> Iterator[None]:
    with patch("vergil_tooling.bin.vrg_finalize_pr.git.working_tree_status", return_value=""):
        yield


def test_deprecated_alias_prints_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch("vergil_tooling.bin.vrg_finalize_pr.git.repo_root", return_value=tmp_path),
        patch("vergil_tooling.bin.vrg_finalize_pr.config.read_config") as mock_config,
        patch("vergil_tooling.bin.vrg_finalize_pr.git.current_branch", return_value="develop"),
        patch("vergil_tooling.bin.vrg_finalize_pr.git.merged_branches", return_value=[]),
        patch("vergil_tooling.bin.vrg_finalize_pr.git.run"),
        patch("vergil_tooling.bin.vrg_finalize_pr.subprocess.run") as mock_sub,
    ):
        mock_config.side_effect = FileNotFoundError
        mock_sub.return_value.returncode = 0
        result = main(["--dry-run"])
    assert result == 0
    err = capsys.readouterr().err
    assert "deprecated" in err.lower()
    assert "vrg-finalize-pr" in err
