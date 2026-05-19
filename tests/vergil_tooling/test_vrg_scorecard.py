"""Tests for vergil_tooling.bin.vrg_scorecard."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.bin.vrg_scorecard import main

if TYPE_CHECKING:
    import pytest


def test_help_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "usage: vrg-scorecard" in out
    assert "scorecard" in out.lower()


def test_h_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["-h"]) == 0
    assert "usage: vrg-scorecard" in capsys.readouterr().out
