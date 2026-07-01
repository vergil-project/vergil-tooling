"""Tests for vergil_tooling.bin.vrg_epic_unlink."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_epic_unlink import main, parse_args
from vergil_tooling.lib import epics

_MOD = "vergil_tooling.bin.vrg_epic_unlink"

PARENT = epics.IssueRef("org", "repo", 100)
TASK = epics.IssueRef("org", "repo", 42)


def test_parse_args_requires_task() -> None:
    with pytest.raises(SystemExit):
        parse_args([])  # missing --task


def test_main_unlinks_from_current_parent() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.parent_of", return_value=PARENT),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
    ):
        rc = main(["--task", "#42"])
    assert rc == 0
    mock_remove.assert_called_once_with(PARENT, TASK)


def test_main_noop_when_no_parent() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
    ):
        rc = main(["--task", "#42"])
    assert rc == 0
    mock_remove.assert_not_called()
