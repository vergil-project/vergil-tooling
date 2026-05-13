"""Tests for vergil_tooling.bin.vrg_wait_until_green."""

from __future__ import annotations

import subprocess
from unittest.mock import call, patch

import pytest

from vergil_tooling.bin.vrg_wait_until_green import main, parse_args

_MOD = "vergil_tooling.bin.vrg_wait_until_green"
_PR = "https://github.com/pr/1"


def test_parse_args() -> None:
    args = parse_args([_PR])
    assert args.pr == _PR


def test_main_happy_path_not_behind() -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
    ):
        result = main([_PR])
    assert result == 0
    mock_wait.assert_called_once_with(_PR)


def test_main_surfaces_check_failure() -> None:
    err = subprocess.CalledProcessError(returncode=1, cmd=["gh", "pr", "checks"])
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks", side_effect=err),
        pytest.raises(subprocess.CalledProcessError),
    ):
        main([_PR])


def test_main_updates_branch_when_behind() -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
        patch(
            f"{_MOD}.github.merge_state_status",
            side_effect=["BEHIND", "CLEAN"],
        ),
        patch(f"{_MOD}.github.update_branch") as mock_update,
    ):
        result = main([_PR])
    assert result == 0
    assert mock_wait.call_count == 2
    mock_update.assert_called_once_with(_PR)


def test_main_updates_branch_multiple_times() -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
        patch(
            f"{_MOD}.github.merge_state_status",
            side_effect=["BEHIND", "BEHIND", "CLEAN"],
        ),
        patch(f"{_MOD}.github.update_branch") as mock_update,
    ):
        result = main([_PR])
    assert result == 0
    assert mock_wait.call_count == 3
    assert mock_update.call_count == 2
    mock_update.assert_has_calls([call(_PR), call(_PR)])


def test_main_gives_up_after_max_updates() -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="BEHIND"),
        patch(f"{_MOD}.github.update_branch"),
    ):
        result = main([_PR])
    assert result == 1


def test_main_succeeds_for_non_behind_states() -> None:
    for status in ("CLEAN", "DIRTY", "BLOCKED", "UNSTABLE", "UNKNOWN"):
        with (
            patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
            patch(f"{_MOD}.github.wait_for_checks"),
            patch(f"{_MOD}.github.merge_state_status", return_value=status),
            patch(f"{_MOD}.github.update_branch") as mock_update,
        ):
            result = main([_PR])
        assert result == 0, f"Expected success for status {status}"
        mock_update.assert_not_called()


def test_main_fails_fast_on_merge_conflicts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="CONFLICTING"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
    ):
        result = main([_PR])
    assert result == 1
    mock_wait.assert_not_called()
    assert "merge conflicts" in capsys.readouterr().err


def test_main_detects_conflicts_after_branch_update(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(
            f"{_MOD}.github.mergeable",
            side_effect=["MERGEABLE", "CONFLICTING"],
        ),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="BEHIND"),
        patch(f"{_MOD}.github.update_branch"),
    ):
        result = main([_PR])
    assert result == 1
    assert "merge conflicts" in capsys.readouterr().err
