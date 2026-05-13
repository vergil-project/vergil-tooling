"""Tests for vergil_tooling.bin.vrg_merge_when_green."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from contextlib import AbstractContextManager

import pytest

from vergil_tooling.bin.vrg_merge_when_green import main, parse_args


def test_parse_args_defaults() -> None:
    args = parse_args(["https://github.com/pr/1"])
    assert args.pr == "https://github.com/pr/1"
    assert args.strategy == "merge"


def test_parse_args_strategy() -> None:
    args = parse_args(["42", "--strategy", "squash"])
    assert args.strategy == "squash"


def test_parse_args_rejects_unknown_strategy() -> None:
    with pytest.raises(SystemExit):
        parse_args(["42", "--strategy", "ff-only"])


_MOD = "vergil_tooling.bin.vrg_merge_when_green"


def _mock_branch(branch: str = "release/1.0.0") -> AbstractContextManager[object]:
    return patch(f"{_MOD}.github.read_output", return_value=branch)


def test_main_happy_path() -> None:
    with (
        _mock_branch("release/1.0.0"),
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["https://github.com/pr/1"])
    assert result == 0
    mock_wait.assert_called_once_with("https://github.com/pr/1")
    mock_merge.assert_called_once_with("https://github.com/pr/1", strategy="merge")


def test_main_custom_strategy() -> None:
    with (
        _mock_branch("release/1.0.0"),
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["42", "--strategy", "squash"])
    assert result == 0
    mock_merge.assert_called_once_with("42", strategy="squash")


def test_main_surfaces_check_failure() -> None:
    err = subprocess.CalledProcessError(returncode=1, cmd=["gh", "pr", "checks"])
    with (
        _mock_branch("release/1.0.0"),
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(
            f"{_MOD}.github.wait_for_checks",
            side_effect=err,
        ),
        patch(f"{_MOD}.github.merge") as mock_merge,
        pytest.raises(subprocess.CalledProcessError),
    ):
        main(["https://github.com/pr/1"])
    mock_merge.assert_not_called()


def test_release_branch_allowed() -> None:
    with (
        _mock_branch("release/1.4.9"),
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["https://github.com/pr/1"])
    assert result == 0
    mock_merge.assert_called_once()


def test_bump_branch_allowed() -> None:
    with (
        _mock_branch("release/bump-version-1.4.10"),
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["https://github.com/pr/1"])
    assert result == 0
    mock_merge.assert_called_once()


def test_legacy_chore_bump_branch_allowed() -> None:
    with (
        _mock_branch("chore/bump-version-1.4.10"),
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["https://github.com/pr/1"])
    assert result == 0
    mock_merge.assert_called_once()


def test_feature_branch_blocked(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        _mock_branch("feature/42-foo"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["https://github.com/pr/1"])
    assert result == 1
    mock_wait.assert_not_called()
    mock_merge.assert_not_called()
    assert "only for release-workflow PRs" in capsys.readouterr().err


def test_main_updates_branch_when_behind() -> None:
    pr = "https://github.com/pr/1"
    with (
        _mock_branch("release/1.0.0"),
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
        patch(
            f"{_MOD}.github.merge_state_status",
            side_effect=["BEHIND", "CLEAN"],
        ),
        patch(f"{_MOD}.github.update_branch") as mock_update,
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main([pr])
    assert result == 0
    assert mock_wait.call_count == 2
    mock_update.assert_called_once_with(pr)
    mock_merge.assert_called_once()


def test_main_gives_up_after_max_updates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        _mock_branch("release/1.0.0"),
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="BEHIND"),
        patch(f"{_MOD}.github.update_branch"),
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["https://github.com/pr/1"])
    assert result == 1
    mock_merge.assert_not_called()
    assert "giving up" in capsys.readouterr().err


def test_main_fails_fast_on_merge_conflicts(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        _mock_branch("release/1.0.0"),
        patch(f"{_MOD}.github.mergeable", return_value="CONFLICTING"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
        patch(f"{_MOD}.github.merge") as mock_merge,
    ):
        result = main(["https://github.com/pr/1"])
    assert result == 1
    mock_wait.assert_not_called()
    mock_merge.assert_not_called()
    assert "merge conflicts" in capsys.readouterr().err
