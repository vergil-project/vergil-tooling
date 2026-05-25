"""Phase 3/5: Verify CD workflow and publish artifacts."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import watch_workflow

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

_CD_WORKFLOW = "cd.yml"
_MAIN_EXPECTED_JOBS = ("docs", "release")
_DEVELOP_EXPECTED_JOBS = ("docs",)
_CD_POLL_INTERVAL = 10
_CD_POLL_ATTEMPTS = 30


def confirm_main(ctx: ReleaseContext) -> None:
    """Watch CD on main and verify publish artifacts."""
    run_id, run_url = _watch_cd(ctx, branch="main")
    _verify_jobs(ctx, run_id, _MAIN_EXPECTED_JOBS, phase="confirm-main")

    ctx.cd_run_id = run_id
    ctx.cd_run_url = run_url

    _verify_artifacts(ctx)
    print(f"All artifacts confirmed for v{ctx.version}.")


def confirm_develop(ctx: ReleaseContext) -> None:
    """Watch CD on develop after back-merge."""
    run_id, run_url = _watch_cd(ctx, branch="develop")
    _verify_jobs(ctx, run_id, _DEVELOP_EXPECTED_JOBS, phase="confirm-develop")

    ctx.develop_cd_run_id = run_id
    ctx.develop_cd_run_url = run_url
    print("Develop CD verified.")


def _watch_cd(ctx: ReleaseContext, *, branch: str) -> tuple[str, str]:
    print(f"Waiting for {_CD_WORKFLOW} on {branch}...")
    git.run("fetch", "origin", branch)
    head_sha = git.read_output("rev-parse", f"origin/{branch}")

    run_id = _poll_for_run(ctx.repo, branch, head_sha)

    watch_workflow(ctx.repo, run_id, verbose=ctx.verbose)

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

    print(f"  CD workflow succeeded: {run_url}")
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


def _verify_jobs(
    ctx: ReleaseContext,
    run_id: str,
    expected: tuple[str, ...],
    *,
    phase: str,
) -> None:
    for job_name in expected:
        conclusion = github.read_output(
            "run",
            "view",
            "--repo",
            ctx.repo,
            run_id,
            "--json",
            "jobs",
            "--jq",
            f'.jobs[] | select(.name | contains("{job_name}")) | .conclusion',
        )
        if not conclusion:
            raise ReleaseError(
                phase=phase,
                command=f"verify job '{job_name}'",
                message=(f"Expected job '{job_name}' not found in workflow run {run_id}."),
            )
        if conclusion != "success":
            raise ReleaseError(
                phase=phase,
                command=f"verify job '{job_name}'",
                message=(f"Job '{job_name}' did not succeed (conclusion: '{conclusion}')."),
            )
        print(f"  Job '{job_name}': success")


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
