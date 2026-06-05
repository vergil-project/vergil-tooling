"""Tests for vergil_tooling.lib.release.merge (thin wrapper over pr_merge)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_merge import MergeAbortError
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import wait_and_merge

_MOD = "vergil_tooling.lib.release.merge"


def test_delegates_with_merge_strategy() -> None:
    with patch(_MOD + ".pr_merge.wait_and_merge") as engine:
        wait_and_merge("https://github.com/o/r/pull/5", phase="phase-2", verbose=True)
    engine.assert_called_once()
    args, kwargs = engine.call_args
    assert args == ("https://github.com/o/r/pull/5",)
    assert kwargs["strategy"] == "merge"
    assert callable(kwargs["wait_checks"])


def test_wraps_merge_abort_in_release_error() -> None:
    with (
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=MergeAbortError("merge conflicts")),
        pytest.raises(ReleaseError, match="merge conflicts") as excinfo,
    ):
        wait_and_merge("https://github.com/o/r/pull/5", phase="phase-3")
    assert excinfo.value.phase == "phase-3"


def test_injected_waiter_carries_verbose_flag() -> None:
    with (
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".wait_for_checks") as waiter,
    ):
        wait_and_merge("https://github.com/o/r/pull/5", phase="phase-2", verbose=True)
        engine.call_args.kwargs["wait_checks"]("https://github.com/o/r/pull/5")
    waiter.assert_called_once_with("https://github.com/o/r/pull/5", verbose=True)
