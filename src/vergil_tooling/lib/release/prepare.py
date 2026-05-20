"""Phase 1: Prepare release — tracking issue, branch, changelog, PR."""

from __future__ import annotations

import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import create_tracking_issue

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

RELEASE_NOTES_DIR = "releases"


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

    print("Merging main into release branch...")
    git.run("fetch", "--tags", "--force", "origin", "main")
    git.run(
        "merge",
        "origin/main",
        "-X",
        "ours",
        "-m",
        f"chore(release): merge main into {branch}",
    )

    _generate_changelog(ctx)

    print(f"Pushing branch: {branch}")
    git.run("push", "-u", "origin", branch)

    pr_url = _create_pr(ctx)

    git.run("checkout", "develop")

    ctx.release_branch = branch
    ctx.release_pr_url = pr_url
    print(f"Release PR created: {pr_url}")


def _generate_changelog(ctx: ReleaseContext) -> None:
    tag = f"develop-v{ctx.version}"
    print(f"Generating changelog with boundary tag: {tag}")
    config_path = files("vergil_tooling.configs") / "cliff.toml"
    subprocess.run(  # noqa: S603
        ("git-cliff", "--config", str(config_path), "--tag", tag, "-o", "CHANGELOG.md"),  # noqa: S607
        check=True,
    )
    _normalize_trailing_newline(Path("CHANGELOG.md"))
    git.run("add", "CHANGELOG.md")

    releases_dir = Path(RELEASE_NOTES_DIR)
    releases_dir.mkdir(exist_ok=True)
    output_file = releases_dir / f"v{ctx.version}.md"
    print(f"Generating release notes: {output_file}")
    release_notes_config = files("vergil_tooling.configs") / "cliff-release-notes.toml"
    subprocess.run(  # noqa: S603
        (  # noqa: S607
            "git-cliff",
            "--config",
            str(release_notes_config),
            "--tag",
            tag,
            "--unreleased",
            "-o",
            str(output_file),
        ),
        check=True,
    )
    _normalize_trailing_newline(output_file)
    git.run("add", str(releases_dir))

    status = git.read_output("status", "--porcelain")
    if not status:
        raise ReleaseError(
            phase="prepare",
            command="git-cliff",
            message=(
                f"No publishable changes since the last release. "
                f"All commits after develop-v{ctx.version} are filtered by git-cliff."
            ),
        )
    git.run("commit", "-m", f"chore(release): prepare {ctx.version}")


def _normalize_trailing_newline(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")


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
