"""Phase 1: Prepare release — tracking issue, branch, changelog, PR."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import changelog, git, github, version
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import create_tracking_issue

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def prepare(ctx: ReleaseContext) -> None:
    """Create tracking issue, release branch, changelog, and PR to main."""
    create_tracking_issue(ctx)
    print(f"Tracking issue created: {ctx.issue_url}")

    branch = f"release/{ctx.version}"

    if git.ref_exists(branch) or git.ref_exists(f"origin/{branch}"):
        raise ReleaseError(
            phase="prepare",
            command=f"git rev-parse {branch}",
            message=f"Release branch '{branch}' already exists.",
        )

    print(f"Creating branch: {branch}")
    git.run("checkout", "-b", branch)

    if ctx.version_override is not None:
        print(f"Applying version override: {ctx.version_override}")
        version.bump(ctx.repo_root, ctx.version_override)
        git.run("add", "-A")
        git.run("commit", "-m", f"chore(release): bump version to {ctx.version}")

    _generate_changelog(ctx)

    print(f"Pushing branch: {branch}")
    git.run("push", "-u", "origin", branch)

    pr_url = _create_pr(ctx)

    git.run("checkout", "develop")

    ctx.release_branch = branch
    ctx.release_pr_url = pr_url
    print(f"Release PR created: {pr_url}")


def _generate_changelog(ctx: ReleaseContext) -> None:
    print(f"Generating changelog for v{ctx.version}")
    changelog.generate_changelog(ctx.repo_root, ctx.version)
    git.run("add", "CHANGELOG.md")

    notes_path = changelog.generate_release_notes(ctx.repo_root, ctx.version)
    git.run("add", str(notes_path))

    status = git.read_output("status", "--porcelain")
    if not status:
        raise ReleaseError(
            phase="prepare",
            command="git-cliff",
            message=(
                f"No publishable changes since the last release. "
                f"All commits after develop-v{ctx.version} are filtered "
                f"by git-cliff."
            ),
        )
    git.run("commit", "-m", f"chore(release): prepare {ctx.version}")


def _create_pr(ctx: ReleaseContext) -> str:
    title = f"release: {ctx.version}"
    body = (
        f"## Summary\n\nRelease {ctx.version}\n\n"
        f"Ref #{ctx.issue_number}\n\n"
        f"Generated with `vrg-release`\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        return github.create_pr(base="main", title=title, body_file=tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
