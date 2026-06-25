"""LocalFileTransport read/write round-trip (#1872)."""

from __future__ import annotations

from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.state import WorkflowState


def _state() -> WorkflowState:
    return WorkflowState(
        issue="42",
        branch="feature/42-x",
        base="origin/develop",
        status="ready",
        created_at="2026-06-25T00:00:00Z",
        updated_at="2026-06-25T00:00:00Z",
        git={"base_sha": "aaa", "head_sha": "bbb"},
    )


def test_read_returns_none_when_absent(tmp_path) -> None:
    assert LocalFileTransport(tmp_path).read() is None


def test_write_then_read_round_trips(tmp_path) -> None:
    transport = LocalFileTransport(tmp_path)
    transport.write(_state())
    assert transport.read() == _state()
