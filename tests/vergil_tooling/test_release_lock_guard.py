"""The unlock path of a release with a tracking issue (spec §5.3 step 2 of epic #1664)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tests.vergil_tooling.release_stubs import FakeLockOwner
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.lock_guard import rearm, release_lock
from vergil_tooling.lib.release_lock import sweeps
from vergil_tooling.lib.release_lock.holder import LockLost
from vergil_tooling.lib.release_lock.model import RefReadError
from vergil_tooling.lib.release_lock.refs import LockState

_MOD = "vergil_tooling.lib.release.lock_guard"
_URL = "https://github.com/owner/repo/pull/"


def _ctx(owner: FakeLockOwner | None) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.lock = owner  # ty: ignore[invalid-assignment]
    return ctx


def test_release_deletes_the_lock_and_drops_the_owner(capsys: pytest.CaptureFixture[str]) -> None:
    owner = FakeLockOwner()
    ctx = _ctx(owner)
    release_lock(ctx, phase="unlock", remedy="retry")
    assert owner.events == [("release",)]
    assert ctx.lock is None
    assert "Released the release lock on owner/repo." in capsys.readouterr().out


def test_a_lost_lock_is_no_longer_the_runs() -> None:
    owner = FakeLockOwner()
    owner.release_error = LockLost(
        "the lock was broken", phase="unlock", cause=None, outside_change=False
    )
    ctx = _ctx(owner)
    with pytest.raises(ReleaseError, match="the lock was broken. retry"):
        release_lock(ctx, phase="unlock", remedy="retry")
    assert ctx.lock is None


def test_no_owner_and_an_unreadable_lock_refuses_naming_it() -> None:
    held = LockState(sha="a" * 40, record=None, error="lock.json is not JSON")
    with (
        patch(_MOD + ".lock_refs.read_lock", return_value=held),
        pytest.raises(ReleaseError, match="cannot be read \\(lock.json is not JSON\\)") as info,
    ):
        release_lock(_ctx(None), phase="unlock", remedy="retry")
    assert "vrg-release --resume 2.1.0" in str(info.value)


def test_no_owner_and_a_failed_lock_read_raises() -> None:
    with (
        patch(_MOD + ".lock_refs.read_lock", side_effect=RefReadError("ls-remote failed")),
        pytest.raises(ReleaseError, match="ls-remote failed"),
    ):
        release_lock(_ctx(None), phase="unlock", remedy="retry")


def test_rearm_returns_the_sweep_result(capsys: pytest.CaptureFixture[str]) -> None:
    result = sweeps.RearmResult(armed=[f"{_URL}1"], merged=[], held=[], refused=[])
    with patch(_MOD + ".sweeps.rearm_all", return_value=result) as m_rearm:
        assert rearm(_ctx(None), phase="rearm", remedy="resume") is result
    m_rearm.assert_called_once_with("owner/repo")
    assert f"Re-armed 1 PR(s): {_URL}1" in capsys.readouterr().out


def test_rearm_that_could_not_offer_some_prs_names_them_and_the_rest() -> None:
    rest = sweeps.RearmResult(armed=[f"{_URL}1"], merged=[], held=[f"{_URL}2"], refused=[])
    failure = sweeps.RearmSweepError(rest, [(f"{_URL}3", "reading the PR failed")])
    with (
        patch(_MOD + ".sweeps.rearm_all", side_effect=failure),
        pytest.raises(ReleaseError) as info,
    ):
        rearm(_ctx(None), phase="rearm", remedy="Run vrg-release --resume 2.1.0.")
    message = str(info.value)
    assert f"{_URL}3: reading the PR failed" in message
    assert "still disarmed" in message
    assert "Run vrg-release --resume 2.1.0." in message
    assert f"Not re-armed (held): {_URL}2" in message
