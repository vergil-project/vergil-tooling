"""Tests for vergil_tooling.bin.vrg_activity_log."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_activity_log import main
from vergil_tooling.lib import github

_MOD = "vergil_tooling.bin.vrg_activity_log"

if TYPE_CHECKING:
    import pytest


def test_main_prints_ledger(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "vergil_tooling.bin.vrg_activity_log.activity_log.gather", return_value=[]
    ) as mock_gather:
        rc = main(["--org", "vergil-project"])
    assert rc == 0
    assert "Activity log" in capsys.readouterr().out
    # the CLI computes and passes a YYYY-MM-DD cutoff
    since = mock_gather.call_args.args[0]
    assert len(since) == 10 and since.count("-") == 2
    assert mock_gather.call_args.kwargs["org"] == "vergil-project"


def test_main_defaults_org_to_current_repo() -> None:
    with (
        patch(
            "vergil_tooling.bin.vrg_activity_log.github.current_org", return_value="acme"
        ) as mock_org,
        patch(
            "vergil_tooling.bin.vrg_activity_log.activity_log.gather", return_value=[]
        ) as mock_gather,
    ):
        rc = main([])
    assert rc == 0
    mock_org.assert_called_once()
    assert mock_gather.call_args.kwargs["org"] == "acme"


def test_main_scopes_token_to_org() -> None:
    """Listing a cross-org activity log mints for that org's installation (#2070)."""
    with (
        patch(f"{_MOD}.github.target_org") as mock_scope,
        patch(f"{_MOD}.activity_log.gather", return_value=[]),
    ):
        rc = main(["--org", "other-org"])
    assert rc == 0
    mock_scope.assert_called_once_with("other-org")


def test_main_reports_missing_installation() -> None:
    with patch(
        f"{_MOD}.activity_log.gather",
        side_effect=github.NoInstallationError("other-org", []),
    ):
        rc = main(["--org", "other-org"])
    assert rc == 1
