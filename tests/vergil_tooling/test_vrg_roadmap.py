"""Tests for vergil_tooling.bin.vrg_roadmap."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_roadmap import main

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
