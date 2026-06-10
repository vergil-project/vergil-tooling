"""PR create / merge / worktree-cleanup helpers for vrg-update-deps."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, pr_merge
from vergil_tooling.lib.managed_worktree import remove_worktree
from vergil_tooling.lib.release.subprocess import wait_for_checks
from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

_TITLE = "chore(deps): dependency update sweep"


def build_pr_body(ctx: UpdateDepsContext) -> str:
    """Build a PR body listing each updater that changed something."""
    lines = ["Mechanized dependency update (`vrg-update-deps`).", "", "## Updated", ""]
    for result in ctx.results:
        if result.changed:
            lines.append(f"- **{result.updater}** — {result.summary}")
    warnings = [w for r in ctx.results for w in r.warnings]
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {w}" for w in warnings)
    lines.extend(["", "Ref #1379"])
    return "\n".join(lines) + "\n"


def prepare_pr(ctx: UpdateDepsContext) -> None:
    """Push the worktree branch and open the PR to develop."""
    if ctx.branch is None:
        raise UpdateDepsError(
            phase="prepare-pr",
            command="prepare_pr",
            message="No branch on context — preflight did not run.",
        )
    git.run("push", "-u", "origin", ctx.branch)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(build_pr_body(ctx))
        body_path = Path(handle.name)
    try:
        ctx.pr_url = github.create_pr(base="develop", title=_TITLE, body_file=str(body_path))
    finally:
        body_path.unlink(missing_ok=True)
    print(f"Opened PR: {ctx.pr_url}")


def merge_pr(ctx: UpdateDepsContext) -> None:
    """Wait for checks and merge the dependency-update PR."""
    if ctx.pr_url is None:
        raise UpdateDepsError(
            phase="merge",
            command="merge_pr",
            message="No PR URL on context — prepare-pr did not run.",
        )
    try:
        pr_merge.wait_and_merge(ctx.pr_url, strategy="merge", wait_checks=wait_for_checks)
    except pr_merge.MergeAbortError as exc:
        raise UpdateDepsError(
            phase="merge",
            command="pr_merge.wait_and_merge",
            message=str(exc),
        ) from exc


def cleanup_worktree(ctx: UpdateDepsContext) -> None:
    """Return to the root checkout and remove the managed worktree + branch."""
    if ctx.worktree_path is None:
        return
    os.chdir(ctx.repo_root)
    remove_worktree(ctx.worktree_path)
    if ctx.branch is not None:
        git.run("branch", "-D", ctx.branch)
