"""Tests for vergil_tooling.lib.release.merge (thin wrapper over pr_merge)."""

from __future__ import annotations

import itertools
import subprocess
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_merge import MergeAbortError
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import known_pr_state, wait_and_merge
from vergil_tooling.lib.retry import KnownResourceNotFoundError

_MOD = "vergil_tooling.lib.release.merge"
_PR = "https://github.com/o/r/pull/5"
_RETRY_SLEEP = "vergil_tooling.lib.retry.time.sleep"
_RETRY_CLOCK = "vergil_tooling.lib.retry.time.monotonic"


def _not_found() -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(
        1, ["gh"], stderr="GraphQL: Could not resolve to a PullRequest with the number of 5."
    )


def test_delegates_with_merge_strategy() -> None:
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
    ):
        wait_and_merge(_PR, phase="phase-2")
    engine.assert_called_once()
    args, kwargs = engine.call_args
    assert args == (_PR,)
    assert kwargs["strategy"] == "merge"
    assert callable(kwargs["wait_checks"])


def test_wraps_merge_abort_in_release_error() -> None:
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=MergeAbortError("merge conflicts")),
        pytest.raises(ReleaseError, match="merge conflicts") as excinfo,
    ):
        wait_and_merge(_PR, phase="phase-3")
    assert excinfo.value.phase == "phase-3"


def test_injected_waiter_is_wait_for_checks() -> None:
    with (
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_MOD + ".wait_for_checks") as waiter,
    ):
        wait_and_merge(_PR, phase="phase-2")
        engine.call_args.kwargs["wait_checks"](_PR)
    waiter.assert_called_once_with(_PR)


# --- #3137: a PR URL GitHub just returned may 404 briefly ---


def test_known_pr_state_retries_404() -> None:
    with (
        patch(_MOD + ".github.pr_state", side_effect=[_not_found(), "OPEN"]) as state,
        patch(_RETRY_SLEEP),
    ):
        assert known_pr_state(_PR) == "OPEN"
    assert state.call_count == 2


def test_known_pr_state_non_404_is_not_retried() -> None:
    err = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403: Forbidden")
    with (
        patch(_MOD + ".github.pr_state", side_effect=err) as state,
        patch(_RETRY_SLEEP) as sleep,
        pytest.raises(subprocess.CalledProcessError, match="returned non-zero"),
    ):
        known_pr_state(_PR)
    state.assert_called_once()
    sleep.assert_not_called()


def test_wait_and_merge_waits_for_new_pr_to_be_readable() -> None:
    """A just-created PR 404s briefly; the merge engine starts once it is readable."""
    calls: list[str] = []

    def pr_state(pr: str) -> str:
        calls.append("state")
        if len(calls) == 1:
            raise _not_found()
        return "OPEN"

    def engine(*_args: object, **_kwargs: object) -> None:
        calls.append("engine")

    with (
        patch(_MOD + ".github.pr_state", side_effect=pr_state),
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=engine),
        patch(_RETRY_SLEEP),
    ):
        wait_and_merge(_PR, phase="back-merge-bump")
    assert calls == ["state", "state", "engine"]


def test_wait_and_merge_persistent_404_never_reaches_engine() -> None:
    with (
        patch(_MOD + ".github.pr_state", side_effect=_not_found()),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
        patch(_RETRY_SLEEP),
        patch(_RETRY_CLOCK, side_effect=itertools.count(0.0, 10.0)),
        pytest.raises(KnownResourceNotFoundError, match=f"PR {_PR} was reported by GitHub"),
    ):
        wait_and_merge(_PR, phase="back-merge-bump")
    engine.assert_not_called()
