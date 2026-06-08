"""Contract every Transport implementation must satisfy.

Parametrized over a transport factory. Add GitHubTransport to ``_FACTORIES``
when it lands; it must pass this suite unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow import engine
from vergil_tooling.lib.pr_workflow.errors import WorkflowError
from vergil_tooling.lib.pr_workflow.local_transport import LocalFileTransport
from vergil_tooling.lib.pr_workflow.transport import Transport

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-06-08T00:00:00Z"

# Each factory takes a tmp_path and returns a Transport whose poll loop will not
# actually sleep (poll_interval 0); time.sleep is patched per test where needed.
_FACTORIES: list[Callable[["Path"], Transport]] = [
    lambda root: LocalFileTransport(root, poll_interval=0.0),
]


def _state(owner: str):
    state = engine.init_state(
        issue="1534", branch="b", base="origin/develop", mode="paired",
        head_sha="h0", base_sha="b0", user_token="u-1", now=_NOW,
    )
    state.owner = owner
    return state


@pytest.fixture(params=_FACTORIES)
def transport(request: pytest.FixtureRequest, tmp_path: Path) -> Transport:
    return request.param(tmp_path)


def test_read_is_none_before_any_write(transport: Transport) -> None:
    assert transport.read() is None


def test_write_read_roundtrip(transport: Transport) -> None:
    transport.write(_state("user"))
    restored = transport.read()
    assert restored is not None
    assert restored.owner == "user"
    assert restored.issue == "1534"


def test_wait_until_owner_returns_immediately_when_matching(transport: Transport) -> None:
    transport.write(_state("audit"))
    # No sleep should be needed; patch it so a bug would surface as a call.
    with patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep") as slept:
        state = transport.wait_until_owner("audit", timeout=5.0)
    assert state.owner == "audit"
    slept.assert_not_called()


def test_wait_until_present_returns_existing(transport: Transport) -> None:
    transport.write(_state("user"))
    with patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep") as slept:
        state = transport.wait_until_present(timeout=5.0)
    assert state.issue == "1534"
    slept.assert_not_called()


def test_wait_until_owner_raises_on_error_state(transport: Transport) -> None:
    state = _state("audit")
    state.error = {"by": "audit", "at": _NOW, "reason": "boom"}
    transport.write(state)
    with patch("vergil_tooling.lib.pr_workflow.local_transport.time.sleep"):
        with pytest.raises(WorkflowError, match="counterpart reported an error"):
            transport.wait_until_owner("user", timeout=5.0)
