"""Contract every Transport implementation must satisfy.

Parametrized over a transport factory. Add GitHubTransport to ``_FACTORIES``
when it lands; it must pass this suite unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.pr_workflow.transport import Transport

_NOW = "2026-06-08T00:00:00Z"

# Each factory takes a tmp_path and returns a Transport whose poll loop will not
# actually sleep (poll_interval 0); time.sleep is patched per test where needed.
_FACTORIES: list[Callable[[Path], Transport]] = [
    lambda root: LocalFileTransport(root, poll_interval=0.0),
]


def _state(status: str = "implementing"):
    state = engine.init_state(
        issue="1534",
        branch="b",
        base="origin/develop",
        head_sha="h0",
        base_sha="b0",
        now=_NOW,
    )
    state.status = status
    return state


@pytest.fixture(params=_FACTORIES)
def transport(request: pytest.FixtureRequest, tmp_path: Path) -> Transport:
    return request.param(tmp_path)


def test_read_is_none_before_any_write(transport: Transport) -> None:
    assert transport.read() is None


def test_write_read_roundtrip(transport: Transport) -> None:
    transport.write(_state("ready"))
    restored = transport.read()
    assert restored is not None
    assert restored.status == "ready"
    assert restored.issue == "1534"


def test_wait_until_owner_returns_immediately_when_matching(transport: Transport) -> None:
    transport.write(_state("ready"))
    # No sleep should be needed; patch it so a bug would surface as a call.
    with patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep") as slept:
        state = transport.wait_until_owner("ready", timeout=5.0)
    assert state.status == "ready"
    slept.assert_not_called()


def test_wait_until_present_returns_existing(transport: Transport) -> None:
    transport.write(_state("implementing"))
    with patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep") as slept:
        state = transport.wait_until_present(timeout=5.0)
    assert state.issue == "1534"
    slept.assert_not_called()


def test_wait_until_owner_times_out_when_status_never_matches(transport: Transport) -> None:
    transport.write(_state("implementing"))
    with (
        patch(
            "vergil_tooling.lib.pr_workflow.local_transport.time.monotonic",
            side_effect=[0.0, 0.0, 100.0],
        ),
        patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep"),
        pytest.raises(WorkflowError, match="timed out"),
    ):
        transport.wait_until_owner("ready", timeout=5.0)
