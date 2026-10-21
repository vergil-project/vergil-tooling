"""Phase 3/5: Verify CD workflow and publish artifacts.

Every read of the CD run (its URL, the watch, its jobs) and of the GitHub
Release goes through :func:`retry.retry_known_resource`: the run ID was just
returned by ``gh run list`` and the release was just published by the CD
release job, so GitHub has established they exist and a 404 only means "not
readable yet" (#3137).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import git, github, progress, retry
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import watch_workflow

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

_CD_WORKFLOW = "cd.yml"
_CD_POLL_INTERVAL = 10
_CD_POLL_ATTEMPTS = 30
# A reusable-workflow leaf job's conclusion can lag the run-level status by a
# few seconds in the jobs API (issue #1611); poll for it to settle.
_JOB_SETTLE_INTERVAL = 5
_JOB_SETTLE_ATTEMPTS = 12


def confirm_main(ctx: ReleaseContext) -> None:
    """Watch CD on main: hard-verify the release, defer other publish jobs."""
    run_id, run_url = _watch_cd(ctx, branch="main")
    ctx.cd_run_id = run_id
    ctx.cd_run_url = run_url

    jobs = _settled_run_jobs(ctx, run_id, (_is_release_job,))
    _verify_release_job(jobs)

    if not _record_publish_state(
        ctx, run_id, jobs, deferred_label="Publish deferred (release is valid)"
    ):
        print("  All CD jobs succeeded.")

    _verify_artifacts(ctx)
    print(f"Release v{ctx.version} confirmed.")


def confirm_develop(ctx: ReleaseContext) -> None:
    """Watch CD on develop; defer a docs publish failure rather than raise."""
    run_id, run_url = _watch_cd(ctx, branch="develop")
    ctx.develop_cd_run_id = run_id
    ctx.develop_cd_run_url = run_url

    jobs = _settled_run_jobs(ctx, run_id, (_name_contains("docs"),))
    if not _record_publish_state(ctx, run_id, jobs, deferred_label="Publish deferred on develop"):
        print("Develop CD verified.")


def _record_publish_state(
    ctx: ReleaseContext, run_id: str, jobs: list[dict[str, Any]], *, deferred_label: str
) -> bool:
    """Record deferred publish failures and stale job records; True if any.

    A job still non-terminal after the settle budget has no conclusion to judge
    (#3352). ``gh run watch`` already saw the run finish, so when the run itself
    concluded ``success`` GitHub is asserting every job succeeded or skipped: the
    job's record is stale, reported by name as a warning but never as a failed
    publish. Under any other run conclusion it may genuinely have failed, so it
    is deferred as ``<job> (never settled)``.
    """
    deferred = _collect_deferred_publish(jobs)
    stale: list[str] = []
    unsettled = _unsettled_jobs(jobs)
    if unsettled:
        if _run_conclusion(ctx, run_id) == "success":
            stale = unsettled
        else:
            deferred.extend(f"{name} (never settled)" for name in unsettled)

    reasons: list[str] = []
    if deferred:
        ctx.deferred_publish_failures.extend(
            d for d in deferred if d not in ctx.deferred_publish_failures
        )
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
        lambda: watch_workflow(ctx.repo, run_id, check_status=False),
        resource=resource,
    )

    print(f"  CD workflow completed: {run_url}")
    return run_id, run_url


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


def _name_contains(fragment: str) -> JobMatcher:
    """Matcher for a job whose name *contains* ``fragment``.

    Reusable-workflow leaf jobs are surfaced as ``<caller> / <job>`` (e.g.
    ``docs / docs``), so this substring-matches rather than compares exactly.
    """
    return lambda name: fragment in name


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
    release settle uses ``_is_release_job`` (the leaf segment), because
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
_RELEASE_JOB_LEAF = "release"


def _is_release_job(name: str) -> bool:
    """True when *name* is the CD release job, identified by its leaf segment.

    Consumer repos call the reusable ``cd-release`` workflow, so their release
    job surfaces as the reusable leaf ``release / release``. vergil-actions
    *defines* that reusable workflow and cannot call itself, so its CD runs an
    inline job it names ``cd / release`` (#2001). Both share the leaf segment
    ``release`` — match on the leaf (the part after the last `` / ``), not the
    full name, so vergil-actions' own release verifies while a decoy like
    ``release-notes / build`` (leaf ``build``) still fails the #1853 guard.
    Matching the leaf exactly keeps that guard against loose substrings intact.
    """
    return name.rsplit(" / ", 1)[-1] == _RELEASE_JOB_LEAF


def _verify_release_job(jobs: list[dict[str, Any]]) -> None:
    """Hard gate: the release job must exist and have concluded ``success``.

    Identified by its leaf segment ``release`` (see ``_is_release_job``), which
    matches both the reusable leaf ``release / release`` and vergil-actions'
    inline ``cd / release`` while still rejecting a ``release``-prefixed decoy —
    the release job is the single load-bearing assertion. A renamed/absent
    release job fails closed (#1853, #2001).
    """
    for job in jobs:
        if _is_release_job(job.get("name", "")):
            conclusion = job.get("conclusion")
            if conclusion != "success":
                raise ReleaseError(
                    phase="confirm-main",
                    command=f"verify job '{_RELEASE_JOB_NAME}'",
                    message=(f"Release job did not succeed (conclusion: '{conclusion}')."),
                )
            return
    raise ReleaseError(
        phase="confirm-main",
        command=f"verify job '{_RELEASE_JOB_NAME}'",
        message=f"Release job '{_RELEASE_JOB_NAME}' not found in the workflow run.",
    )


def _job_family(name: str) -> str:
    return name.split(" / ", 1)[0]


def _unsettled_jobs(jobs: list[dict[str, Any]]) -> list[str]:
    """Full names of jobs whose ``status`` is not yet ``completed``."""
    return [job.get("name", "") for job in jobs if job.get("status") != "completed"]


def _collect_deferred_publish(jobs: list[dict[str, Any]]) -> list[str]:
    """Ordered-unique families of completed non-release jobs that did not succeed.

    A job "did not succeed" when it completed with a conclusion that is neither
    ``success`` nor ``skipped`` (a skipped job — e.g. codeql — is not a
    failure). A job that has not completed is not judged here — see
    ``_unsettled_jobs`` (#3352). Reusable leaves are ``<family> / <job>``;
    collapse to the family so a matrix of failed ``docker-publish`` leaves
    reports once as ``docker-publish`` — except when the family is the release
    job's own (``release / package-sign``), where the bare family would read as
    the release itself failing, so the full job name is reported.
    """
    release_families = {_job_family(n) for j in jobs if _is_release_job(n := j.get("name", ""))}
    failed: list[str] = []
    for job in jobs:
        name = job.get("name", "")
        if _is_release_job(name) or job.get("status") != "completed":
            continue
        if job.get("conclusion") in ("success", "skipped"):
            continue
        family = _job_family(name)
        label = name if family in release_families else family
        if label and label not in failed:
            failed.append(label)
    return failed


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

    develop_tag = f"develop-v{ctx.version}"
    if not git.ref_exists(develop_tag):
        raise ReleaseError(
            phase="confirm-main",
            command=f"git rev-parse {develop_tag}",
            message=f"Develop boundary tag {develop_tag} does not exist.",
        )
    ctx.develop_tag = develop_tag
