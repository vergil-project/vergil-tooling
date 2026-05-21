"""Phase 4: Watch CD workflow and verify publish artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import watch_workflow

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

_CD_WORKFLOW = "cd.yml"


def confirm_publish(ctx: ReleaseContext) -> None:
    """Watch the CD workflow on main and verify publish artifacts."""
    cfg = config.read_config(ctx.repo_root)

    if cfg.publish.release or cfg.publish.docs:
        _watch_cd(ctx)

    _verify_artifacts(ctx, release=cfg.publish.release)

    print(f"All artifacts confirmed for v{ctx.version}.")


def _watch_cd(ctx: ReleaseContext) -> None:
    print(f"Waiting for {_CD_WORKFLOW} on main...")
    run_id = github.read_output(
        "run",
        "list",
        "--repo",
        ctx.repo,
        "--workflow",
        _CD_WORKFLOW,
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
            command=f"gh run list --workflow {_CD_WORKFLOW}",
            message="No CD workflow run found on main.",
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

    ctx.cd_run_id = run_id
    ctx.cd_run_url = run_url
    print(f"  CD workflow succeeded: {run_url}")


def _verify_artifacts(ctx: ReleaseContext, *, release: bool) -> None:
    git.run("fetch", "--tags", "--force", "origin")

    if release:
        tag = f"v{ctx.version}"
        if not git.ref_exists(tag):
            raise ReleaseError(
                phase="confirm-publish",
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
            phase="confirm-publish",
            command=f"git rev-parse {develop_tag}",
            message=f"Develop boundary tag {develop_tag} does not exist.",
        )
    ctx.develop_tag = develop_tag
