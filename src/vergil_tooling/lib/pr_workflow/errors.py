"""Domain errors for the PR workflow oracle."""

from __future__ import annotations


class WorkflowError(Exception):
    """Raised when workflow state is malformed or a verb is invalid for the state."""
