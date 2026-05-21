"""Phase 4: Watch workflows and verify publish artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import watch_workflow

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def confirm_publish(ctx: ReleaseContext) -> None:
    """Block on publish + docs workflows, then verify artifacts."""
    cfg = config.read_config(ctx.repo_root)
    docs_workflow = cfg.publish.docs_workflow

    _watch_workflow(ctx, "publish.yml", "publish")
    _watch_workflow(ctx, docs_workflow, "docs")
    _verify_artifacts(ctx)

    print(f"All artifacts confirmed for v{ctx.version}.")


def _watch_workflow(ctx: ReleaseContext, workflow: str, label: str) -> None:
    print(f"Waiting for {workflow} on main...")
    run_id = github.read_output(
        "run",
        "list",
        "--repo",
        ctx.repo,
        "--workflow",
        workflow,
        "--branch",
        "main",
        "--limit",
        "1",
        "--json",
        "databaseId",
        "--jq",
        ".[0].databaseId",
    )
    if not run_id:
        raise ReleaseError(
            phase="confirm-publish",
            command=f"gh run list --workflow {workflow}",
            message=f"No {workflow} run found on main.",
        )

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

    if label == "publish":
        ctx.publish_run_id = run_id
        ctx.publish_run_url = run_url
    else:
        ctx.docs_run_id = run_id
        ctx.docs_run_url = run_url

    print(f"  {workflow} succeeded: {run_url}")


def _verify_artifacts(ctx: ReleaseContext) -> None:
    git.run("fetch", "--tags", "--force", "origin")

    tag = f"v{ctx.version}"
    if not git.ref_exists(tag):
        raise ReleaseError(
            phase="confirm-publish",
            command=f"git rev-parse {tag}",
            message=f"Tag {tag} does not exist after publish.",
        )
    ctx.tag = tag

    develop_tag = f"develop-v{ctx.version}"
    if not git.ref_exists(develop_tag):
        raise ReleaseError(
            phase="confirm-publish",
            command=f"git rev-parse {develop_tag}",
            message=f"Develop boundary tag {develop_tag} does not exist.",
        )
    ctx.develop_tag = develop_tag

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
