"""Tests for vergil_tooling.bin.vrg_issue_create."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_issue_create import main, parse_args
from vergil_tooling.lib import epics, github

_MOD = "vergil_tooling.bin.vrg_issue_create"

EPIC = epics.IssueRef("org", "repo", 710)
_URL = "https://github.com/org/repo/issues/123"


def test_parse_args_requires_epic() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--title", "T"])  # missing --epic


def test_parse_args_requires_title() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--epic", "standing"])  # missing --title


def test_main_creates_issue_and_links_under_epic() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL) as mock_create,
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "standing", "--title", "T", "--body", "B", "--label", "bug"])
    assert rc == 0
    mock_resolve.assert_called_once_with("standing", repo="org/repo")
    assert mock_create.call_args.kwargs["title"] == "T"
    assert mock_create.call_args.kwargs["labels"] == ["bug"]
    mock_add.assert_called_once_with(EPIC, epics.IssueRef("org", "repo", 123))


def test_main_adhoc_sentinel_skips_cross_org_guard() -> None:
    # 'adhoc' (like the deprecated 'standing') resolves within the repo's org
    # (.github), so it must skip the explicit-ref cross-org guard and link.
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC) as mock_resolve,
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--epic", "adhoc", "--title", "T"])
    assert rc == 0
    mock_resolve.assert_called_once_with("adhoc", repo="org/repo")
    mock_add.assert_called_once_with(EPIC, epics.IssueRef("org", "repo", 123))


def test_main_epic_resolution_failure_creates_nothing() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.resolve_epic_ref",
            side_effect=ValueError("multiple standing epics in org/repo — pass an explicit --epic"),
        ),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "standing", "--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()


def test_main_link_failure_reports_created_issue(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child", side_effect=RuntimeError("link failed")),
    ):
        rc = main(["--epic", "standing", "--title", "T"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "123" in err  # orphan-safe: the created issue is surfaced


def test_main_scopes_token_to_repo_owner() -> None:
    """Issue creation + linking run under the --repo owner installation (#2070)."""
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child"),
    ):
        rc = main(["--repo", "other-org/repo", "--epic", "standing", "--title", "T"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_explicit_same_org_epic_proceeds() -> None:
    """An explicit epic ref in the issue's own org passes the guard and links."""
    with (
        patch(f"{_MOD}.epics.resolve_epic_ref", return_value=EPIC),
        patch(f"{_MOD}.github.create_issue", return_value=_URL),
        patch(f"{_MOD}.epics.add_child") as mock_add,
    ):
        rc = main(["--repo", "org/repo", "--epic", "org/.github#5", "--title", "T"])
    assert rc == 0
    mock_add.assert_called_once_with(EPIC, epics.IssueRef("org", "repo", 123))


def test_main_rejects_bad_epic_ref() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "not-a-ref", "--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()


def test_main_rejects_cross_org_epic() -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_ref") as mock_resolve,
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--repo", "org-a/repo", "--epic", "org-b/repo#5", "--title", "T"])
    assert rc == 1
    mock_resolve.assert_not_called()
    mock_create.assert_not_called()


def test_main_reports_missing_installation() -> None:
    with (
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.epics.resolve_epic_ref",
            side_effect=github.NoInstallationError("org", []),
        ),
        patch(f"{_MOD}.github.create_issue") as mock_create,
    ):
        rc = main(["--epic", "standing", "--title", "T"])
    assert rc == 1
    mock_create.assert_not_called()
