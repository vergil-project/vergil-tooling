"""Tests for vergil_tooling.bin.vrg_roadmap."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_roadmap import main
from vergil_tooling.lib import github

_MOD = "vergil_tooling.bin.vrg_roadmap"

if TYPE_CHECKING:
    import pytest


def test_main_prints_roadmap(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_roadmap.roadmap.gather", return_value=[]):
        rc = main(["--org", "vergil-project"])
    assert rc == 0
    assert "Roadmap" in capsys.readouterr().out


def test_main_defaults_org_to_current_repo() -> None:
    with (
        patch("vergil_tooling.bin.vrg_roadmap.github.current_org", return_value="acme") as mock_org,
        patch("vergil_tooling.bin.vrg_roadmap.roadmap.gather", return_value=[]) as mock_gather,
    ):
        rc = main([])
    assert rc == 0
    mock_org.assert_called_once()
    assert mock_gather.call_args.args[0] == "acme"


def test_main_scopes_token_to_org() -> None:
    """Reading a cross-org roadmap mints for that org's installation (#2070)."""
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.roadmap.gather", return_value=[]),
    ):
        rc = main(["--org", "other-org"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_reports_missing_installation() -> None:
    with patch(
        f"{_MOD}.roadmap.gather",
        side_effect=github.NoInstallationError("other-org", []),
    ):
        rc = main(["--org", "other-org"])
    assert rc == 1


def test_main_repo_uses_resolved_home() -> None:
    with (
        patch(f"{_MOD}.epics.resolve_epic_home", return_value="org/lab") as mock_home,
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.roadmap.gather", return_value=[]) as mock_gather,
    ):
        rc = main(["--repo", "org/lab"])
    assert rc == 0
    mock_home.assert_called_once_with("org", "lab")
    mock_scope.assert_called_once_with("org")
    assert mock_gather.call_args.kwargs["home"] == "org/lab"


def test_main_org_and_repo_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.roadmap.gather") as mock_gather:
        rc = main(["--org", "x", "--repo", "x/y"])
    assert rc == 1
    assert "mutually exclusive" in capsys.readouterr().err
    mock_gather.assert_not_called()


def test_main_malformed_repo_errors(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.roadmap.gather") as mock_gather:
        rc = main(["--repo", "noslash"])
    assert rc == 1
    assert "owner/repo" in capsys.readouterr().err
    mock_gather.assert_not_called()
