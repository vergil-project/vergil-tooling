"""The lock owner (epic vergil-project/.github#1664, Task 21).

Exercised against a local bare repository served as ``origin``; see
:mod:`tests.vergil_tooling.release_lock_stubs`. The heartbeat interval is
shortened so the background writer ticks within a test.
"""

from __future__ import annotations

import dataclasses
import itertools
import threading
import time
from typing import TYPE_CHECKING, Any, cast

import pytest

from tests.vergil_tooling.release_lock_stubs import REPO, Origin, make_origin
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release_lock import RefReadError, RefWriteError, history, holder, refs
from vergil_tooling.lib.release_lock.holder import LockLost, LockOwner
from vergil_tooling.lib.release_lock.model import LockRecord, from_json, to_json

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path

#: The heartbeat interval the tests run with, in seconds.
_TICK = 0.05


@pytest.fixture
def origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Origin:
    return make_origin(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """A short heartbeat interval and a clock that advances one second per read."""
    monkeypatch.setattr(holder, "HEARTBEAT_INTERVAL_SECS", _TICK)
    seconds = itertools.count()
    lock = threading.Lock()

    def now() -> str:
        with lock:
            n = next(seconds)
        return f"2026-11-27T{10 + n // 3600:02d}:{n // 60 % 60:02d}:{n % 60:02d}Z"

    monkeypatch.setattr(holder, "utc_now", now)


@pytest.fixture
def owners() -> Generator[list[LockOwner]]:
    """Owners a test made; every heartbeat is stopped when the test ends."""
    made: list[LockOwner] = []
    yield made
    for owner in made:
        owner.stop_heartbeat()


def _record(token: str = "caller-token", **fields: str) -> LockRecord:  # noqa: S107
    base = LockRecord(
        holder_identity="human",
        holder_host="mac.local",
        version="2.1.400",
        worktree="",
        tracking_issue="",
        stage="pre-check",
        health="running",
        broken_stage="",
        started_at="2026-11-27T10:00:00Z",
        heartbeat_at="2026-11-27T10:00:00Z",
        token=token,
    )
    return dataclasses.replace(base, **cast("dict[str, Any]", fields))


def _on_origin(origin: Origin) -> LockRecord:
    return from_json(origin.file(refs.LOCK_REF, refs.LOCK_FILE))


def _wait_for(check: Callable[[], bool], *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            msg = "the condition did not hold in time"
            raise AssertionError(msg)
        time.sleep(_TICK / 2)


def _take(owners: list[LockOwner], rec: LockRecord | None = None) -> LockOwner:
    owner = LockOwner.take(REPO, rec or _record())
    assert owner is not None
    owners.append(owner)
    return owner


def _foreign(origin: Origin, **fields: str) -> str:
    """Rewrite the lock on origin, as another writer would; the new sha."""
    sha = origin.commit(refs.LOCK_FILE, to_json(_record(**fields)))
    origin.set_ref(refs.LOCK_REF, sha)
    return sha


# --- taking, adopting and taking over ---------------------------------------------


def test_take_adopt_takeover_start_heartbeat(origin: Origin, owners: list[LockOwner]) -> None:
    taken = _take(owners)
    assert taken.heartbeat_running
    first = taken.sha
    _wait_for(lambda: taken.sha != first)
    taken.stop_heartbeat()
    assert not taken.heartbeat_running

    adopted = LockOwner.adopt(REPO, refs.read_lock(REPO))
    owners.append(adopted)
    assert adopted.heartbeat_running
    first = adopted.sha
    _wait_for(lambda: adopted.sha != first)
    adopted.stop_heartbeat()

    taken_over = LockOwner.takeover(REPO, refs.read_lock(REPO), _record(holder_host="linux"))
    owners.append(taken_over)
    assert taken_over.heartbeat_running
    first = taken_over.sha
    _wait_for(lambda: taken_over.sha != first)
    assert _on_origin(origin).holder_host == "linux"


def test_take_held_lock_returns_none(origin: Origin, owners: list[LockOwner]) -> None:
    _take(owners)
    assert LockOwner.take(REPO, _record(holder_identity="other")) is None


def test_take_writes_its_own_token(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    assert owner.token != "caller-token"
    assert _on_origin(origin).token == owner.token


def test_heartbeat_refreshes_during_blocking_wait(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    seen = {_on_origin(origin).heartbeat_at}
    deadline = time.monotonic() + 10
    while len(seen) < 4 and time.monotonic() < deadline:
        time.sleep(_TICK)  # the caller is blocked; only the background writer runs
        seen.add(_on_origin(origin).heartbeat_at)
    assert len(seen) >= 4
    assert _on_origin(origin).stage == "pre-check"
    assert owner.heartbeat_running


def test_writes_serialised_no_lost_update(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    errors: list[BaseException] = []

    def write(field: str, value: str) -> None:
        try:
            owner.update(**{field: value})
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)

    workers = [
        threading.Thread(target=write, args=("stage", "cut")),
        threading.Thread(target=write, args=("worktree", "/w/release")),
        threading.Thread(target=write, args=("tracking_issue", "org/repo#7")),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    owner.stop_heartbeat()
    rec = _on_origin(origin)
    assert (rec.stage, rec.worktree, rec.tracking_issue) == ("cut", "/w/release", "org/repo#7")
    assert origin.sha(refs.LOCK_REF) == owner.sha


def test_update_rejects_other_fields(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    with pytest.raises(ValueError, match="token"):
        owner.update(token="mine")
    with pytest.raises(ValueError, match="broken_stage"):
        owner.update(health="broken")  # a record with no broken stage is invalid


# --- own writes and lost locks ----------------------------------------------------


def test_own_write_cas_failure_retried(
    origin: Origin, owners: list[LockOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()
    real = refs.write_ref
    calls = {"n": 0}

    def lands_then_fails(where: Path, ref: str, **kwargs: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            assert real(where, ref, **kwargs)
            msg = "pushing refs/vergil/release-lock failed: the connection dropped"
            raise RefWriteError(msg)
        return real(where, ref, **kwargs)

    monkeypatch.setattr(refs, "write_ref", lands_then_fails)
    with pytest.raises(RefWriteError, match="connection dropped"):
        owner.update(stage="cut")
    landed = origin.sha(refs.LOCK_REF)
    assert landed != owner.sha  # the owner still expects its earlier write

    owner.update(stage="publish")  # a mismatch against its own write: retried
    assert _on_origin(origin).stage == "publish"
    assert origin.sha(refs.LOCK_REF) == owner.sha


def test_same_fields_other_writer_raises_lock_lost(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()
    ours = _on_origin(origin)
    # Every field the same, the token included: only the sha tells it is not ours.
    sha = origin.commit(refs.LOCK_FILE, to_json(ours), author="Another Run")
    assert sha != owner.sha
    origin.set_ref(refs.LOCK_REF, sha)
    with pytest.raises(LockLost):
        owner.update(stage="cut")


def test_adopt_writes_new_token(origin: Origin, owners: list[LockOwner]) -> None:
    first = _take(owners)
    first.stop_heartbeat()
    adopted = LockOwner.adopt(REPO, refs.read_lock(REPO))
    owners.append(adopted)
    assert adopted.token != first.token
    rec = _on_origin(origin)
    assert rec.token == adopted.token
    assert (rec.holder_identity, rec.version, rec.started_at) == (
        "human",
        "2.1.400",
        "2026-11-27T10:00:00Z",
    )
    with pytest.raises(LockLost):  # the earlier owner sees a foreign record and stops
        first.update(stage="cut")


def test_adopt_refuses_an_unreadable_or_absent_lock(origin: Origin) -> None:
    with pytest.raises(ValueError, match="absent"):
        LockOwner.adopt(REPO, refs.read_lock(REPO))
    origin.set_ref(refs.LOCK_REF, origin.commit(refs.LOCK_FILE, "{not json"))
    with pytest.raises(ValueError, match="unreadable"):
        LockOwner.adopt(REPO, refs.read_lock(REPO))


def test_adopt_of_a_lock_changed_since_read_raises_lock_lost(
    origin: Origin, owners: list[LockOwner]
) -> None:
    first = _take(owners)
    first.stop_heartbeat()
    state = refs.read_lock(REPO)
    _foreign(origin, stage="elsewhere")
    with pytest.raises(LockLost):
        LockOwner.adopt(REPO, state)
    assert _on_origin(origin).stage == "elsewhere"


def test_takeover_refuses_an_absent_lock(origin: Origin) -> None:
    with pytest.raises(ValueError, match="absent"):
        LockOwner.takeover(REPO, refs.read_lock(REPO), _record())


def test_takeover_of_an_unreadable_lock(origin: Origin, owners: list[LockOwner]) -> None:
    origin.set_ref(refs.LOCK_REF, origin.commit(refs.LOCK_FILE, "{not json"))
    owner = LockOwner.takeover(REPO, refs.read_lock(REPO), _record(holder_host="linux"))
    owners.append(owner)
    assert owner.record.holder_host == "linux"
    assert _on_origin(origin).token == owner.token


def test_start_heartbeat_twice_is_refused(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    with pytest.raises(RuntimeError, match="already running"):
        owner.start_heartbeat()


def test_heartbeat_stopped_while_it_waited_writes_nothing(
    origin: Origin, owners: list[LockOwner]
) -> None:
    owner = _take(owners)
    sha = owner.sha
    with owner._write_lock:  # noqa: SLF001 - hold the writer so the heartbeat waits on it
        time.sleep(_TICK * 6)
        owner._stop.set()  # noqa: SLF001
    _wait_for(lambda: not owner.heartbeat_running)
    assert origin.sha(refs.LOCK_REF) == sha


def test_writes_bounded_against_own_writes(
    origin: Origin, owners: list[LockOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()
    monkeypatch.setattr(refs, "write_ref", lambda *_a, **_k: False)
    monkeypatch.setattr(refs, "remote_sha", lambda *_a: owner._sha)  # noqa: SLF001
    with pytest.raises(RefWriteError, match="own earlier writes"):
        owner.update(stage="cut")


def test_failed_reread_after_rejection_raises(
    origin: Origin, owners: list[LockOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()

    def unreadable(*_args: object) -> str | None:
        msg = "ls-remote broke"
        raise RefReadError(msg)

    monkeypatch.setattr(refs, "write_ref", lambda *_a, **_k: False)
    monkeypatch.setattr(refs, "remote_sha", unreadable)
    with pytest.raises(RefWriteError, match="re-reading .* ls-remote broke"):
        owner.update(stage="cut")


def test_broken_lock_raises_lock_lost_on_next_update(
    origin: Origin, owners: list[LockOwner]
) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()
    origin.git("update-ref", "-d", refs.LOCK_REF)
    with pytest.raises(LockLost) as caught:
        owner.update(stage="cut")
    assert isinstance(caught.value, ReleaseError)
    assert origin.sha(refs.LOCK_REF) is None  # never re-taken
    with pytest.raises(LockLost):  # and every later write stops the same way
        owner.update(stage="publish")


def test_heartbeat_stops_on_loss_and_never_retakes(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    origin.git("update-ref", "-d", refs.LOCK_REF)
    _wait_for(lambda: not owner.heartbeat_running)
    time.sleep(_TICK * 4)
    assert origin.sha(refs.LOCK_REF) is None
    with pytest.raises(LockLost):
        owner.update(stage="cut")


def test_heartbeat_keeps_running_through_a_failed_write(
    origin: Origin,
    owners: list[LockOwner],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real = refs.write_ref
    failures = {"left": 2}

    def flaky(where: Path, ref: str, **kwargs: Any) -> bool:
        if threading.current_thread() is not threading.main_thread() and failures["left"]:
            failures["left"] -= 1
            msg = "pushing refs/vergil/release-lock failed: 502"
            raise RefWriteError(msg)
        return real(where, ref, **kwargs)

    owner = _take(owners)
    first = owner.sha
    monkeypatch.setattr(refs, "write_ref", flaky)
    _wait_for(lambda: owner.sha != first)
    assert owner.heartbeat_running
    assert "heartbeat" in capsys.readouterr().err


def test_heartbeat_crash_is_raised_on_next_update(
    origin: Origin, owners: list[LockOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _take(owners)
    real = refs.write_ref
    crash = {"on": True}

    def broken(where: Path, ref: str, **kwargs: Any) -> bool:
        if crash["on"]:
            msg = "a bug in the writer"
            raise KeyError(msg)
        return real(where, ref, **kwargs)

    monkeypatch.setattr(refs, "write_ref", broken)
    _wait_for(lambda: not owner.heartbeat_running)
    crash["on"] = False
    with pytest.raises(RuntimeError, match="heartbeat .* stopped"):
        owner.update(stage="cut")


# --- releasing --------------------------------------------------------------------


def test_release_stops_heartbeat_before_delete(
    origin: Origin, owners: list[LockOwner], monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _take(owners)
    order: list[str] = []
    real_stop = owner.stop_heartbeat
    real_write = refs.write_ref

    def stop() -> None:
        real_stop()
        order.append("stop")

    def write(where: Path, ref: str, *, new: str | None, expected: str | None) -> bool:
        if new is None:
            assert not owner.heartbeat_running
            order.append("delete")
        return real_write(where, ref, new=new, expected=expected)

    monkeypatch.setattr(owner, "stop_heartbeat", stop)
    monkeypatch.setattr(refs, "write_ref", write)
    owner.release()
    assert order == ["stop", "delete"]
    assert origin.sha(refs.LOCK_REF) is None


def test_release_of_a_lost_lock_raises_lock_lost(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()
    other = _foreign(origin, stage="elsewhere")
    with pytest.raises(LockLost):
        owner.release()
    assert origin.sha(refs.LOCK_REF) == other


# --- naming the cause -------------------------------------------------------------


def _append(action: str, old: LockRecord, *, actor: str = "user-agent") -> None:
    history.append(
        REPO,
        history.HistoryEntry(
            action=cast("history.Action", action),
            old=to_json(old),
            actor=actor,
            at="2026-11-27T12:00:00Z",
            reason="the heartbeat went stale",
            rearmed=(),
        ),
    )


@pytest.mark.parametrize(
    ("action", "verb"),
    [("break", "broken"), ("reclaim", "reclaimed"), ("takeover", "taken over")],
)
def test_loss_with_history_entry_names_cause(
    origin: Origin, owners: list[LockOwner], action: str, verb: str
) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()
    ours = _on_origin(origin)
    _append("reclaim", _record(token="some-other-release"), actor="someone-else")
    _append(action, ours)
    _append("reclaim-rearm", ours, actor="not-the-cause")  # a record only: never the cause
    history.append(  # an old lock that does not parse names no token
        REPO,
        history.HistoryEntry(
            action="break",
            old="{not json",
            actor="x",
            at="2026-11-27T13:00:00Z",
            reason="",
            rearmed=(),
        ),
    )
    origin.git("update-ref", "-d", refs.LOCK_REF)
    with pytest.raises(LockLost) as caught:
        owner.update(stage="cut")
    lost = caught.value
    assert lost.cause is not None
    assert (lost.cause.action, lost.cause.actor) == (action, "user-agent")
    assert not lost.outside_change
    assert verb in str(lost)
    assert "user-agent" in str(lost)
    assert "the heartbeat went stale" in str(lost)
    # A superseded release has nothing to resume; a broken or reclaimed one does.
    assert ("--resume" in str(lost)) == (action != "takeover")
    assert ("superseded" in str(lost)) == (action == "takeover")


def test_loss_without_history_entry_reported_as_outside_change(
    origin: Origin, owners: list[LockOwner]
) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()
    _append("reclaim", _record(token="some-other-release"))  # not this owner's lock
    _foreign(origin, stage="elsewhere")
    with pytest.raises(LockLost) as caught:
        owner.update(stage="cut")
    lost = caught.value
    assert lost.cause is None
    assert lost.outside_change
    assert "outside the tooling" in str(lost)


def test_loss_with_unreadable_history_says_so(origin: Origin, owners: list[LockOwner]) -> None:
    owner = _take(owners)
    owner.stop_heartbeat()
    origin.set_ref(refs.HISTORY_REF, origin.commit(history.HISTORY_FILE, "{not json"))
    origin.git("update-ref", "-d", refs.LOCK_REF)
    with pytest.raises(LockLost) as caught:
        owner.update(stage="cut")
    lost = caught.value
    assert lost.cause is None
    assert not lost.outside_change
    assert "could not be read" in str(lost)
