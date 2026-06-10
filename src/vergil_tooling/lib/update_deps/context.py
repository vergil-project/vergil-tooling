"""Shared state and error type for the vrg-update-deps pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.update_deps.updater import UpdateResult


@dataclass
class UpdateDepsContext:
    """State that flows through every vrg-update-deps stage."""

    repo: str
    repo_root: Path
    branch: str | None = None
    worktree_path: Path | None = None
    pr_url: str | None = None
    any_changes: bool = False
    results: list[UpdateResult] = field(default_factory=list)
    vergil_bump: str | None = None


class UpdateDepsError(Exception):
    """Raised when a vrg-update-deps stage fails."""

    def __init__(
        self,
        phase: str,
        command: str,
        message: str,
        detail: str | None = None,
    ) -> None:
        self.phase = phase
        self.command = command
        self.detail = detail
        super().__init__(message)
