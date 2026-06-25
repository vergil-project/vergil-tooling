"""The transport interface.

The CLI orchestrates engine + transport; the engine never touches transport
directly. ``LocalFileTransport`` implements it now; a future ``GitHubTransport``
would implement the same read/write/git-fact contract so the recorder can run
against a remote relay. (The dual-agent polling methods were removed with the
loop in #1872.)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vergil_tooling.lib.pr_workflow.state import WorkflowState


class Transport(ABC):
    """Read/write the workflow state and surface git facts."""

    @abstractmethod
    def read(self) -> WorkflowState | None:
        """Return the current state, or None if no workflow exists yet."""

    @abstractmethod
    def write(self, state: WorkflowState) -> None:
        """Persist the state atomically."""

    @abstractmethod
    def head_sha(self) -> str:
        """Return the current HEAD commit SHA."""

    @abstractmethod
    def merge_base(self) -> str:
        """Return the merge-base SHA of the base ref and HEAD."""
