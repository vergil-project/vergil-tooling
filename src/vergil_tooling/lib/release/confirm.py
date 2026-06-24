"""Phase 3/5: Verify CD workflow and publish artifacts."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib import git, github
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

    jobs = _settled_run_jobs(ctx, run_id, ("release",))
    _verify_release_job(jobs)

    deferred = _collect_deferred_publish(jobs)
    if deferred:
        ctx.deferred_publish_failures.extend(
            d for d in deferred if d not in ctx.deferred_publish_failures
        )
        print(f"  Publish deferred (release is valid): {', '.join(deferred)}")
    else:
        print("  All CD jobs succeeded.")

    _verify_artifacts(ctx)
    print(f"Release v{ctx.version} confirmed.")


def confirm_develop(ctx: ReleaseContext) -> None:
    """Watch CD on develop; defer a docs publish failure rather than raise."""
    run_id, run_url = _watch_cd(ctx, branch="develop")
    ctx.develop_cd_run_id = run_id
    ctx.develop_cd_run_url = run_url

    jobs = _settled_run_jobs(ctx, run_id, ("docs",))
    deferred = _collect_deferred_publish(jobs)
    if deferred:
        ctx.deferred_publish_failures.extend(
            d for d in deferred if d not in ctx.deferred_publish_failures
        )
        print(f"  Publish deferred on develop: {', '.join(deferred)}")
    else:
        print("Develop CD verified.")


def _watch_cd(
    ctx: ReleaseContext,
    *,
    branch: str,
) -> tuple[str, str]:
    print(f"Waiting for {_CD_WORKFLOW} on {branch}...")
    git.run("fetch", "origin", branch)
    head_sha = git.read_output("rev-parse", f"origin/{branch}")

    run_id = _poll_for_run(ctx.repo, branch, head_sha)

    run_url = github.read_output(
        "run",
        "view",
        "--repo",
        ctx.repo,
        run_id,
        "--json",
        "url",
        "--jq",
        ".url",
    )
    print(f"  Workflow run: {run_url}")

    watch_workflow(ctx.repo, run_id, check_status=False)

    print(f"  CD workflow completed: {run_url}")
    return run_id, run_url


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


def _fetch_run_jobs(ctx: ReleaseContext, run_id: str) -> list[dict[str, Any]]:
    """Return the ``jobs`` array for *run_id* from the GitHub API."""
    out = github.read_output("run", "view", "--repo", ctx.repo, run_id, "--json", "jobs")
    data = json.loads(out) if out.strip() else {}
    jobs: list[dict[str, Any]] = data.get("jobs", [])
    return jobs


def _find_job(jobs: list[dict[str, Any]], job_name: str) -> dict[str, Any] | None:
    """First job whose name *contains* ``job_name``.

    Reusable-workflow leaf jobs are surfaced as ``<caller> / <job>`` (e.g.
    ``docs / docs``), so we substring-match rather than compare exactly.
    """
    for job in jobs:
        if job_name in job.get("name", ""):
            return job
    return None


def _settled_run_jobs(
    ctx: ReleaseContext, run_id: str, expected: tuple[str, ...]
) -> list[dict[str, Any]]:
    """Poll until every *expected* job is present and ``completed``.

    ``gh run watch`` returns when the run-level status is terminal, but a
    reusable-workflow leaf job's ``conclusion`` can still be ``null`` in the
    jobs API for a few seconds (issue #1611). Reading once raced that window
    and aborted an already-succeeded release. Polling for ``completed`` closes
    the race; a genuinely absent job never settles and the final snapshot lets
    the caller report it as not found.
    """
    jobs: list[dict[str, Any]] = []
    for attempt in range(_JOB_SETTLE_ATTEMPTS):
        jobs = _fetch_run_jobs(ctx, run_id)
        if all(
            (job := _find_job(jobs, name)) is not None and job.get("status") == "completed"
            for name in expected
        ):
            return jobs
        if attempt < _JOB_SETTLE_ATTEMPTS - 1:
            time.sleep(_JOB_SETTLE_INTERVAL)
    return jobs


_RELEASE_JOB_NAME = "release / release"


def _verify_release_job(jobs: list[dict[str, Any]]) -> None:
    """Hard gate: the release job must exist and have concluded ``success``.

    Matched EXACTLY (the reusable-workflow leaf ``release / release``), not by
    the substring ``_find_job`` uses for the deferred sweep — the release job is
    the single load-bearing assertion, so a future ``release``-prefixed job must
    not satisfy it. A renamed/absent release job fails closed (#1853).
    """
    for job in jobs:
        if job.get("name") == _RELEASE_JOB_NAME:
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


def _collect_deferred_publish(jobs: list[dict[str, Any]]) -> list[str]:
    """Ordered-unique families of non-release jobs that did not succeed.

    A job "did not succeed" when its conclusion is neither ``success`` nor
    ``skipped`` (a skipped job — e.g. codeql — is not a failure). Reusable
    leaves are ``<family> / <job>``; collapse to the family so a matrix of
    failed ``docker-publish`` leaves reports once as ``docker-publish``.
    """
    families: list[str] = []
    for job in jobs:
        name = job.get("name", "")
        if name == _RELEASE_JOB_NAME:
            continue
        if job.get("conclusion") in ("success", "skipped"):
            continue
        family = name.split(" / ", 1)[0]
        if family and family not in families:
            families.append(family)
    return families


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

    release_url = github.read_output(
        "release",
        "view",
        "--repo",
        ctx.repo,
        tag,
        "--json",
        "url",
        "--jq",
        ".url",
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
