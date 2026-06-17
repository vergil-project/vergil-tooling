"""Domain errors for the PR workflow oracle."""

from __future__ import annotations


class WorkflowError(Exception):
    """Raised when workflow state is malformed or a verb is invalid for the state."""


class AlreadySubmittedError(WorkflowError):
    """Raised when a worktree's PR has already been submitted.

    The state file is retained (marked submitted) after a successful
    ``vrg-submit-pr`` so the worktree scanner can report it as in-flight
    rather than re-submitting it. Callers catch this to distinguish an
    in-flight worktree from one that is genuinely not ready. Subclasses
    ``WorkflowError`` so code that only knows the base type still treats it
    as a benign domain error, but it should be caught explicitly first.
    """

    def __init__(self, *, pr_url: str, pr_number: int | None = None) -> None:
        self.pr_url = pr_url
        self.pr_number = pr_number
        ref = f"PR #{pr_number}" if pr_number is not None else "a PR"
        super().__init__(f"already submitted as {ref} ({pr_url})")
