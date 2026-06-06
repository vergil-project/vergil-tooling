"""Release workflow data types."""

from __future__ import annotations

from dataclasses import dataclass
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

    promote: bool = True
    skip_cd_docs: bool = False


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
