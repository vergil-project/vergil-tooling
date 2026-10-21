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
    # The commit the back-merge PR's merge produced on develop (its
    # mergeCommit oid). confirm-develop verifies the CD run for exactly this
    # commit, not develop's tip, which may have moved by the time a resumed
    # release reaches it (#3329). Set by back-merge-bump on every path —
    # create, adopt-open and adopt-merged — so a resume re-hydrates it.
    bump_merge_sha: str | None = None

    cd_run_id: str | None = None
    cd_run_url: str | None = None
    tag: str | None = None
    develop_tag: str | None = None
    release_url: str | None = None

    develop_cd_run_id: str | None = None
    develop_cd_run_url: str | None = None

    # The index file the package-index stage saw the released package in
    # (deb Packages or rpm repomd.xml URL). None when the repo has no
    # [package] block or the stage has not succeeded.
    package_index_url: str | None = None

    consumer_refresh_message: str | None = None

    # The version-expanded consumer-refresh command block, without the
    # display wrapper, so ``vrg-release --install`` can execute exactly what
    # the message shows (issue #1643). None when no template is configured.
    consumer_refresh_commands: str | None = None

    # Names of fail-defer stages that errored. close-finalize leaves the
    # tracking issue open (and skips cleanup) when any are pending, so the
    # release stays resumable (#1612).
    deferred_failures: list[str] = field(default_factory=list)

    # Families of CD publish jobs that did not succeed (e.g. "docker-publish",
    # "docs"). The release itself is valid (tag + Release published); these are
    # re-publishable. Surfaced by the publish-status stage (exit 1), the open
    # tracking issue, and the consumer-refresh hold-warning. Separate from
    # deferred_failures, which the _tracked wrapper fills with failed STAGE
    # names (#1853).
    deferred_publish_failures: list[str] = field(default_factory=list)

    promote: bool = True

    # update-deps stage (epic vergil-project/.github#381, spec §5.1). Skipped
    # with --without-deps. deps_failure is the human summary when the update
    # did not land; publish-status repeats it.
    without_deps: bool = False
    deps_report: str | None = None
    deps_pr_url: str | None = None
    deps_fix_issue_url: str | None = None
    deps_failure: str | None = None
    # Set when the update for the develop version had already merged before
    # this stage ran (a re-run such as --resume, #3330): nothing was updated.
    deps_landed_pr_url: str | None = None

    # Open dependency-update PRs this release bypassed with --allow-pending-deps
    # (spec §5.3); named in the prepare-stage tracking comment so the bypass is
    # never silent.
    bypassed_deps_prs: list[str] = field(default_factory=list)

    # Open dependency-update PRs preflight closed because their fix task closed
    # as completed — the update landed through a (squash-merged) fix PR, so
    # GitHub never marked the deps PR merged (#3286). Named in the
    # prepare-stage tracking comment alongside any bypass.
    cleared_deps_prs: list[str] = field(default_factory=list)

    # Head branches of those closed PRs that preflight could not delete
    # (#3332). GitHub's delete-on-merge never covers a closed PR, so a leftover
    # is named in the prepare-stage tracking comment rather than left silent.
    undeleted_deps_branches: list[str] = field(default_factory=list)

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
