"""Tests for lib/checkout_lock.py (epic vergil-project/.github#1218, spec §6.4)."""

from __future__ import annotations

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.checkout_lock import LOCK_NAME, CheckoutLockError, checkout_lock
from vergil_tooling.lib.merge_review.state import StateDirError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# A child that takes the lock, says so on stdout, then holds it until its
# stdin closes (or it is killed).
_HOLDER = """
import sys
from pathlib import Path
from vergil_tooling.lib.checkout_lock import checkout_lock
with checkout_lock(Path(sys.argv[1]), purpose=sys.argv[2]):
    print("held", flush=True)
    sys.stdin.read()
"""

# Long enough for a blocked acquire to show it is blocked; short enough to
# keep the suite fast.
_SETTLE_SECS = 0.3


@pytest.fixture
def holder(tmp_path: Path) -> Iterator[subprocess.Popen[str]]:
    """A child process holding the checkout lock of *tmp_path*."""
    proc = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", _HOLDER, str(tmp_path), "test holder"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline() == "held\n"
    yield proc
    if proc.poll() is None:
        proc.kill()
    proc.wait()


def _acquire_in_thread(root: Path) -> tuple[threading.Thread, threading.Event]:
    acquired = threading.Event()

    def take() -> None:
        with checkout_lock(root, purpose="waiter"):
            acquired.set()

    thread = threading.Thread(target=take, daemon=True)
    thread.start()
    return thread, acquired


def test_lock_serializes_two_processes(tmp_path: Path, holder: subprocess.Popen[str]) -> None:
    """The second acquirer blocks until the first process lets go."""
    thread, acquired = _acquire_in_thread(tmp_path)
    assert not acquired.wait(_SETTLE_SECS), "acquired while another process held the lock"
    assert holder.stdin is not None
    holder.stdin.close()
    holder.wait(timeout=10)
    assert acquired.wait(10)
    thread.join(10)


def test_lock_released_when_holder_dies(tmp_path: Path, holder: subprocess.Popen[str]) -> None:
    """A killed holder never wedges the next run: the OS frees the lock."""
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=10)
    start = time.monotonic()
    with checkout_lock(tmp_path, purpose="after the crash"):
        lock_text = (tmp_path / "build" / ".vergil-state" / LOCK_NAME).read_text()
    assert time.monotonic() - start < _SETTLE_SECS
    assert lock_text == f"{os.getpid()} after the crash\n"


def test_wait_message_names_holder(
    tmp_path: Path, holder: subprocess.Popen[str], capsys: pytest.CaptureFixture[str]
) -> None:
    thread, acquired = _acquire_in_thread(tmp_path)
    assert not acquired.wait(_SETTLE_SECS)
    out = capsys.readouterr().out
    assert out == f"Waiting for the checkout lock (held by pid {holder.pid}: test holder)…\n"
    # The waiter never overwrote the holder's record.
    lock_file = tmp_path / "build" / ".vergil-state" / LOCK_NAME
    assert lock_file.read_text() == f"{holder.pid} test holder\n"
    assert holder.stdin is not None
    holder.stdin.close()
    assert acquired.wait(10)
    thread.join(10)
    # Said once, and the record is cleared once the waiter lets go.
    assert "Waiting" not in capsys.readouterr().out
    assert lock_file.read_text() == ""


@pytest.mark.parametrize("record", ["", "garbage", "12", "x12 purpose"])
def test_wait_message_without_record(
    tmp_path: Path, record: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """A holder whose record is absent or unreadable is reported as unrecorded."""
    state = tmp_path / "build" / ".vergil-state"
    state.mkdir(parents=True)
    state.chmod(0o700)
    lock_file = state / LOCK_NAME
    lock_file.write_text(record)
    with lock_file.open("r+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        thread, acquired = _acquire_in_thread(tmp_path)
        assert not acquired.wait(_SETTLE_SECS)
        assert capsys.readouterr().out == "Waiting for the checkout lock (holder not recorded)…\n"
    assert acquired.wait(10)
    thread.join(10)


def test_nested_acquire_raises(tmp_path: Path) -> None:
    with (
        checkout_lock(tmp_path, purpose="outer"),
        pytest.raises(CheckoutLockError, match="checkout lock already held by this process"),
        checkout_lock(tmp_path, purpose="inner"),
    ):
        pass
    # The failed nested acquire left the outer lock's bookkeeping intact.
    with checkout_lock(tmp_path, purpose="again"):
        pass


def test_lock_released_on_exception(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), checkout_lock(tmp_path, purpose="raises"):
        raise RuntimeError
    with checkout_lock(tmp_path, purpose="next"):
        pass


def test_state_dir_symlink_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / ".vergil-state").symlink_to(elsewhere)
    with pytest.raises(StateDirError, match="symlink"), checkout_lock(tmp_path, purpose="p"):
        pass
    assert list(elsewhere.iterdir()) == []


def test_state_dir_created_private(tmp_path: Path) -> None:
    with checkout_lock(tmp_path, purpose="p"):
        pass
    state = tmp_path / "build" / ".vergil-state"
    assert state.stat().st_mode & 0o777 == 0o700


def test_state_dir_wrong_mode_refused(tmp_path: Path) -> None:
    (tmp_path / "build" / ".vergil-state").mkdir(parents=True)
    (tmp_path / "build" / ".vergil-state").chmod(0o755)
    with pytest.raises(StateDirError, match="mode 755"), checkout_lock(tmp_path, purpose="p"):
        pass


def test_lock_file_symlink_refused(tmp_path: Path) -> None:
    state = tmp_path / "build" / ".vergil-state"
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    target = tmp_path / "target"
    target.write_text("keep\n")
    (state / LOCK_NAME).symlink_to(target)
    with pytest.raises(CheckoutLockError, match="symlink"), checkout_lock(tmp_path, purpose="p"):
        pass
    assert target.read_text() == "keep\n"


def test_lock_file_directory_refused(tmp_path: Path) -> None:
    state = tmp_path / "build" / ".vergil-state"
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    (state / LOCK_NAME).mkdir()
    with (
        pytest.raises(CheckoutLockError, match="not a regular file"),
        checkout_lock(tmp_path, purpose="p"),
    ):
        pass


def test_lock_file_fifo_refused(tmp_path: Path) -> None:
    state = tmp_path / "build" / ".vergil-state"
    state.mkdir(parents=True, mode=0o700)
    state.chmod(0o700)
    os.mkfifo(state / LOCK_NAME)
    with (
        pytest.raises(CheckoutLockError, match="not a regular file"),
        checkout_lock(tmp_path, purpose="p"),
    ):
        pass


def test_lock_file_open_error_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An open failure that is not about the file's kind is raised as it is."""

    def denied(*_a: object) -> int:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("vergil_tooling.lib.checkout_lock.os.open", denied)
    with pytest.raises(PermissionError), checkout_lock(tmp_path, purpose="p"):
        pass


@pytest.mark.parametrize("purpose", ["", "  ", "two\nlines", "carriage\rreturn"])
def test_bad_purpose_refused(tmp_path: Path, purpose: str) -> None:
    with (
        pytest.raises(CheckoutLockError, match="one non-empty line"),
        checkout_lock(tmp_path, purpose=purpose),
    ):
        pass
    assert not (tmp_path / "build").exists()
