"""Tests for LocalFileTransport (state store + waiting)."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.lib.pr_workflow.local_transport"
_NOW = "2026-06-08T00:00:00Z"


def _state(owner: str = "audit"):
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    state.owner = owner
    return state


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    assert LocalFileTransport(tmp_path).read() is None


def test_write_then_read_roundtrips(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path)
    transport.write(_state())
    restored = transport.read()
    assert restored is not None
    assert restored.owner == "audit"
    assert (tmp_path / ".vergil" / "pr-workflow.json").is_file()


def test_wait_until_owner_returns_when_owner_matches(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    transport.write(_state(owner="user"))
    with patch(f"{_MOD}.time.sleep") as slept:
        state = transport.wait_until_owner("user", timeout=5.0)
    assert state.owner == "user"
    slept.assert_not_called()


def test_wait_until_owner_blocks_then_returns(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    transport.write(_state(owner="audit"))

    def flip(_seconds: float) -> None:
        transport.write(_state(owner="user"))

    with patch(f"{_MOD}.time.sleep", side_effect=flip) as slept:
        state = transport.wait_until_owner("user", timeout=5.0)
    assert state.owner == "user"
    slept.assert_called_once()


def test_wait_until_owner_times_out(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    transport.write(_state(owner="audit"))
    # monotonic advances past the deadline on the second reading.
    with patch(f"{_MOD}.time.monotonic", side_effect=[0.0, 0.0, 100.0]), \
         patch(f"{_MOD}.time.sleep"):
        with pytest.raises(WorkflowError, match="timed out"):
            transport.wait_until_owner("user", timeout=5.0)


def test_wait_until_owner_raises_on_counterpart_error(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    state = _state(owner="audit")
    state.error = {"by": "audit", "at": _NOW, "reason": "crashed hard"}
    transport.write(state)
    with patch(f"{_MOD}.time.sleep"):
        with pytest.raises(WorkflowError, match="counterpart reported an error"):
            transport.wait_until_owner("user", timeout=5.0)


def test_wait_until_present_times_out_when_no_file(tmp_path: Path) -> None:
    transport = LocalFileTransport(tmp_path, poll_interval=0.0)
    with patch(f"{_MOD}.time.monotonic", side_effect=[0.0, 0.0, 100.0]), \
         patch(f"{_MOD}.time.sleep"):
        with pytest.raises(WorkflowError, match="timed out waiting for the workflow file"):
            transport.wait_until_present(timeout=5.0)
