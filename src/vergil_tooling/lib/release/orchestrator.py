"""Declarative stage list for the vrg-release pipeline."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from vergil_tooling.lib.process_errors import command_text, error_text
from vergil_tooling.lib.progress import Stage, mark_warn, note
from vergil_tooling.lib.promote import promote
from vergil_tooling.lib.release import checklist
from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.cd_publish import deliver_failed_jobs
from vergil_tooling.lib.release.confirm import confirm_develop, confirm_main
from vergil_tooling.lib.release.conflict import merge_main_into_release
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.finalize import close_tracking, finalize, teardown_worktree
from vergil_tooling.lib.release.guard import TrackingIssueBreach
from vergil_tooling.lib.release.handoff import holds
from vergil_tooling.lib.release.install import install, minor
from vergil_tooling.lib.release.merge import (
    ReleaseConflictError,
    known_pr_state,
    sync_local_branch,
    wait_and_merge,
)
from vergil_tooling.lib.release.package_index import wait_for_index
from vergil_tooling.lib.release.preflight import preflight, run_audit
from vergil_tooling.lib.release.prepare import prepare
from vergil_tooling.lib.release.records import post_records
from vergil_tooling.lib.release.resume import ALWAYS_RUN
from vergil_tooling.lib.release.tracking import (
    comment_phase_complete,
    comment_phase_failed,
    ensure_checklist,
    tick_stage,
    write_state,
)
from vergil_tooling.lib.release.update_deps_stage import update_deps

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext


# merge-release merges main into a conflicting release PR at most this many
# times per run, pausing after each push so GitHub recomputes mergeable (#3526).
_MAX_CONFLICT_HEALS = 3
_CONFLICT_SETTLE_SECS = 10


@dataclass
class ReleaseState:
    """Pipeline context for vrg-release; ctx is populated by the preflight stage."""

    version_override: str | None
    repo_root: Path
    ctx: ReleaseContext | None = None
    resume: bool = False
    resume_version: str | None = None
    resume_issue_number: int | None = None
    without_deps: bool = False
    allow_pending_deps: bool = False


def _audit_stage(state: ReleaseState) -> None:
    run_audit(state.repo_root)


def _preflight_stage(state: ReleaseState) -> None:
    ctx = preflight(
        version_override=state.version_override,
        repo_root=state.repo_root,
        resume=state.resume,
        resume_version=state.resume_version,
        resume_issue_number=state.resume_issue_number,
        allow_pending_deps=state.allow_pending_deps,
        stages=_stage_names(),
    )
    ctx.resume = state.resume
    ctx.without_deps = _without_deps(ctx, given=state.without_deps)
    # The read-only stages before prepare ran before the issue existed, so the
    # checklist the issue is created with has them ticked already (#4367).
    names = _stage_names()
    ctx.progress_block = checklist.render(names, checked=names[: names.index("prepare")])
    state.ctx = ctx


def _without_deps(ctx: ReleaseContext, *, given: bool) -> bool:
    """Whether this run skips ``update-deps``: ``--without-deps`` now, or on an earlier run.

    A fresh start records the flag in the state block it creates. A resume
    reads it from the state block preflight read, and records a
    ``--without-deps`` given now there, so every later resume skips the
    update too (spec §4.3, §4.5 of epic vergil-project/.github#1440).
    """
    if not ctx.resume:
        # prepare creates the state block from ctx.without_deps.
        return given
    current = ctx.state
    if current is None:
        raise ReleaseError(
            phase="preflight",
            command="read_state",
            message=(
                "A resume holds no release state block after preflight, so whether "
                "--without-deps was given on an earlier run is unknown."
            ),
        )
    if given and not current.without_deps:
        write_state(ctx, replace(current, without_deps=True))
        return True
    return current.without_deps


def _tracked(name: str, fn: Callable[[ReleaseContext], None]) -> Callable[[ReleaseState], None]:
    """Wrap a phase fn with the state block and tracking-issue comments (command-local, per spec).

    A stage the state block records as done prints ``already done`` and
    returns, so a resume runs only what is not done. A stage the hold rule
    holds back (:func:`~vergil_tooling.lib.release.handoff.holds`: a stage it
    needs failed in this run) does not run, is marked with a warning and is
    not recorded done. A stage that succeeds records itself done in the
    state block with the facts later stages need (:func:`_record_done`). The checklist is
    still ticked for the human (spec §4.3 and §4.4 of epic
    vergil-project/.github#1440).
    """

    def stage(state: ReleaseState) -> None:
        ctx = state.ctx
        if ctx is None:
            raise ReleaseError(
                phase=name,
                command=name,
                message="release context missing — preflight did not run",
            )
        if ctx.state is not None and name in ctx.state.done:
            print(f"{name}: already done")
            note("already done")
            return
        held = holds(name, ctx.deferred_failures)
        if held:
            reason = f"held: {', '.join(held)} failed"
            print(f"{name}: {reason}; vrg-release --resume runs it once they succeed.")
            mark_warn(reason)
            return
        try:
            fn(ctx)
            _record_done(ctx, name)
        except TrackingIssueBreach:
            # A suspected breach stops the run before any write, so the
            # failure is never commented on the issue (spec §4.2).
            raise
        except ReleaseError as exc:
            comment_phase_failed(ctx, name, exc)
            _record_failure(ctx, name)
            raise
        except Exception as exc:
            wrapped = ReleaseError(
                phase=name,
                command=command_text(getattr(exc, "cmd", type(exc).__name__)),
                message=error_text(exc, output=False),
                detail=(getattr(exc, "stderr", None) or getattr(exc, "stdout", None)),
            )
            comment_phase_failed(ctx, name, wrapped)
            _record_failure(ctx, name)
            raise wrapped from exc
        comment_phase_complete(ctx, name, _phase_details(ctx, name))
        names = _stage_names()
        ensure_checklist(ctx, names, checked=names[: names.index(name)])
        tick_stage(ctx, name)

    return stage


def _record_done(ctx: ReleaseContext, name: str) -> None:
    """Record stage *name* done in the tracking issue's state block, with the facts on *ctx*.

    The facts are the PRs, the tag and the commit it peels to, the CD runs,
    and whether the package index lists the release (``index_listed``, set
    once ``package-index`` has seen every package); one *ctx* has not learnt
    keeps its recorded value. No state block
    on *ctx* is an error, never a stage left unrecorded.
    """
    current = ctx.state
    if current is None:
        raise ReleaseError(
            phase=name,
            command="write_state",
            message=(
                f"{name} succeeded, but the run holds no release state block to record "
                "it in; refusing to go on without the record a resume needs."
            ),
        )
    write_state(
        ctx,
        replace(
            current,
            done=(*current.done, name),
            release_pr=ctx.release_pr_url or current.release_pr,
            bump_pr=ctx.bump_pr_url or current.bump_pr,
            tag=ctx.tag or current.tag,
            tag_commit=ctx.tag_commit or current.tag_commit,
            main_cd_run=ctx.cd_run_id or current.main_cd_run,
            develop_cd_run=ctx.develop_cd_run_id or current.develop_cd_run,
            # package-index found every package in the index (spec §4.5).
            index_listed=current.index_listed or bool(ctx.package_index_urls),
            # install ran configured commands, so the records say it ran
            # (spec §4.9); with none configured it is done but did not run.
            install_done=current.install_done
            or (name == "install" and ctx.install_commands is not None),
        ),
    )


def _record_failure(ctx: ReleaseContext, name: str) -> None:
    """List failed stage *name* on *ctx*, for the hold rule and publish-status.

    A deferred publish (``cd-publish``, ``package-index``) is listed like any
    other failure: it holds back none of ``promote``, ``finalize`` and
    ``install``, but ``close-tracking`` needs it, so the tracking issue stays
    open for ``vrg-release --resume`` (spec §4.4 and §4.5 of epic
    vergil-project/.github#1440).
    """
    ctx.deferred_failures.append(name)


def _stage_names() -> list[str]:
    """The pipeline stage names, in execution order."""
    return [stage.name for stage in build_stages()]


def build_stages() -> list[Stage]:
    """The vrg-release pipeline, in execution order."""
    return [
        Stage("audit", _audit_stage, mode="fail_fast", skip_flag="skip_audit"),
        Stage("preflight", _preflight_stage, mode="fail_fast"),
        Stage("prepare", _tracked("prepare", prepare), mode="fail_fast"),
        Stage("merge-release", _tracked("merge-release", merge_release), mode="fail_fast"),
        Stage("confirm-main", _tracked("confirm-main", confirm_main), mode="fail_fast"),
        Stage(
            "back-merge-bump",
            _tracked("back-merge-bump", back_merge_and_bump),
            mode="fail_fast",
        ),
        # fail_fast, as every branch-work stage is: finalize prunes the release
        # branches, so it runs only once all of them have succeeded, and a
        # resume that reaches one always finds its branch (spec §4.3 of epic
        # vergil-project/.github#1440).
        Stage(
            "teardown-worktree",
            _tracked("teardown-worktree", teardown_worktree),
            mode="fail_fast",
        ),
        Stage(
            "confirm-develop",
            _tracked("confirm-develop", confirm_develop),
            mode="fail_defer",
        ),
        # Moves vX.Y once the tag and the GitHub Release exist, whatever the
        # CD publish jobs or the package index show (spec §4.6 of epic
        # vergil-project/.github#1440).
        Stage("promote", _tracked("promote", _promote_phase), mode="fail_defer"),
        # Delivers the CD publish jobs confirm-main and confirm-develop
        # recorded as failed: a fresh run fails it deferred, a resume re-runs
        # them (spec §4.5 of epic vergil-project/.github#1440).
        Stage("cd-publish", _tracked("cd-publish", deliver_failed_jobs), mode="fail_defer"),
        # A miss is a deferred publish: it holds back only close-tracking.
        Stage("package-index", _tracked("package-index", wait_for_index), mode="fail_defer"),
        # Prunes the release branches; the tracking issue stays open until
        # close-tracking.
        Stage("finalize", _tracked("finalize", finalize), mode="fail_defer"),
        # The first change on the new develop version, once the release is
        # out (epic vergil-project/.github#381, spec §5.1).
        Stage("update-deps", _tracked("update-deps", update_deps), mode="fail_defer"),
        # Needs promote: an install that follows the rolling vX.Y tag
        # (vergil-claude-plugin's marketplace, vrg-tooling-install v<MINOR>)
        # installs the release only once promote has moved it (issue #4739).
        Stage("install", _tracked("install", install), mode="fail_defer"),
        # A Released: record on every issue the release contains, once no
        # stage before it failed; a resume posts only the missing ones (spec
        # §4.9 of epic vergil-project/.github#1440).
        Stage("release-records", _tracked("release-records", post_records), mode="fail_defer"),
        # Closes the tracking issue only once no stage before it failed and
        # every record is posted, so a failed or deferred stage leaves it open
        # for --resume.
        Stage("close-tracking", _tracked("close-tracking", close_tracking), mode="fail_defer"),
        # The terminal exit summary: what is left, and the command that
        # finishes it.
        Stage("publish-status", _publish_status_stage, mode="fail_defer"),
    ]


def merge_release(ctx: ReleaseContext) -> None:
    """Phase 2: merge the release PR.

    Resume-safe: if the PR is already merged (a prior run got this far), skip
    the merge and hydrate ``release_merge_sha`` rather than re-attempting it.

    A CONFLICTING release PR self-heals: main is merged into the release
    branch and pushed, a CHANGELOG-only conflict resolved automatically, and
    any other conflict refused with recovery steps (#3526, ``conflict``).
    """
    if ctx.release_pr_url is None:
        raise ReleaseError(
            phase="merge-release",
            command="merge_release",
            message=("release_pr_url is not set — prepare phase may not have run."),
        )
    # release_pr_url was just created or listed by prepare (#3137).
    if known_pr_state(ctx.release_pr_url) == "MERGED":
        print("Release PR already merged — skipping merge.")
        ctx.release_merge_sha = "merged"
        return
    updates: list[None] = []
    heals = 0
    while True:
        try:
            wait_and_merge(
                ctx.release_pr_url,
                phase="merge-release",
                on_branch_updated=lambda: updates.append(None),
            )
            break
        except ReleaseConflictError as exc:
            # main moved since the branch was cut: merge it in, resolving a
            # CHANGELOG-only conflict, and wait again (#3526). Bounded, since
            # main can keep moving and GitHub's mergeable can read stale.
            heals += 1
            if heals > _MAX_CONFLICT_HEALS:
                raise ReleaseError(
                    phase="merge-release",
                    command="merge_main_into_release",
                    message=(
                        f"{exc} The release PR still conflicts after merging main into "
                        f"it {_MAX_CONFLICT_HEALS} times; re-run: vrg-release --resume"
                    ),
                ) from exc
            _heal_conflict(ctx, ctx.release_pr_url)
            time.sleep(_CONFLICT_SETTLE_SECS)
    if updates:
        # GitHub added a merge commit the local release branch never saw;
        # bring the managed worktree up to what actually merged (#3175).
        _sync_release_branch(ctx, ctx.release_pr_url)
    ctx.release_merge_sha = "merged"


def _heal_conflict(ctx: ReleaseContext, pr_url: str) -> None:
    if ctx.release_branch is None or ctx.worktree_path is None:
        raise ReleaseError(
            phase="merge-release",
            command="merge_main_into_release",
            message=(
                "The release PR conflicts with main, but release_branch or "
                "worktree_path is not set, so main cannot be merged into it."
            ),
        )
    merge_main_into_release(
        pr_url, branch=ctx.release_branch, worktree=ctx.worktree_path, phase="merge-release"
    )


def _sync_release_branch(ctx: ReleaseContext, pr_url: str) -> None:
    if ctx.release_branch is None or ctx.worktree_path is None:
        raise ReleaseError(
            phase="merge-release",
            command="sync_local_branch",
            message=(
                "The release PR was updated on GitHub, but release_branch or "
                "worktree_path is not set, so the local release branch cannot be synced."
            ),
        )
    sync_local_branch(
        pr_url, branch=ctx.release_branch, worktree=ctx.worktree_path, phase="merge-release"
    )


def _promote_phase(ctx: ReleaseContext) -> None:
    """Move the vX.Y rolling tag to this release.

    It runs once the tag and the GitHub Release exist, whatever the package
    index or the CD publish jobs show: while the index is deferred, vX.Y may
    name a version whose packages are not listed yet, which is accepted
    (spec §4.6 of epic vergil-project/.github#1440).
    """
    promote(ctx.version)


def _left(ctx: ReleaseContext) -> list[str]:
    """The tracked stages the state block does not record done, in pipeline order."""
    done = ctx.state.done if ctx.state is not None else ()
    return [name for name in _stage_names() if name not in ALWAYS_RUN and name not in done]


def _publish_status_stage(state: ReleaseState) -> None:
    """Terminal summary: name what is left of the release and the command that finishes it.

    A no-op when no stage failed. Otherwise the run already ends non-zero (a
    failed fail_defer stage), so this stage only warns, never raises a
    second failure (#3283): the warning names each failed stage, each stage
    still not done, a deferred publish, and a failed dependency update, and
    the one command that finishes the release, ``vrg-release --resume``, or,
    for a release that cannot finish, ``vrg-release --supersede X.Y.Z``
    (spec §4.4 of epic vergil-project/.github#1440).
    """
    ctx = state.ctx
    if ctx is None or not ctx.deferred_failures:
        return
    problems = [
        f"Release v{ctx.version} is not finished: {', '.join(ctx.deferred_failures)} failed"
        f"; not done: {', '.join(_left(ctx))}."
    ]
    if ctx.deferred_publish_failures:
        problems.append(
            f"Deferred publish: {', '.join(ctx.deferred_publish_failures)} (the tag and "
            "the GitHub Release are published)."
        )
    if ctx.deps_failure:
        problems.append(ctx.deps_failure)
    problems.append(
        f"Once the cause is fixed, finish it with `vrg-release --resume {ctx.version}`; "
        f"a release that cannot finish is superseded with "
        f"`vrg-release --supersede {ctx.version}`."
    )
    message = " ".join(problems)
    print(message)
    mark_warn(message)


def _records_details(ctx: ReleaseContext) -> str:
    """The release-records completion line: what this run posted and found."""
    issues = len(ctx.state.issues) if ctx.state is not None else 0
    if not issues:
        return "No records to post: the release's PRs close no issues."
    earlier = issues - ctx.records_new - ctx.records_found
    return (
        f"Released: records on all {issues} issue(s): {ctx.records_new} posted by this run, "
        f"{ctx.records_found} found on the issue, {earlier} recorded by an earlier run."
    )


def _phase_details(ctx: ReleaseContext, phase: str) -> str:
    """Build human-readable details from ctx for a completed phase."""
    lines: list[str] = []
    if phase == "prepare":
        if ctx.release_branch:
            lines.append(f"Branch: `{ctx.release_branch}`")
        if ctx.release_pr_url:
            lines.append(f"PR: {ctx.release_pr_url}")
        if ctx.issue_url:
            lines.append(f"Tracking issue: {ctx.issue_url}")
        if ctx.cleared_deps_prs:
            # #3286: closing a landed dependency-update PR is recorded, never silent.
            prs = ", ".join(ctx.cleared_deps_prs)
            lines.append(f"**Closed landed dependency-update PR(s)** (fix task completed): {prs}")
        if ctx.undeleted_deps_branches:
            # #3332: a closed PR's branch is never auto-deleted; a failed delete is named.
            branches = ", ".join(f"`{b}`" for b in ctx.undeleted_deps_branches)
            lines.append(
                f"**Could not delete the branch(es) of those closed PR(s)** — delete by "
                f"hand: {branches}"
            )
        if ctx.bypassed_deps_prs:
            # spec §5.3: bypassing the dependency-update gate is never silent.
            prs = ", ".join(ctx.bypassed_deps_prs)
            lines.append(
                f"**Bypassed pending dependency-update PR(s)** with `--allow-pending-deps`: {prs}"
            )
    elif phase == "merge-release":
        if ctx.release_pr_url:
            lines.append(f"Merged: {ctx.release_pr_url}")
    elif phase == "confirm-main":
        if ctx.tag:
            lines.append(f"Tag: `{ctx.tag}`")
        if ctx.release_url:
            lines.append(f"Release: {ctx.release_url}")
        if ctx.cd_run_url:
            lines.append(f"CD workflow: {ctx.cd_run_url}")
    elif phase == "back-merge-bump":
        if ctx.bump_pr_url:
            lines.append(f"Back-merge PR: {ctx.bump_pr_url}")
        if ctx.next_version:
            lines.append(f"Next version: {ctx.next_version}")
    elif phase == "confirm-develop":
        if ctx.develop_cd_run_url:
            lines.append(f"Develop CD: {ctx.develop_cd_run_url}")
    elif phase == "cd-publish":
        lines.append("No failed CD publish job is left to deliver.")
    elif phase == "promote":
        lines.append(f"Promoted v{minor(ctx.version)} -> v{ctx.version}")
    elif phase == "finalize":
        lines.append("Repository finalized.")
    elif phase == "release-records":
        lines.append(_records_details(ctx))
    elif phase == "close-tracking":
        lines.append("Tracking issue closed.")
    elif phase == "package-index":
        if ctx.package_index_urls:
            # Packages sharing one index file (same suite and arch) list it once.
            lines.extend(
                f"Package visible in index: {url}" for url in dict.fromkeys(ctx.package_index_urls)
            )
        else:
            lines.append("No [package] block; no package index to wait for.")
    elif phase == "install":
        if ctx.install_commands is None:
            lines.append("None configured: vergil.toml has no [publish].install.")
        else:
            lines.append(f"Install commands for v{ctx.version} ran.")
    elif phase == "update-deps":
        if ctx.without_deps:
            lines.append("Dependency update skipped (--without-deps).")
        elif ctx.deps_landed_pr_url:
            lines.append(
                "The dependency update for this develop version had already merged: "
                f"{ctx.deps_landed_pr_url}. Nothing to update."
            )
        else:
            lines.append("**Held back by declared bounds**")
            lines.append("")
            lines.append(ctx.deps_report or "")
            lines.append("")
            if ctx.deps_pr_url:
                lines.append(f"Dependency-update PR: {ctx.deps_pr_url}")
            else:
                lines.append("No dependency changes; no PR opened.")
    return "\n".join(lines)
