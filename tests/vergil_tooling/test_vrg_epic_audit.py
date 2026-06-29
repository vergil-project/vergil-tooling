"""Tests for vergil_tooling.bin.vrg_epic_audit."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_epic_audit import main

if TYPE_CHECKING:
    import pytest

_MOD = "vergil_tooling.bin.vrg_epic_audit"


def test_main_prints_audit(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
    ):
        rc = main()
    assert rc == 0
    assert "drift audit" in capsys.readouterr().out
