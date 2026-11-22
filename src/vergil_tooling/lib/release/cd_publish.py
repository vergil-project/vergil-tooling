"""The ``cd-publish`` stage: deliver the CD publish jobs confirm recorded as failed (spec §4.5).

``confirm-main`` and ``confirm-develop`` record every CD publish job that did
not succeed in the tracking issue's state block (``failed_jobs``), as its
branch, run ID and full job name. The release itself is valid; the jobs are
deferred. A fresh run's ``cd-publish`` fails, deferred, naming them, and the
run carries on. On ``vrg-release --resume`` it delivers them:

- **main:** each recorded run is re-run with ``gh run rerun <id> --failed``,
  watched, and its jobs judged again with ``confirm-main``'s rules (the
  release job must still have succeeded).
- **develop:** a recorded job is delivered when a later ``develop`` CD run's
  same job has succeeded, so newer develop docs are never overwritten by an
  older run; only a job no later run delivered is re-run, by itself
  (``gh run rerun <id> --job <job-id>``).

Either way, a recorded job whose run has succeeded since (a re-run by hand)
is delivered without a re-run. Each delivered job leaves ``failed_jobs`` as
soon as its run is judged, so a second resume re-runs only what is still
recorded; the stage fails while any job remains (epic
vergil-project/.github#1440).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import github, progress, retry
from vergil_tooling.lib.cd_runs import is_release_job
from vergil_tooling.lib.release.confirm import (
    _CD_WORKFLOW,
    _current_state,
    _fetch_run_jobs,
    _run_resource,
    _settled_run_jobs,
    _verify_release_job,
    judge_publish_jobs,
    record_failed_jobs,
    rerun_and_watch,
)
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.state import FailedJob

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vergil_tooling.lib.release.context import ReleaseContext

PHASE = "cd-publish"

# How many of develop's newest CD runs are read for a later run that
# delivered a recorded job. One that does not reach back to the recorded run
# is refused rather than read as "no later run" (spec §6).
_LATER_RUNS_LIMIT = 50


def deliver_failed_jobs(ctx: ReleaseContext) -> None:
    """Deliver the CD publish jobs the state block records as failed.

    A no-op when none is recorded. A fresh run fails, deferred, naming them;
    a resume delivers them as the module docstring says, and fails naming
    every job still not delivered.
    """
    current = _current_state(ctx, PHASE)
    if not current.failed_jobs:
        print("No failed CD publish jobs to deliver.")
        progress.note("nothing to deliver")
        return
    if not ctx.resume:
        raise ReleaseError(
            phase=PHASE,
            command=PHASE,
            message=(
                f"CD publish job(s) did not succeed: {_describe(current.failed_jobs)}. "
                "The release is valid; they are recorded on the tracking issue. Once the "
                f"cause is fixed, `vrg-release --resume {ctx.version}` re-runs them."
            ),
        )
    for branch, run_id in dict.fromkeys((job.branch, job.run_id) for job in current.failed_jobs):
        _check_recorded_run(ctx, branch, run_id)
        names = [
            job.job
            for job in _current_state(ctx, PHASE).failed_jobs
            if job.branch == branch and job.run_id == run_id
        ]
        if branch == "main":
            still = _deliver_main(ctx, run_id, names)
        else:
            still = _deliver_develop(ctx, run_id, names)
        for name in names:
            if name not in still:
                print(f"  Delivered: {name} (CD run {run_id} on {branch})")
        record_failed_jobs(
            ctx,
            branch,
            [FailedJob(branch=branch, run_id=run_id, job=name) for name in still],
            replaces=_on_run(branch, run_id),
            phase=PHASE,
        )
    remaining = _current_state(ctx, PHASE).failed_jobs
    if remaining:
        raise ReleaseError(
            phase=PHASE,
            command=PHASE,
            message=(
                f"CD publish job(s) still not delivered: {_describe(remaining)}. They stay "
                "recorded on the tracking issue. Once the cause is fixed, "
                f"`vrg-release --resume {ctx.version}` re-runs only these."
            ),
        )
    print("Every failed CD publish job is delivered.")


def _on_run(branch: str, run_id: str) -> Callable[[FailedJob], bool]:
    """Whether a recorded failed job is one of *branch*'s CD run *run_id*."""

    def matches(job: FailedJob) -> bool:
        return job.branch == branch and job.run_id == run_id

    return matches


def _describe(jobs: Sequence[FailedJob]) -> str:
    return ", ".join(f"{job.job} (CD run {job.run_id} on {job.branch})" for job in jobs)


def _check_recorded_run(ctx: ReleaseContext, branch: str, run_id: str) -> None:
    """Refuse unless *run_id* is the CD run the state records for *branch*.

    ``verify_facts`` has checked that run's head against the release, so a
    failed job recorded on any other run belongs to no fact a resume trusts.
    """
    current = _current_state(ctx, PHASE)
    recorded = current.main_cd_run if branch == "main" else current.develop_cd_run
    if recorded != run_id:
        raise ReleaseError(
            phase=PHASE,
            command=PHASE,
            message=(
                f"A failed CD publish job is recorded on run {run_id} on {branch}, but the "
                f"state records {recorded or 'no run'} as the release's {branch} CD run; "
                "refusing to re-run it. Inspect the tracking issue before resuming."
            ),
        )


