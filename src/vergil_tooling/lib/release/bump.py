"""Phase 4: Back-merge main to develop with version bump."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, version
from vergil_tooling.lib.release.merge import known_pr_state, wait_and_merge

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def back_merge_and_bump(ctx: ReleaseContext) -> None:
    """Create back-merge branch from main, bump version, PR to develop.

    Runs inside the release's managed worktree (#1578): ``checkout -b``
    moves the worktree's own HEAD, never the root checkout's. The root's
    ``develop`` is fast-forwarded to the back-merge later, by the
    finalize cleanup stage, from the root checkout.

    Resume-safe (#1612, #3328): if the back-merge PR already exists it is
    adopted — merged when still open, skipped when already merged — and
    ``bump_pr_url`` / ``next_version`` are hydrated rather than re-creating
    the branch. A closed, unmerged PR is not adopted.
    """
    branch = f"release/post-{ctx.version}"

    existing = github.pr_for_branch(branch)
    if existing is not None:
        ctx.bump_pr_url = str(existing["url"])
        if known_pr_state(ctx.bump_pr_url) == "MERGED":
            print(f"Back-merge PR already merged: {ctx.bump_pr_url}")
        else:
            print(f"Back-merge PR already open — merging: {ctx.bump_pr_url}")
            wait_and_merge(ctx.bump_pr_url, phase="back-merge-bump")
        ctx.next_version = _post_branch_version(ctx, branch)
        return

    # Resume-safe against an already-merged back-merge PR (#3328), the bump
    # counterpart of prepare's #2998. `pr_for_branch` returns open PRs only, so
    # a failure after the back-merge merged (e.g. a confirm-develop failure)
    # leaves the merged PR invisible to the check above, and `checkout -b`
    # below fails on the existing branch. Adopt it instead. A merged PR's head
    # branch may already be deleted, so the bumped version is read from the
    # PR's own pull ref, which GitHub keeps.
    merged = github.closed_pr_for_branch(branch)
    if merged is not None and known_pr_state(str(merged["url"])) == "MERGED":
        ctx.bump_pr_url = str(merged["url"])
        print(f"Back-merge PR already merged: {ctx.bump_pr_url}")
        ctx.next_version = _merged_pr_version(ctx, str(merged["number"]))
        return

    print("Fetching main...")
    git.run("fetch", "--tags", "--force", "origin", "main")

    print(f"Creating branch: {branch}")
    git.run("checkout", "-b", branch, "origin/main")

    next_ver = version.bump(ctx.work_root)
    print(f"Bumped version to {next_ver}")
    git.run("add", "-A")
    git.run("commit", "-m", f"chore(release): bump version to {next_ver}")

    git.run("push", "-u", "origin", branch)

    pr_url = _create_bump_pr(ctx, next_ver)
    print(f"Back-merge PR created: {pr_url}")

    wait_and_merge(pr_url, phase="back-merge-bump")

    ctx.bump_pr_url = pr_url
    ctx.next_version = next_ver


def _post_branch_version(ctx: ReleaseContext, branch: str) -> str:
    """The version the back-merge bumped to — read from the post branch's VERSION."""
    git.run("fetch", "origin", branch)
    return version.show(ctx.repo_root, ref="FETCH_HEAD")


def _merged_pr_version(ctx: ReleaseContext, number: str) -> str:
    """The version a merged back-merge PR bumped to — read from its pull ref.

    ``pull/<N>/head`` outlives the head branch, which finalize cleanup (or
    GitHub's auto-delete) may already have removed.
    """
    git.run("fetch", "origin", f"pull/{number}/head")
    return version.show(ctx.repo_root, ref="FETCH_HEAD")


def _create_bump_pr(ctx: ReleaseContext, next_ver: str) -> str:
    title = f"chore(release): back-merge {ctx.version} and bump to {next_ver}"
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
