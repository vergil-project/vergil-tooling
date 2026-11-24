"""Phase 3/5: Verify CD workflow and publish artifacts.

Every read of the CD run (its URL, the watch, its jobs) and of the GitHub
Release goes through :func:`retry.retry_known_resource`: the run ID was just
returned by ``gh run list`` and the release was just published by the CD
release job, so GitHub has established they exist and a 404 only means "not
readable yet" (#3137).
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from vergil_tooling.lib import ci_evidence, git, github, progress, retry
from vergil_tooling.lib.cd_runs import (
    _supersede_group,
    failed_job_names,
    is_docs_job,
    is_release_job,
)
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.state import FailedJob
from vergil_tooling.lib.release.subprocess import watch_workflow
from vergil_tooling.lib.release.tracking import write_state

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.release.context import ReleaseContext
    from vergil_tooling.lib.release.state import StateBlock

_CD_WORKFLOW = "cd.yml"
_CD_POLL_INTERVAL = 10
_CD_POLL_ATTEMPTS = 30
# A reusable-workflow leaf job's conclusion can lag the run-level status by a
# few seconds in the jobs API (issue #1611); poll for it to settle.
_JOB_SETTLE_INTERVAL = 5
_JOB_SETTLE_ATTEMPTS = 12


def confirm_main(ctx: ReleaseContext) -> None:
    """Watch CD on main: hard-verify the release, defer other publish jobs.

    A run whose release job a concurrency supersede cancelled published
    nothing, so it is dispatched again, once (:func:`_redispatch_main_cd`). A
    supersede that cancelled only other jobs (the docs job, whose ``cd-docs``
    group develop shares) is not: the release job is verified as usual and the
    cancelled job is deferred.

    A release job that did not succeed records its run as
    ``failed_release_run`` in the state block before the stage fails. The
    resume that follows re-runs that CD run, and never the merged release
    PR's CI run, watching it before judging it as here
    (:func:`_recover_failed_release_run`). Every other job that did not
    succeed is recorded in ``failed_jobs`` and deferred to ``cd-publish``
    (spec §4.5 of epic vergil-project/.github#1440).
    """
    _recover_failed_release_run(ctx)
    run_id, run_url = _watch_cd(ctx, branch="main")
    jobs = _settled_run_jobs(ctx, run_id, (is_release_job,))
    group = _release_supersede_group(run_id, run_url, jobs)
    if group is not None:
        superseded_id = run_id
        run_id, run_url = _redispatch_main_cd(ctx, superseded_id, run_url, group)
        jobs = _settled_run_jobs(ctx, run_id, (is_release_job,))
        group = _release_supersede_group(run_id, run_url, jobs)
        if group is not None:
            raise ReleaseError(
                phase="confirm-main",
                command=f"gh workflow run {_CD_WORKFLOW} --ref main",
                message=(
                    f"CD run {run_id} on main, dispatched again after CD run "
                    f"{superseded_id} was superseded, was superseded again: GitHub "
                    f"cancelled it for a higher-priority run waiting on concurrency "
                    f"group '{group}'. {run_url}. Once main's CD queue has cleared, "
                    f"resume with `vrg-release --resume {ctx.version}`, which "
                    "dispatches it again."
                ),
            )
    ctx.cd_run_id = run_id
    ctx.cd_run_url = run_url

    _verify_release_job(jobs, on_failure=lambda: _record_failed_release(ctx, run_id))

    if not _record_publish_state(
        ctx, run_id, jobs, branch="main", deferred_label="Publish deferred (release is valid)"
    ):
        print("  All CD jobs succeeded.")

    _verify_artifacts(ctx)
    print(f"Release v{ctx.version} confirmed.")


def confirm_develop(ctx: ReleaseContext) -> None:
    """Watch CD on develop; defer a docs publish failure rather than raise.

    A run a concurrency supersede cancelled is a loud warning, never a failure
    or a deferred publish, as ``vrg-finalize-pr`` reports one: the run that
    took its place publishes instead.
    """
    run_id, run_url = _watch_cd(ctx, branch="develop")
    ctx.develop_cd_run_id = run_id
    ctx.develop_cd_run_url = run_url

    jobs = _settled_run_jobs(ctx, run_id, (is_docs_job,))
    if _warn_develop_superseded(run_id, run_url, jobs):
        return
    if not _record_publish_state(
        ctx, run_id, jobs, branch="develop", deferred_label="Publish deferred on develop"
    ):
        print("Develop CD verified.")


def _cancelled(job: dict[str, Any]) -> bool:
    return job.get("conclusion") == "cancelled"


def _release_supersede_group(run_id: str, run_url: str, jobs: list[dict[str, Any]]) -> str | None:
    """The concurrency group that superseded main CD run *run_id*'s release, or ``None``.

    Only a supersede that cancelled the release job, or every job, counts: one
    that cancelled only other jobs leaves a release that ran. Any other cancel
    returns ``None`` and is judged by the release-job gate. A cancel that
    cannot be checked for a supersede fails loudly, naming the lookup's error.
    """
    release = _find_job(jobs, is_release_job)
    release_cancelled = release is not None and _cancelled(release)
    if not (release_cancelled or (jobs and all(_cancelled(job) for job in jobs))):
        return None
    try:
        return _supersede_group(run_id, run_url)
    except (subprocess.CalledProcessError, ValueError) as exc:
        raise ReleaseError(
            phase="confirm-main",
            command=f"gh run view {run_id} --json jobs",
            message=(
                f"CD run {run_id} on main was cancelled, and whether a concurrency "
                f"supersede cancelled it could not be checked: {exc}. {run_url}"
            ),
        ) from exc


def _redispatch_main_cd(
    ctx: ReleaseContext, superseded_id: str, superseded_url: str, group: str
) -> tuple[str, str]:
    """Dispatch main's CD again for superseded run *superseded_id*; watch the new run.

    The superseded run's ID and the dispatch time are recorded before the
    dispatch, and the new run is the one on the same head SHA with another ID,
    event ``workflow_dispatch`` and created at or after that time: the
    cancelled run is never watched again. Returns the new run's ID and URL.
    """
    head_sha = retry.retry_known_resource(
        lambda: github.read_output(
            "run",
            "view",
            "--repo",
            ctx.repo,
            superseded_id,
            "--json",
            "headSha",
            "--jq",
            ".headSha",
        ),
        resource=_run_resource(superseded_id),
    )
    # createdAt has whole-second resolution, so the dispatch time does too.
    dispatched_at = datetime.now(UTC).replace(microsecond=0)
    print(
        f"  CD run {superseded_id} was superseded before its release ran: GitHub "
        f"cancelled it for a higher-priority run waiting on concurrency group "
        f"'{group}'. {superseded_url}"
    )
    print(
        f"  Dispatching {_CD_WORKFLOW} on main again at {dispatched_at.isoformat()} "
        f"(superseded run: {superseded_id})..."
    )
    github.run("workflow", "run", _CD_WORKFLOW, "--repo", ctx.repo, "--ref", "main")
    run_id = _poll_for_dispatched_run(
        ctx, head_sha, superseded_id=superseded_id, dispatched_at=dispatched_at
    )
    run_url = _watch_run(ctx, run_id)
    return run_id, run_url


def _poll_for_dispatched_run(
    ctx: ReleaseContext, head_sha: str, *, superseded_id: str, dispatched_at: datetime
) -> str:
    """The ID of the CD run that confirm's dispatch on main started."""
    for _ in range(_CD_POLL_ATTEMPTS):
        runs = json.loads(
            github.read_output(
                "run",
                "list",
                "--repo",
                ctx.repo,
                "--workflow",
                _CD_WORKFLOW,
                "--branch",
                "main",
                "--limit",
                "10",
                "--json",
                "databaseId,headSha,event,createdAt",
            )
            or "[]"
        )
        for run in runs:
            if (
                run.get("headSha") == head_sha
                and str(run.get("databaseId")) != superseded_id
                and run.get("event") == "workflow_dispatch"
                and datetime.fromisoformat(run["createdAt"]) >= dispatched_at
            ):
                return str(run["databaseId"])
        time.sleep(_CD_POLL_INTERVAL)

    raise ReleaseError(
        phase="confirm-main",
        command=f"gh run list --workflow {_CD_WORKFLOW} --branch main",
        message=(
            f"CD on main was re-dispatched at {dispatched_at.isoformat()} after CD run "
            f"{superseded_id} was superseded, but no dispatched run for commit "
            f"{head_sha[:12]} registered. Check main's runs in the Actions tab, then "
            f"resume with `vrg-release --resume {ctx.version}`."
        ),
    )


def _warn_develop_superseded(run_id: str, run_url: str, jobs: list[dict[str, Any]]) -> bool:
    """Warn and return True when a concurrency supersede cancelled develop run *run_id*.

    A cancel that cannot be checked is said so, and the run is then judged as
    any other cancel (its cancelled jobs deferred).
    """
    if not any(_cancelled(job) for job in jobs):
        return False
    try:
        group = _supersede_group(run_id, run_url)
    except (subprocess.CalledProcessError, ValueError) as exc:
        print(f"  Could not check whether a concurrency supersede cancelled CD run {run_id}: {exc}")
        return False
    if group is None:
        return False
    print("  WARNING: the develop CD run was superseded, not failed.")
    print(
        f"  GitHub cancelled CD run {run_id} for a higher-priority run waiting on "
        f"concurrency group '{group}'; that run publishes in its place. {run_url}"
    )
    progress.mark_warn(f"CD run {run_id} superseded (concurrency group '{group}')")
    return True


def judge_publish_jobs(
    ctx: ReleaseContext, run_id: str, jobs: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """The full names of CD run *run_id*'s publish jobs that failed, and of its stale records.

    A completed non-release job that did not succeed has failed
    (:func:`cd_runs.failed_job_names`). A job still non-terminal after the
    settle budget has no conclusion to judge (#3352). ``gh run watch``
    already saw the run finish, so when the run itself concluded ``success``
    GitHub is asserting every job succeeded or skipped: the job's record is
    stale, returned second and never as a failure. Under any other run
    conclusion it may genuinely have failed, so it is returned as failed.
    The release job is never judged here: :func:`_verify_release_job` is.
    """
    failed = failed_job_names(jobs)
    stale: list[str] = []
    unsettled = _unsettled_jobs(jobs)
    if unsettled:
        if _run_conclusion(ctx, run_id) == "success":
            stale = unsettled
        else:
            failed.extend(name for name in unsettled if name not in failed)
    return failed, stale


def _current_state(ctx: ReleaseContext, phase: str) -> StateBlock:
    """*ctx*'s state block; none is an error, never a failure left unrecorded."""
    if ctx.state is None:
        raise ReleaseError(
            phase=phase,
            command="write_state",
            message=(
                "The run holds no release state block to record the CD run's outcome in; "
                "refusing to go on without the record a resume needs."
            ),
        )
    return ctx.state


def record_failed_jobs(
    ctx: ReleaseContext,
    branch: Literal["main", "develop"],
    failed: Sequence[FailedJob],
    *,
    replaces: Callable[[FailedJob], bool],
    phase: str,
) -> None:
    """Write *failed* into the state block's ``failed_jobs`` in place of those *replaces* picks.

    Every other recorded job is kept, in order. A judged ``main`` run that
    got this far has a release job that succeeded, so ``failed_release_run``
    is cleared with it. Nothing is written when nothing changes.
    """
    current = _current_state(ctx, phase)
    kept = tuple(job for job in current.failed_jobs if not replaces(job))
    release_run = None if branch == "main" else current.failed_release_run
    new = replace(current, failed_jobs=(*kept, *failed), failed_release_run=release_run)
    if new != current:
        write_state(ctx, new)


def _record_publish_state(
    ctx: ReleaseContext,
    run_id: str,
    jobs: list[dict[str, Any]],
    *,
    branch: Literal["main", "develop"],
    deferred_label: str,
) -> bool:
    """Record deferred publish failures and report stale job records; True if any.

    The jobs :func:`judge_publish_jobs` finds failed replace every job
    recorded for *branch* in the state block's ``failed_jobs``, each as its
    run ID and full job name, so a resume knows exactly what to re-run. A
    job that never settled is printed as ``<job> (never settled)``; a stale
    record is reported by name as a warning but never recorded.
    """
    failed, stale = judge_publish_jobs(ctx, run_id, jobs)
    unsettled = set(_unsettled_jobs(jobs))
    record_failed_jobs(
        ctx,
        branch,
        [FailedJob(branch=branch, run_id=run_id, job=name) for name in failed],
        replaces=lambda job: job.branch == branch,
        phase=f"confirm-{branch}",
    )

    reasons: list[str] = []
    if failed:
        deferred = [f"{name} (never settled)" if name in unsettled else name for name in failed]
        print(f"  {deferred_label}: {', '.join(deferred)}")
        reasons.append(f"publish deferred: {', '.join(deferred)}")
    if stale:
        print(
            f"  Stale job record (run concluded success; ignored): {', '.join(stale)} "
            "never reached 'completed' in the jobs API"
        )
        reasons.append(f"stale job record: {', '.join(stale)}")
    if reasons:
        # mark_warn is last-call-wins, so the stage carries one combined reason.
        progress.mark_warn("; ".join(reasons))
    return bool(reasons)


def _watch_cd(
    ctx: ReleaseContext,
    *,
    branch: str,
) -> tuple[str, str]:
    print(f"Waiting for {_CD_WORKFLOW} on {branch}...")
    head_sha = _cd_head_sha(ctx, branch)

    run_id = _poll_for_run(ctx.repo, branch, head_sha)
    return run_id, _watch_run(ctx, run_id)


def _watch_run(ctx: ReleaseContext, run_id: str, *, check_status: bool = False) -> str:
    """Print run *run_id*'s URL, watch it to completion, and return the URL.

    With *check_status* the watch is ``gh run watch --exit-status``, so a run
    that did not succeed raises ``CalledProcessError``. Without it, the caller
    judges the run's jobs itself.
    """
    resource = _run_resource(run_id)

    run_url = retry.retry_known_resource(
        lambda: github.read_output(
            "run",
            "view",
            "--repo",
            ctx.repo,
            run_id,
            "--json",
            "url",
            "--jq",
            ".url",
        ),
        resource=resource,
    )
    print(f"  Workflow run: {run_url}")

    retry.retry_known_resource(
        lambda: watch_workflow(ctx.repo, run_id, check_status=check_status),
        resource=resource,
    )

    print(f"  CD workflow completed: {run_url}")
    return run_url


def _run_attempt(ctx: ReleaseContext, run_id: str) -> int:
    """Run *run_id*'s latest attempt number."""
    out = retry.retry_known_resource(
        lambda: github.read_output(
            "run", "view", "--repo", ctx.repo, run_id, "--json", "attempt", "--jq", ".attempt"
        ),
        resource=_run_resource(run_id),
    )
    try:
        return int(out)
    except ValueError as exc:
        msg = f"gh run view {run_id} --json attempt returned {out!r}, not an attempt number"
        raise ValueError(msg) from exc


def rerun_and_watch(
    ctx: ReleaseContext,
    run_id: str,
    *,
    rerun_args: Sequence[str] = (),
    check_status: bool,
    phase: str,
) -> str:
    """``gh run rerun <run_id> <rerun_args>``, then watch the new attempt; return its URL.

    The attempt number is read before the re-run and polled until it moves
    on, so the watch never returns on the attempt that already finished. An
    attempt that never registers fails loudly. With *check_status* the watch
    is ``--exit-status`` and a run that did not succeed raises
    ``CalledProcessError``; without it the caller judges the jobs.
    """
    before = _run_attempt(ctx, run_id)
    github.run("run", "rerun", run_id, "--repo", ctx.repo, *rerun_args)
    for _ in range(_CD_POLL_ATTEMPTS):
        if _run_attempt(ctx, run_id) > before:
            return _watch_run(ctx, run_id, check_status=check_status)
        time.sleep(_CD_POLL_INTERVAL)
    command = " ".join(("gh", "run", "rerun", run_id, *rerun_args))
    raise ReleaseError(
        phase=phase,
        command=command,
        message=(
            f"Run {run_id} was re-run, but no attempt after attempt {before} registered. "
            f"Check the run in the Actions tab, then resume with "
            f"`vrg-release --resume {ctx.version}`."
        ),
    )


def _cd_head_sha(ctx: ReleaseContext, branch: str) -> str:
    """The commit whose CD run the confirm stage on *branch* must verify.

    develop: the back-merge PR's merge commit, recorded by back-merge-bump.
    develop's tip is not used — on ``--resume`` anything merged since (a
    dependency-update or feature PR) has moved it, and its CD run says nothing
    about the release (#3329). No recorded SHA fails loudly; there is no
    fallback to the tip.

    main: the tip of ``origin/main``. Only release PRs merge to main, so its
    tip is the release merge.
    """
    if branch == "develop":
        if not ctx.bump_merge_sha:
            raise ReleaseError(
                phase="confirm-develop",
                command="gh pr view --json mergeCommit",
                message=(
                    "The back-merge PR's merge commit SHA is not recorded "
                    f"(back-merge PR: {ctx.bump_pr_url}), so the develop CD run "
                    "for the release cannot be identified. Refusing to verify "
                    "develop's tip instead. Check the back-merge PR is merged, "
                    "then resume with vrg-release --resume."
                ),
            )
        return ctx.bump_merge_sha
    git.run("fetch", "origin", branch)
    return git.read_output("rev-parse", f"origin/{branch}")


def _poll_for_run(repo: str, branch: str, head_sha: str) -> str:
    for _ in range(_CD_POLL_ATTEMPTS):
        run_id = github.read_output(
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            _CD_WORKFLOW,
            "--branch",
            branch,
            "--limit",
            "5",
            "--json",
            "databaseId,headSha",
            "--jq",
            f'[.[] | select(.headSha == "{head_sha}")][0].databaseId // empty',
        )
        if run_id:
            return run_id
        time.sleep(_CD_POLL_INTERVAL)

    raise ReleaseError(
        phase=f"confirm-{branch}",
        command=f"gh run list --workflow {_CD_WORKFLOW}",
        message=f"No CD workflow run found on {branch} for commit {head_sha[:12]}.",
    )


def _run_resource(run_id: str) -> str:
    """Human label for the just-listed CD run, used in the 404-budget error."""
    return f"CD run {run_id}"


def _fetch_run_jobs(ctx: ReleaseContext, run_id: str) -> list[dict[str, Any]]:
    """Return the ``jobs`` array for *run_id* from the GitHub API."""
    out = retry.retry_known_resource(
        lambda: github.read_output("run", "view", "--repo", ctx.repo, run_id, "--json", "jobs"),
        resource=_run_resource(run_id),
    )
    data = json.loads(out) if out.strip() else {}
    jobs: list[dict[str, Any]] = data.get("jobs", [])
    return jobs


def _run_conclusion(ctx: ReleaseContext, run_id: str) -> str:
    """Return the run-level ``conclusion`` of *run_id* (empty if none)."""
    return retry.retry_known_resource(
        lambda: github.read_output(
            "run", "view", "--repo", ctx.repo, run_id, "--json", "conclusion", "--jq", ".conclusion"
        ),
        resource=_run_resource(run_id),
    ).strip()


JobMatcher = Callable[[str], bool]


def _find_job(jobs: list[dict[str, Any]], matches: JobMatcher) -> dict[str, Any] | None:
    """First job whose name satisfies ``matches``."""
    for job in jobs:
        if matches(job.get("name", "")):
            return job
    return None


def _settled_run_jobs(
    ctx: ReleaseContext, run_id: str, expected: tuple[JobMatcher, ...]
) -> list[dict[str, Any]]:
    """Poll until a job satisfying every *expected* matcher is ``completed``.

    ``gh run watch`` returns when the run-level status is terminal, but a
    reusable-workflow leaf job's ``conclusion`` can still be ``null`` in the
    jobs API for a few seconds (issue #1611). Reading once raced that window
    and aborted an already-succeeded release. Polling for ``completed`` closes
    the race; a genuinely absent job never settles and the final snapshot lets
    the caller report it as not found.

    Each matcher must identify the exact job the caller then verifies: the
    release settle uses ``is_release_job`` (the leaf segment), because
    cd-release also runs ``release / package-*`` jobs that finish first and
    would end the wait early under a substring match (#3102).

    Every other job must settle too, within the same budget: the same lag hits
    any leaf, and a non-terminal job has no conclusion to judge (#3352). A job
    that never settles is left in the final snapshot for the caller to report
    (``_unsettled_jobs``).
    """
    jobs: list[dict[str, Any]] = []
    for attempt in range(_JOB_SETTLE_ATTEMPTS):
        jobs = _fetch_run_jobs(ctx, run_id)
        if all(
            (job := _find_job(jobs, matches)) is not None and job.get("status") == "completed"
            for matches in expected
        ) and not _unsettled_jobs(jobs):
            return jobs
        if attempt < _JOB_SETTLE_ATTEMPTS - 1:
            time.sleep(_JOB_SETTLE_INTERVAL)
    return jobs


_RELEASE_JOB_NAME = "release / release"


def _verify_release_job(
    jobs: list[dict[str, Any]], *, on_failure: Callable[[], str] | None = None
) -> None:
    """Hard gate: the release job must exist and have concluded ``success``.

    Identified by its leaf segment ``release`` (see ``is_release_job``), which
    matches both the reusable leaf ``release / release`` and vergil-actions'
    inline ``cd / release`` while still rejecting a ``release``-prefixed decoy —
    the release job is the single load-bearing assertion. A renamed/absent
    release job fails closed (#1853, #2001).

    A release job that ran and did not succeed calls *on_failure* and appends
    its text to the error. It is called only on that failure, so a passing
    release makes none of its writes; it never turns the failure into
    anything but a raise.
    """
    for job in jobs:
        if is_release_job(job.get("name", "")):
            conclusion = job.get("conclusion")
            if conclusion != "success":
                message = f"Release job did not succeed (conclusion: '{conclusion}')."
                if on_failure is not None:
                    message = f"{message} {on_failure()}"
                raise ReleaseError(
                    phase="confirm-main",
                    command=f"verify job '{_RELEASE_JOB_NAME}'",
                    message=message,
                )
            return
    raise ReleaseError(
        phase="confirm-main",
        command=f"verify job '{_RELEASE_JOB_NAME}'",
        message=f"Release job '{_RELEASE_JOB_NAME}' not found in the workflow run.",
    )


def _record_failed_release(ctx: ReleaseContext, cd_run_id: str) -> str:
    """Record CD run *cd_run_id* as ``failed_release_run``; return the resume instruction.

    Written before confirm-main fails, so the resume knows which run to
    recover (spec §4.5 of epic vergil-project/.github#1440).
    """
    current = _current_state(ctx, "confirm-main")
    if current.failed_release_run != cd_run_id:
        write_state(ctx, replace(current, failed_release_run=cd_run_id))
    return (
        f"CD run {cd_run_id} is recorded as the failed release run. Once the cause is "
        f"fixed, resume with `vrg-release --resume {ctx.version}`: it re-runs this CD run, "
        "watching it, and judges the release again."
    )


def _recover_failed_release_run(ctx: ReleaseContext) -> None:
    """Re-run the recorded failed CD release run, and only it (spec §4.5, #5153).

    A no-op unless the state block records a ``failed_release_run``, which
    only a run that confirm-main failed on writes. The recorded run must be
    for main's tip, the release merge. The release PR is merged by now, so
    its CI run is never re-run: its gates were judged before the merge, and
    a re-run after it fails the version check. The recovery only checks that
    the release PR's head has a CI attempt that passed, naming it in the log
    (:func:`_check_release_ci`). The CD run is then re-run and watched, and
    confirm-main judges the new attempt through the unchanged gate, the
    release job's CI evidence gate included.
    """
    if ctx.state is None or ctx.state.failed_release_run is None:
        return
    run_id = ctx.state.failed_release_run
    head_sha = _cd_head_sha(ctx, "main")
    run_head = retry.retry_known_resource(
        lambda: github.read_output(
            "run", "view", "--repo", ctx.repo, run_id, "--json", "headSha", "--jq", ".headSha"
        ),
        resource=_run_resource(run_id),
    )
    if run_head != head_sha:
        raise ReleaseError(
            phase="confirm-main",
            command=f"gh run view {run_id} --json headSha",
            message=(
                f"The recorded failed release run {run_id} is for commit {run_head[:12]}, "
                f"not main's tip {head_sha[:12]}; refusing to re-run it. Inspect the "
                "tracking issue and main's CD runs before resuming."
            ),
        )
    _check_release_ci(ctx)
    print(f"Re-running the failed CD release run {run_id} on main...")
    rerun_and_watch(ctx, run_id, check_status=False, phase="confirm-main")


def _check_release_ci(ctx: ReleaseContext) -> ci_evidence.PassedAttempt:
    """Refuse unless the release PR's head has a CI attempt that passed; log which one.

    The release PR is merged by the time confirm-main runs, so its CI is
    never re-run: its gates were judged before the merge, and its version
    check fails after it, with head and main at the same version (#5153).
    The newest attempt that passed counts, even when a later re-run of the
    same run did not (:func:`ci_evidence.select_passed_attempt`), and the log
    names it. A release PR that is not recorded, or no attempt that passed,
    is a :class:`ReleaseError` naming it, raised before CD is touched.
    """
    if ctx.release_pr_url is None:
        raise ReleaseError(
            phase="confirm-main",
            command="gh run list --workflow CI",
            message=(
                "The release PR is not recorded, so its CI run cannot be checked before "
                "the failed CD release run is re-run."
            ),
        )
    head_sha: str | None = None
    try:
        head_sha = github.head_sha(ctx.release_pr_url)
        passed = ci_evidence.select_passed_attempt(ctx.repo, head_sha)
    except (subprocess.CalledProcessError, ValueError, ci_evidence.NoQualifyingRunError) as exc:
        where = (
            f"gh api repos/{ctx.repo}/actions/runs?head_sha={head_sha} (and each run's attempts)"
            if head_sha
            else f"gh pr view {ctx.release_pr_url} --json headRefOid"
        )
        raise ReleaseError(
            phase="confirm-main",
            command=where,
            message=f"The release PR's CI run could not be looked up: {exc}",
        ) from exc
    named = f"The release PR's CI run {passed.run_id} passed at attempt {passed.attempt} ({passed.url})"
    if passed.is_latest:
        print(f"{named}; it is not re-run, as its gates were judged before the merge.")
    else:
        print(
            f"{named}; its latest attempt, {passed.latest_attempt}, did not pass. "
            f"Using attempt {passed.attempt}, the newest that passed. The CI run is not "
            "re-run: a re-run after the merge fails its version check."
        )
    return passed


def _unsettled_jobs(jobs: list[dict[str, Any]]) -> list[str]:
    """Full names of jobs whose ``status`` is not yet ``completed``."""
    return [job.get("name", "") for job in jobs if job.get("status") != "completed"]


def _verify_artifacts(ctx: ReleaseContext) -> None:
    git.run("fetch", "--tags", "--force", "origin")

    tag = f"v{ctx.version}"
    if not git.ref_exists(tag):
        raise ReleaseError(
            phase="confirm-main",
            command=f"git rev-parse {tag}",
            message=f"Tag {tag} does not exist after publish.",
        )
    ctx.tag = tag
    # Recorded in the state block, so a resume can check the tag has not moved.
    ctx.tag_commit = git.read_output("rev-parse", f"{tag}^{{commit}}")

    # The release job that publishes this release just concluded success
    # (_verify_release_job) and its tag is on origin, so it exists.
    release_url = retry.retry_known_resource(
        lambda: github.read_output(
            "release",
            "view",
            "--repo",
            ctx.repo,
            tag,
            "--json",
            "url",
            "--jq",
            ".url",
        ),
        resource=f"GitHub Release {tag}",
    )
    ctx.release_url = release_url
    print(f"  GitHub Release: {release_url}")
