"""Tests for vergil_tooling.bin.vrg_epic_audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_epic_audit import main

_MOD = "vergil_tooling.bin.vrg_epic_audit"


def test_main_prints_audit(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=[]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "drift audit" in out
    # The read-only banner names the auto-detected org and states nothing changed.
    assert "Read-only audit" in out
    assert "**vergil-project**" in out


def test_main_errors_when_org_undetectable(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.github.detect_org", return_value=None):
        rc = main([])
    assert rc == 1
    assert "could not determine the GitHub org" in capsys.readouterr().err


def test_window_days_controls_since() -> None:
    task_drift = MagicMock(return_value=[])
    with (
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.task_drift", task_drift),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=[]),
    ):
        rc = main(["--window-days", "7"])
    assert rc == 0
    expected_since = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    assert task_drift.call_args.args[0] == expected_since
    assert task_drift.call_args.kwargs["org"] == "vergil-project"


def test_invalid_window_days_rejected() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--window-days", "0"])
    assert exc.value.code == 2


def test_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "Read-only" in capsys.readouterr().out


def test_close_refused_for_agent(capsys: pytest.CaptureFixture[str]) -> None:
    # Refusal happens before any network work, so nothing else needs mocking.
    with patch(f"{_MOD}.identity_mode.is_human", return_value=False):
        rc = main(["--close"])
    assert rc == 1
    assert "human action" in capsys.readouterr().err


def test_close_as_human_closes_and_summarizes(capsys: pytest.CaptureFixture[str]) -> None:
    close_drift = MagicMock(return_value=["o/r#1"])
    with (
        patch(f"{_MOD}.identity_mode.is_human", return_value=True),
        patch(f"{_MOD}.github.detect_org", return_value="vergil-project"),
        patch(f"{_MOD}.epic_audit.task_drift", return_value=["T"]),
        patch(f"{_MOD}.epic_audit.epic_drift", return_value=["E"]),
        patch(f"{_MOD}.epic_audit.close_drift", close_drift),
    ):
        rc = main(["--close"])
    assert rc == 0
    close_drift.assert_called_once_with(["T"], ["E"], org="vergil-project")
    out = capsys.readouterr().out
    assert "closed" in out.lower()
    assert "o/r#1" in out
