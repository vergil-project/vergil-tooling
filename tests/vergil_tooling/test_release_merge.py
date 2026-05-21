from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import wait_and_merge

_MOD = "vergil_tooling.lib.release.merge"


def test_merge_succeeds_when_checks_pass() -> None:
    with (
        patch(_MOD + ".github.mergeable", return_value="MERGEABLE"),
        patch(_MOD + ".github.wait_for_checks"),
        patch(_MOD + ".github.merge_state_status", return_value="CLEAN"),
        patch(_MOD + ".github.merge") as mock_merge,
    ):
        wait_and_merge("https://github.com/o/r/pull/1", phase="merge-release")
    mock_merge.assert_called_once_with("https://github.com/o/r/pull/1", strategy="merge")


def test_merge_fails_on_conflict() -> None:
    with (
        patch(_MOD + ".github.mergeable", return_value="CONFLICTING"),
        pytest.raises(ReleaseError, match="merge conflicts"),
    ):
        wait_and_merge("https://github.com/o/r/pull/1", phase="merge-release")


def test_merge_updates_branch_when_behind() -> None:
    states = iter(["BEHIND", "CLEAN"])
    with (
        patch(_MOD + ".github.mergeable", return_value="MERGEABLE"),
        patch(_MOD + ".github.wait_for_checks"),
        patch(_MOD + ".github.merge_state_status", side_effect=states),
        patch(_MOD + ".github.update_branch") as mock_update,
        patch(_MOD + ".github.merge"),
    ):
        wait_and_merge("https://github.com/o/r/pull/1", phase="merge-release")
    mock_update.assert_called_once()


def test_merge_gives_up_after_max_updates() -> None:
    with (
        patch(_MOD + ".github.mergeable", return_value="MERGEABLE"),
        patch(_MOD + ".github.wait_for_checks"),
        patch(_MOD + ".github.merge_state_status", return_value="BEHIND"),
        patch(_MOD + ".github.update_branch"),
        pytest.raises(ReleaseError, match="still behind"),
    ):
        wait_and_merge("https://github.com/o/r/pull/1", phase="merge-release")
