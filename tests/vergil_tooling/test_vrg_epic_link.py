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
        patch(f"{_MOD}.github.is_public", return_value=True),
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
        patch(f"{_MOD}.github.is_public", return_value=True),
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
        patch(f"{_MOD}.github.is_public", return_value=True),
        patch(
            f"{_MOD}.epics.add_child",
            side_effect=github.NoInstallationError("org", []),
        ),
    ):
        rc = main(["--epic", "org/.github#40", "--task", "#42"])
    assert rc == 1


def test_refuses_public_task_under_private_epic(capsys: pytest.CaptureFixture[str]) -> None:
    # A public task must not be a native sub-issue of a private epic: it would
    # leak the private repo's name and break cross-boundary roll-up.
    def pub(nwo: str) -> bool:
        return {"org/tooling": True, "org/lab": False}[nwo]

    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/tooling"),
        patch(f"{_MOD}.github.is_public", side_effect=pub),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/lab#5", "--task", "org/tooling#9"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing to link public task" in err
    assert "Blocked-by" in err
    mock_add.assert_not_called()


def test_allows_private_task_under_public_epic() -> None:
    # The reverse is fine: a private child under a public parent doesn't leak and
    # roll-up reads the public parent.
    def pub(nwo: str) -> bool:
        return {"org/.github": True, "org/lab": False}[nwo]

    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/lab"),
        patch(f"{_MOD}.github.is_public", side_effect=pub),
        patch(f"{_MOD}.github.target_org"),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "org/.github#5", "--task", "org/lab#9"])
    assert rc == 0
    mock_add.assert_called_once()
