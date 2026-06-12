"""Release workflow data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class ReleaseContext:
    """Shared state that flows through every release phase."""

    repo: str
    version: str
    repo_root: Path
    version_override: str | None

    issue_number: int | None = None
    issue_url: str | None = None
    release_branch: str | None = None
    release_pr_url: str | None = None

    # Managed worktree all release branch work happens in, so the root
    # checkout's HEAD never moves while a release runs (#1578).
    worktree_path: Path | None = None

    release_merge_sha: str | None = None

    bump_pr_url: str | None = None
    next_version: str | None = None

    cd_run_id: str | None = None
    cd_run_url: str | None = None
    tag: str | None = None
    develop_tag: str | None = None
    release_url: str | None = None

    develop_cd_run_id: str | None = None
    develop_cd_run_url: str | None = None

    consumer_refresh_message: str | None = None

    # Names of fail-defer stages that errored. close-finalize leaves the
    # tracking issue open (and skips cleanup) when any are pending, so the
    # release stays resumable (#1612).
    deferred_failures: list[str] = field(default_factory=list)

    promote: bool = True

    @property
    def work_root(self) -> Path:
        """Directory release artifacts are written into and git runs in.

        Post-#1600 every release phase runs inside the managed worktree
        (preflight chdir's into it), so artifact writes — CHANGELOG, release
        notes, version bump — must target the worktree. ``repo_root`` stays
        the main checkout, which ``finalize`` chdir's back to so
        ``vrg-finalize-pr`` (which refuses to run outside the main worktree)
        works. Falls back to ``repo_root`` when no worktree is set (defensive;
        preflight always sets ``worktree_path``).
        """
        return self.worktree_path or self.repo_root


class ReleaseError(Exception):
    """Raised when a release phase fails."""

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
