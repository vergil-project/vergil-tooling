"""Tests for vergil_tooling.bin.vrg_roadmap."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_roadmap import main
from vergil_tooling.lib import epic_docs, github, trust

_MOD = "vergil_tooling.bin.vrg_roadmap"


def test_main_prints_roadmap(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.roadmap.gather", return_value=[]),
        patch(f"{_MOD}.roadmap.gather_completed", return_value=[]),
    ):
        rc = main(["--org", "vergil-project"])
    assert rc == 0
    assert "Roadmap" in capsys.readouterr().out


def test_main_defaults_org_to_current_repo() -> None:
    with (
        patch("vergil_tooling.bin.vrg_roadmap.github.current_org", return_value="acme") as mock_org,
        patch(f"{_MOD}.roadmap.gather", return_value=[]) as mock_gather,
        patch(f"{_MOD}.roadmap.gather_completed", return_value=[]) as mock_completed,
    ):
        rc = main([])
    assert rc == 0
    mock_org.assert_called_once()
    assert mock_gather.call_args.args[0] == "acme"
    assert mock_completed.call_args.args[0] == "acme"


def test_main_scopes_token_to_org() -> None:
    """Reading a cross-org roadmap mints for that org's installation (#2070)."""
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.roadmap.gather", return_value=[]),
        patch(f"{_MOD}.roadmap.gather_completed", return_value=[]),
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


def test_main_reports_missing_token(capsys: pytest.CaptureFixture[str]) -> None:
    err = github.MissingGitHubTokenError(4, ("gh", "repo", "view"), None, "no token")
    with patch(f"{_MOD}.github.current_org", side_effect=err):
        rc = main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "no GitHub token" in captured.err
    assert "GH_TOKEN" in captured.err


@pytest.mark.parametrize(
    "error",
    [
        trust.TrustReadError("search_window issue closed", "the search failed"),
        epic_docs.EpicDocsFormatError("epic #9: malformed Documents section"),
    ],
)
def test_main_fails_when_completed_epics_cannot_be_read(
    error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    # A Completed epic that cannot be read fails the page: no partial roadmap.
    with (
        patch(f"{_MOD}.roadmap.gather", return_value=[]),
        patch(f"{_MOD}.roadmap.gather_completed", side_effect=error),
    ):
        rc = main(["--org", "vergil-project"])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("vrg-roadmap: ")
