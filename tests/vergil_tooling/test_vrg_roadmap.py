"""Tests for vergil_tooling.bin.vrg_roadmap."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_roadmap import main

if TYPE_CHECKING:
    import pytest


def test_main_prints_roadmap(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_roadmap.roadmap.gather", return_value=[]):
        rc = main()
    assert rc == 0
    assert "Roadmap" in capsys.readouterr().out
