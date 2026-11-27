"""Release workflow data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.pending_release import PendingRelease
    from vergil_tooling.lib.release.state import IssueEntry, StateBlock
    from vergil_tooling.lib.release.supersede import Superseded
    from vergil_tooling.lib.release_lock.holder import LockOwner

# The package-index stage's name, by which a deferred index miss is listed.
INDEX_STAGE = "package-index"


@dataclass
class ReleaseContext:
    """Shared state that flows through every release phase."""

    repo: str
    version: str
    repo_root: Path
    version_override: str | None

    # True on vrg-release --resume: cd-publish then delivers the CD publish
    # jobs that a fresh run only records, and package-index dispatches the
    # index workflow itself before it waits (spec §4.5 of epic
    # vergil-project/.github#1440).
    resume: bool = False

    issue_number: int | None = None
    issue_url: str | None = None
    # The stage checklist block the tracking issue is created with, rendered
    # by the orchestrator once preflight has run. Carrying it in the issue's
    # creation body means there is never an issue without one, so an interrupt
    # anywhere in prepare leaves a release that --resume can pick up (#4367).
    progress_block: str | None = None
    release_branch: str | None = None
    # The develop commit a fresh start cuts the release branch from, and what
    # main..cut holds: the tracking issue's Contents section and state block
    # are built from them (spec §4.1-§4.2 of epic vergil-project/.github#1440).
    # Unset on a resume, whose cut is in the state block.
    cut: str | None = None
    pending_release: PendingRelease | None = None
    # The issues the release contains, from its PRs' closing references, and
    # on vrg-release --supersede those absorbed from the superseded release.
    issues: tuple[IssueEntry, ...] = ()
    # The open release a fresh vrg-release --supersede takes the place of,
    # read and checked by preflight (spec §4.7 of epic
    # vergil-project/.github#1440). Unset on a resume, whose state block
    # records the superseded version.
    superseded: Superseded | None = None
    # The tracking issue's state block as last read or written by this run.
    state: StateBlock | None = None
    # The identity-rule warnings (foreign traces on the tracking issue) this
    # run has printed, so each prints once per run however often the state
    # is read (spec §4.2 of epic vergil-project/.github#1440).
    tracking_warnings: set[str] = field(default_factory=set)
    release_pr_url: str | None = None

    # The release lock this run holds (spec §5.2 of epic
    # vergil-project/.github#1664): taken by a fresh start's preflight before
    # the cut, and the one writer of its stage, health, tracking issue and
    # worktree. None on a resume, and before preflight takes it.
    lock: LockOwner | None = None

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
    # The commit the tag peels to, recorded in the state block by confirm-main.
    tag_commit: str | None = None
    release_url: str | None = None

    develop_cd_run_id: str | None = None
    develop_cd_run_url: str | None = None

    # The index file the package-index stage saw each released package in
    # (deb Packages or rpm repomd.xml URL), one per [[package]] entry in
    # vergil.toml order. Empty when the repo has no [package] block or the
    # stage has not succeeded.
    package_index_urls: tuple[str, ...] = ()

    # What the install stage leaves for the human to read below the summary:
    # a failed run's full output. None after a run that succeeded, or with
    # none configured.
    install_message: str | None = None

    # The version-expanded [publish].install command block the stage runs
    # (issue #1643). None when no install is configured.
    install_commands: str | None = None

    # The release and commit a successful install reported installing, as
    # ``(X.Y.Z, commit)``, read from an ``installed vX.Y.Z (commit <sha>)``
    # line of its output (vrg-tooling-install prints one). None when it
    # reported neither, or did not run.
    install_reported: tuple[str, str] | None = None

    # What this run's release-records stage did: the records it posted, and
    # the issues it found already holding a counting record (spec §4.9 of
    # epic vergil-project/.github#1440).
    records_new: int = 0
    records_found: int = 0

    # Names of the stages that failed in this run, deferred publishes
    # included, in pipeline order. The hold rule (release.handoff.holds)
    # reads it: a later stage that needs a failed one does not run, so the
    # tracking issue stays open for vrg-release --resume (#1612; spec §4.4 of
    # epic vergil-project/.github#1440). A stage an earlier run failed is not
    # recorded done, so a resume runs it again and lists it here if it fails.
    deferred_failures: list[str] = field(default_factory=list)

    # True once this run's package-index stage missed the release in the
    # index. The stage runs again on every resume until the state block
    # records the index listed, so a miss is always this run's.
    index_missed: bool = False

    # update-deps stage (epic vergil-project/.github#381, spec §5.1). Skipped
    # with --without-deps, given on the run or recorded in the state block by
    # an earlier one. deps_failure is the human summary when the update did
    # not land; publish-status repeats it.
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
    def deferred_publish_failures(self) -> list[str]:
        """The publishes this release has deferred, derived from the state block.

        Each failed CD publish job recorded in ``state.failed_jobs``, by its
        job name and once however many runs it is recorded for, then
        ``package-index`` when this run's index wait missed the release
        (:attr:`index_missed`) and the state does not record it listed. The
        release itself is valid (tag and GitHub Release published); these are
        delivered by ``vrg-release --resume``. Separate from
        ``deferred_failures``, which the ``_tracked`` wrapper fills with
        failed stage names (#1853). Each read returns a new list: it is a
        view, never written (spec §4.3 of epic vergil-project/.github#1440).
        """
        failures: list[str] = []
        if self.state is not None:
            failures.extend(dict.fromkeys(job.job for job in self.state.failed_jobs))
        listed = self.state is not None and self.state.index_listed
        if self.index_missed and not listed and INDEX_STAGE not in failures:
            failures.append(INDEX_STAGE)
        return failures

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
