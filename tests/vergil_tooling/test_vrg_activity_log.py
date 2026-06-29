"""Tests for vergil_tooling.bin.vrg_activity_log."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_activity_log import main

if TYPE_CHECKING:
    import pytest


def test_main_prints_ledger(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(
        "vergil_tooling.bin.vrg_activity_log.activity_log.gather", return_value=[]
    ) as mock_gather:
        rc = main()
    assert rc == 0
    assert "Activity log" in capsys.readouterr().out
    # the CLI computes and passes a YYYY-MM-DD cutoff
    since = mock_gather.call_args.args[0]
    assert len(since) == 10 and since.count("-") == 2
