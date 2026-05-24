"""Phase 4: Back-merge main to develop with version bump."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, version
from vergil_tooling.lib.release.merge import wait_and_merge

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def back_merge_and_bump(ctx: ReleaseContext) -> None:
    """Create back-merge branch from main, bump version, PR to develop."""
    branch = f"release/post-{ctx.version}"

    print("Fetching main...")
    git.run("fetch", "--tags", "--force", "origin", "main")

    print(f"Creating branch: {branch}")
    git.run("checkout", "-b", branch, "origin/main")

    next_ver = version.bump(ctx.repo_root)
    print(f"Bumped version to {next_ver}")
    git.run("add", "-A")
    git.run("commit", "-m", f"chore(release): bump version to {next_ver}")

    git.run("push", "-u", "origin", branch)

    pr_url = _create_bump_pr(ctx, next_ver)
    print(f"Back-merge PR created: {pr_url}")

    wait_and_merge(pr_url, phase="back-merge-bump", verbose=ctx.verbose)

    git.run("checkout", "develop")
    git.run("pull", "origin", "develop")

    ctx.bump_pr_url = pr_url
    ctx.next_version = next_ver


def _create_bump_pr(ctx: ReleaseContext, next_ver: str) -> str:
    title = (
        f"chore(release): back-merge {ctx.version} "
        f"and bump to {next_ver}"
    )
    body = (
        f"## Summary\n\n"
        f"Back-merge main after release {ctx.version} "
        f"and bump to {next_ver}.\n\n"
        f"Ref #{ctx.issue_number}\n\n"
        f"Generated with `vrg-release`\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        return github.create_pr(
            base="develop",
            title=title,
            body_file=tmp_path,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
