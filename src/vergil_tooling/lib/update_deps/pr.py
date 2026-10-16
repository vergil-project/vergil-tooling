"""PR, fix-task and worktree helpers for vrg-update-deps."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, pr_merge, progress
from vergil_tooling.lib.labels import load_labels
from vergil_tooling.lib.managed_worktree import remove_worktree
from vergil_tooling.lib.update_deps import report
from vergil_tooling.lib.update_deps.context import DEPS_LABEL, DepsFailure, UpdateDepsError
from vergil_tooling.lib.update_deps.failure import failure_markdown, tail_output

if TYPE_CHECKING:
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

# The release gate parses this line out of the PR body (spec §5.3).
FIX_TASK_PREFIX = "Fix task: "
# vrg-issue-create prints "Created <url>, linked under epic <slug>." on success.
_CREATED_RE = re.compile(r"Created (https://github\.com/\S+/issues/\d+),")


def fix_task_title(version: str) -> str:
    """The fix task's exact title (spec §5.2)."""
    return f"Fix failed dependency update for {version}"


def _pr_title(ctx: UpdateDepsContext) -> str:
    return f"chore(deps): dependency update for {ctx.version}"


def build_pr_body(ctx: UpdateDepsContext) -> str:
    """Build the PR body: updates, warnings, held-back report, and any failure."""
    lines = ["Mechanized dependency update (`vrg-update-deps`).", "", "## Updated", ""]
    lines.extend(f"- **{r.updater}** — {r.summary}" for r in ctx.results if r.changed)
    warnings = [w for r in ctx.results for w in r.warnings]
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {w}" for w in warnings)
    lines.extend(
        [
            "",
            "## Held back by declared bounds",
            "",
            report.render_markdown(report.collect(ctx.results)),
        ]
    )
    if ctx.failure is not None:
        lines.extend(["", failure_markdown(ctx.failure)])
    if ctx.fix_issue_url is not None:
        lines.extend(["", f"{FIX_TASK_PREFIX}{ctx.fix_issue_url}"])
    return "\n".join(lines) + "\n"


def _ensure_label(repo: str) -> None:
    """Create the dependency-update label idempotently, as the registry defines it."""
    spec = {entry["name"]: entry for entry in load_labels()["labels"]}[DEPS_LABEL]
    progress.run(
        [
            "vrg-ensure-label",
            "--repo",
            repo,
            "--label",
            DEPS_LABEL,
            "--color",
            spec["color"],
            "--description",
            spec["description"],
        ]
    )


def prepare_pr(ctx: UpdateDepsContext) -> None:
    """Push the worktree branch and open the labelled PR to develop."""
    if ctx.branch is None:
        raise UpdateDepsError(
            phase="prepare-pr",
            command="prepare_pr",
            message="No branch on context — preflight did not run.",
        )
    git.run("push", "-u", "origin", ctx.branch)
    _ensure_label(ctx.repo)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(build_pr_body(ctx))
        body_path = Path(handle.name)
    try:
        ctx.pr_url = github.create_pr(
            base="develop",
            title=_pr_title(ctx),
            body_file=str(body_path),
            labels=[DEPS_LABEL],
        )
    finally:
        body_path.unlink(missing_ok=True)
    print(f"Opened PR: {ctx.pr_url}")


def _require_pr_url(ctx: UpdateDepsContext, phase: str) -> str:
    if ctx.pr_url is None:
        raise UpdateDepsError(
            phase=phase,
            command=phase,
            message="No PR URL on context — prepare-pr did not run.",
        )
    return ctx.pr_url


def merge_pr(ctx: UpdateDepsContext) -> None:
    """Wait for checks and merge; a merge abort is recorded, not raised (spec §5.2)."""
    pr_url = _require_pr_url(ctx, "merge")
    try:
        pr_merge.wait_and_merge(pr_url, strategy="merge")
    except pr_merge.MergeAbortError as exc:
        record_merge_failure(ctx, str(exc))


def record_merge_failure(ctx: UpdateDepsContext, message: str) -> None:
    """Record a remote-CI or merge failure and put it in the PR body."""
    pr_url = _require_pr_url(ctx, "merge")
    failed = github.failed_check_names(pr_url)
    output = message
    if failed:
        output += "\n\nFailed checks:\n" + "\n".join(f"- {name}" for name in failed)
    ctx.failure = DepsFailure(
        kind="merge",
        message="The dependency-update PR could not merge.",
        output_tail=tail_output(output),
        reproduce=f"{pr_url}/checks",
    )
    github.edit_pr_body(pr_url, body=build_pr_body(ctx))


def build_fix_task_body(ctx: UpdateDepsContext) -> str:
    """The fix task's body: the open PR, how to cut the fix branch, and the failure."""
    failure = ctx.failure
    if failure is None:
        msg = "build_fix_task_body called without a recorded failure"
        raise ValueError(msg)
    return (
        "\n".join(
            [
                f"The mechanized dependency update for `{ctx.version}` failed. Its PR "
                f"stays open, labelled `{DEPS_LABEL}`, and gates the next "
                f"`vrg-release`: {ctx.pr_url}",
                "",
                "## How to fix",
                "",
                "Cut the fix branch from the failed update, so it starts from its "
                f"commits: `chore/<this issue number>-dep-update-{ctx.version}` from "
                f"`origin/deps/update-{ctx.version}`. Fix, then hand off as usual. Once "
                "the fix PR merges and closes this issue as completed, the next "
                "`vrg-release` closes the dependency-update PR and the gate clears.",
                "",
                failure_markdown(failure),
            ]
        )
        + "\n"
    )


def file_fix_task(ctx: UpdateDepsContext) -> None:
    """File the fix task under the repo's ad-hoc epic and link it from the PR body."""
    if ctx.version is None or ctx.pr_url is None:
        raise UpdateDepsError(
            phase="fix-task",
            command="file_fix_task",
            message="No version or PR on context — preflight or prepare-pr did not run.",
        )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(build_fix_task_body(ctx))
        body_path = Path(handle.name)
    cmd = [
        "vrg-issue-create",
        "--epic",
        "adhoc",
        "--repo",
        ctx.repo,
        "--title",
        fix_task_title(ctx.version),
        "--body-file",
        str(body_path),
    ]
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)  # noqa: S603
    finally:
        body_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise UpdateDepsError(
            phase="fix-task",
            command=" ".join(cmd),
            message="vrg-issue-create failed; no fix task was filed.",
            detail=result.stderr,
        )
    match = _CREATED_RE.search(result.stdout)
    if match is None:
        raise UpdateDepsError(
            phase="fix-task",
            command=" ".join(cmd),
            message=(
                "vrg-issue-create succeeded but could not find the created issue URL in its output."
            ),
            detail=result.stdout,
        )
    ctx.fix_issue_url = match.group(1)
    github.edit_pr_body(ctx.pr_url, body=build_pr_body(ctx))
    print(f"Filed fix task: {ctx.fix_issue_url}")


def cleanup_worktree(ctx: UpdateDepsContext) -> None:
    """Return to the root checkout and remove the managed worktree + local branch.

    On a failure the branch is already on origin (the PR's head), so deleting the
    local copy loses nothing.
    """
    if ctx.worktree_path is None:
        return
    os.chdir(ctx.repo_root)
    remove_worktree(ctx.worktree_path)
    if ctx.branch is not None:
        git.run("branch", "-D", ctx.branch)
