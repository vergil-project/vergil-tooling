"""Shared state and error type for the vrg-update-deps pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.update_deps.updater import UpdateResult

# Every dependency-update PR carries this label from creation (spec §5.2); the
# vrg-release preflight gate refuses while any open PR has it (spec §5.3).
DEPS_LABEL = "dependency-update"

FailureKind = Literal["update", "validation", "merge"]


@dataclass
class DepsFailure:
    """A recorded (not raised) failure; the status stage surfaces it loud.

    ``update`` — an updater or hook failed before validation: no PR.
    ``validation`` — the single vrg-validate run failed: PR stays open.
    ``merge`` — remote CI failed or the merge aborted: PR stays open.
    """

    kind: FailureKind
    message: str
    output_tail: str
    reproduce: str


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
    # The develop version the update is the first change on (spec §5.1).
    version: str | None = None
    failure: DepsFailure | None = None
    fix_issue_url: str | None = None

    @property
    def has_work(self) -> bool:
        """True when there is a diff that goes through validation and a PR.

        A pre-validation (``update``) failure has no PR, even if an earlier
        updater already committed (spec §5.2).
        """
        return self.any_changes and (self.failure is None or self.failure.kind != "update")


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
