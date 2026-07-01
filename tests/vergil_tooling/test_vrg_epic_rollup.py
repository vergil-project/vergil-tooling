"""Tests for vergil_tooling.bin.vrg_epic_rollup."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_epic_rollup import main, parse_args
from vergil_tooling.lib import epics, github

_MOD = "vergil_tooling.bin.vrg_epic_rollup"


def test_parse_args_requires_task() -> None:
    with pytest.raises(SystemExit):
        parse_args([])  # missing --task


def test_main_rolls_up_parent_epic_for_closed_task() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.rollup") as mock_rollup,
    ):
        rc = main(["--task", "#42"])
    assert rc == 0
    mock_rollup.assert_called_once_with(epics.IssueRef("org", "repo", 42))


def test_main_accepts_fully_qualified_ref() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.rollup") as mock_rollup,
    ):
        rc = main(["--task", "org/other#7"])
    assert rc == 0
    mock_rollup.assert_called_once_with(epics.IssueRef("org", "other", 7))


def test_main_rejects_bad_ref() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.rollup") as mock_rollup,
    ):
        rc = main(["--task", "not-a-ref"])
    assert rc == 1
    mock_rollup.assert_not_called()


def test_main_scopes_token_to_task_owner() -> None:
    """The rollup queries + epic close run under the task's owner (#2070)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/repo"),
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.rollup"),
    ):
        rc = main(["--task", "other-org/other#7"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_reports_missing_installation() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.rollup",
            side_effect=github.NoInstallationError("org", []),
        ),
    ):
        rc = main(["--task", "#42"])
    assert rc == 1
