"""Tests for vergil_tooling.bin.vrg_epic_move."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_epic_move import main, parse_args
from vergil_tooling.lib import epics, github

_MOD = "vergil_tooling.bin.vrg_epic_move"

OLD = epics.IssueRef("org", "repo", 100)
NEW = epics.IssueRef("org", "repo", 200)
TASK = epics.IssueRef("org", "repo", 42)


def test_parse_args_requires_task_and_epic() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "standing"])  # missing --task


def test_main_reparents_unlink_then_link() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=OLD),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_remove.assert_called_once_with(OLD, TASK)
    mock_add.assert_called_once_with(NEW, TASK)


def test_main_idempotent_when_already_under_target() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=NEW),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_remove.assert_not_called()
    mock_add.assert_not_called()


def test_main_links_when_task_has_no_parent() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.remove_child") as mock_remove,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 0
    mock_remove.assert_not_called()
    mock_add.assert_called_once_with(NEW, TASK)


def test_main_rejects_non_epic_target() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", side_effect=ValueError("not an epic")),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "#999"])
    assert rc == 1
    mock_add.assert_not_called()


def test_main_accepts_standing_epic() -> None:
    """The 'standing' sentinel skips the cross-org guard (it resolves in-repo)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "#42", "--epic", "standing"])
    assert rc == 0
    mock_add.assert_called_once_with(NEW, TASK)


def test_main_scopes_token_to_task_owner() -> None:
    """The re-parent runs under the task's owner installation, not cwd (#2070)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/repo"),
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--task", "other-org/repo#42", "--epic", "other-org/repo#200"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_rejects_cross_org_task_and_epic() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--task", "org-a/repo#42", "--epic", "org-b/repo#200"])
    assert rc == 1
    mock_resolve.assert_not_called()
    mock_add.assert_not_called()


def test_main_reports_missing_installation() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=NEW),
        patch(f"{_MOD}.epics.parent_of", return_value=None),
        patch(
            f"{_MOD}.epics.add_child",
            side_effect=github.NoInstallationError("org", []),
        ),
    ):
        rc = main(["--task", "#42", "--epic", "org/repo#200"])
    assert rc == 1
