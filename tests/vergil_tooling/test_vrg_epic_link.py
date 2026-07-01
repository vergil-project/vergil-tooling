"""Tests for vergil_tooling.bin.vrg_epic_link."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_epic_link import main, parse_args
from vergil_tooling.lib import epics, github

_MOD = "vergil_tooling.bin.vrg_epic_link"


def test_parse_args_requires_epic_and_task() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "org/.github#40"])  # missing --task


def test_main_links_task_under_epic() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 0
    mock_add.assert_called_once_with(
        epics.IssueRef("org", ".github", 40),
        epics.IssueRef("org", "repo", 42),
    )


def test_main_rejects_bad_ref() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "not-a-ref", "--task", "#42"])
    assert rc == 1
    mock_add.assert_not_called()


def test_main_scopes_token_to_target_owner() -> None:
    """The link runs under the epic/task owner's App installation, not the
    cwd org's (#2070)."""
    with (
        patch(f"{_MOD}.github.current_repo", return_value="cwd-org/repo"),
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--epic", "other-org/.github#40", "--task", "other-org/repo#42"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_rejects_cross_org_refs() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org-a/.github#40", "--task", "org-b/repo#42"])
    assert rc == 1
    mock_add.assert_not_called()


def test_main_reports_missing_installation() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.add_child",
            side_effect=github.NoInstallationError("org", []),
        ),
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1