def _still_failed(ctx: ReleaseContext, run_id: str, names: Sequence[str]) -> list[str]:
    """Those of *names* that run *run_id*'s latest attempt has not delivered.

    A job is delivered when :func:`judge_publish_jobs` does not count it
    failed. One no longer in the run is not delivered.
    """
    jobs = _settled_run_jobs(ctx, run_id, ())
    failed, _stale = judge_publish_jobs(ctx, run_id, jobs)
    present = {job.get("name", "") for job in jobs}
    return [name for name in names if name in failed or name not in present]


def _deliver_main(ctx: ReleaseContext, run_id: str, names: Sequence[str]) -> list[str]:
    """Re-run main CD run *run_id*'s failed jobs and judge it again; the jobs still failed."""
    still = _still_failed(ctx, run_id, names)
    if not still:
        return []
    print(f"Re-running the failed jobs of CD run {run_id} on main: {', '.join(still)}")
    rerun_and_watch(ctx, run_id, rerun_args=("--failed",), check_status=False, phase=PHASE)
    jobs = _settled_run_jobs(ctx, run_id, (is_release_job,))
    # confirm-main's hard gate: the re-run must leave the release job succeeded.
    _verify_release_job(jobs)
    return [name for name in still if name in _still_failed(ctx, run_id, still)]


def _deliver_develop(ctx: ReleaseContext, run_id: str, names: Sequence[str]) -> list[str]:
    """Deliver develop CD run *run_id*'s failed jobs; the jobs still failed.

    A job a later develop CD run delivered is not re-run; every other job is
    re-run by itself, so no job a later run delivered is published again
    from this older run.
    """
    still = _still_failed(ctx, run_id, names)
    if not still:
        return []
    later = _delivered_later(ctx, run_id, still)
    for name in later:
        print(f"  {name}: delivered by later develop CD run {later[name]}; not re-run.")
    for name in [name for name in still if name not in later]:
        job_id = _job_id(ctx, run_id, name)
        print(f"Re-running {name} in CD run {run_id} on develop...")
        rerun_and_watch(ctx, run_id, rerun_args=("--job", job_id), check_status=False, phase=PHASE)
    return [
        name for name in still if name not in later and name in _still_failed(ctx, run_id, [name])
    ]


def _job_id(ctx: ReleaseContext, run_id: str, name: str) -> str:
    """The ID of job *name* in run *run_id*'s latest attempt."""
    for job in _fetch_run_jobs(ctx, run_id):
        if job.get("name") == name and job.get("databaseId") is not None:
            return str(job["databaseId"])
    raise ReleaseError(
        phase=PHASE,
        command=f"gh run view {run_id} --json jobs",
        message=f"Job {name!r} is not in CD run {run_id}, so it cannot be re-run.",
    )


def _created_at(ctx: ReleaseContext, run_id: str) -> datetime:
    out = retry.retry_known_resource(
        lambda: github.read_output(
            "run", "view", "--repo", ctx.repo, run_id, "--json", "createdAt", "--jq", ".createdAt"
        ),
        resource=_run_resource(run_id),
    )
    return datetime.fromisoformat(out)


def _delivered_later(ctx: ReleaseContext, run_id: str, names: Sequence[str]) -> dict[str, str]:
    """Each of *names* that a develop CD run created after *run_id* ran with success, by that run.

    Develop's newest CD runs are read back to *run_id*. A listing that does
    not reach back to it, while a job is still undelivered, is a
    :class:`ReleaseError`: an older run is never re-run on a guess that no
    newer one exists.
    """
    created = _created_at(ctx, run_id)
    raw = github.read_output(
        "run",
        "list",
        "--repo",
        ctx.repo,
        "--workflow",
        _CD_WORKFLOW,
        "--branch",
        "develop",
        "--limit",
        str(_LATER_RUNS_LIMIT),
        "--json",
        "databaseId,createdAt,status",
    )
    runs: list[dict[str, Any]] = json.loads(raw or "[]")
    delivered: dict[str, str] = {}
    reached = False
    for run in runs:
        other = str(run.get("databaseId"))
        if other == run_id or datetime.fromisoformat(str(run["createdAt"])) <= created:
            reached = True
            break
        if run.get("status") != "completed":
            continue
        for job in _fetch_run_jobs(ctx, other):
            name = job.get("name", "")
            if name in names and name not in delivered and _succeeded(job):
                delivered[name] = other
    if not reached and any(name not in delivered for name in names):
        raise ReleaseError(
            phase=PHASE,
            command=f"gh run list --workflow {_CD_WORKFLOW} --branch develop",
            message=(
                f"develop's {_LATER_RUNS_LIMIT} newest CD runs do not reach back to run "
                f"{run_id}, so whether a later run delivered "
                f"{', '.join(n for n in names if n not in delivered)} cannot be told; "
                "refusing to re-run the older run. Re-run develop's CD by hand, then resume."
            ),
        )
    return delivered


def _succeeded(job: dict[str, Any]) -> bool:
    return job.get("status") == "completed" and job.get("conclusion") == "success"
