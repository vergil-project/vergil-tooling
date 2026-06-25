"""The transport interface.

The engine never touches this directly; the CLI orchestrates engine + transport.
``LocalFileTransport`` implements it now; a future ``GitHubTransport`` will
implement the same contract (enforced by the shared contract test) so the
identical loop can drive a live PR. Turn detection and termination live here,
behind the interface — never in the engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_workflow.state import WorkflowState


class Transport(ABC):
    """Read/write the workflow state and block until it is a role's turn."""

    @abstractmethod
    def read(self) -> WorkflowState | None:
        """Return the current state, or None if no workflow exists yet."""

    @abstractmethod
    def write(self, state: WorkflowState) -> None:
        """Persist the state atomically."""

    @abstractmethod
    def wait_until_present(
        self, *, timeout: float, waiting_for: str | None = None
    ) -> WorkflowState:
        """Block until a workflow exists. Raise WorkflowError on timeout.

        ``waiting_for`` (when set) names what is being waited on, for a periodic
        heartbeat so a long wait is visible rather than a silent hang."""

    @abstractmethod
    def wait_until_owner(
        self, status: str, *, timeout: float, waiting_for: str | None = None
    ) -> WorkflowState:
        """Block until ``state.status == status``. Raise WorkflowError on timeout.
        ``waiting_for`` drives a heartbeat for long waits (see ``wait_until_present``).

        Named ``wait_until_owner`` for historical compatibility; the parameter
        was renamed from ``role`` to ``status`` when the dual-agent ownership
        model was removed (#1872)."""

    @abstractmethod
    def head_sha(self) -> str:
        """Return the current HEAD commit SHA."""

    @abstractmethod
    def merge_base(self) -> str:
        """Return the merge-base SHA of the base ref and HEAD."""
