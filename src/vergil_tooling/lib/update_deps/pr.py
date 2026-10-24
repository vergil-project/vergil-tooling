"""PR, fix-task and worktree helpers for vrg-update-deps."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import git, github, pr_merge
from vergil_tooling.lib.labels import ensure_labels
from vergil_tooling.lib.managed_worktree import remove_worktree
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.update_deps import report
from vergil_tooling.lib.update_deps.context import DEPS_LABEL, DepsFailure, UpdateDepsError
from vergil_tooling.lib.update_deps.failure import failure_markdown, tail_output

if TYPE_CHECKING:
    from vergil_tooling.lib.update_deps.context import FailureKind, UpdateDepsContext

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


def prepare_pr(ctx: UpdateDepsContext) -> None:
    """Push the worktree branch and open the labelled PR to develop."""
    if ctx.branch is None:
        raise UpdateDepsError(
            phase="prepare-pr",
            command="prepare_pr",
            message="No branch on context — preflight did not run.",
        )
    git.run("push", "-u", "origin", ctx.branch)
    ensure_labels(ctx.repo, (DEPS_LABEL,))
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
        return
    delete_merged_branch(ctx)


def delete_merged_branch(ctx: UpdateDepsContext) -> None:
    """Delete the merged PR's head branch on origin (#3330).

    GitHub's delete-on-merge setting usually has already, but it can skip its
    after-merge processing; a leftover branch would make the next run at this
    version report "already merged". Deliberately not ``gh pr merge
    --delete-branch``: ``github.merge`` is shared with other callers, and gh
    also deletes the *local* branch, switching off it first — from this linked
    worktree (``develop`` is held by the root checkout) that can fail and make
    gh exit non-zero after a successful merge, which would read as a merge
    failure. The same split as the release gate's close-then-delete (#3332).
    A failure is loud but does not fail the run: the update has landed.
    """
    endpoint = f"repos/{ctx.repo}/git/refs/heads/{ctx.branch}"
    try:
        deleted = github.delete_if_exists(endpoint)
    except subprocess.CalledProcessError as exc:
        print(
            f"WARNING: merged {ctx.pr_url}, but could not delete its branch "
            f"{ctx.branch}: {error_text(exc)}\n"
            f"Delete it by hand: git push origin --delete {ctx.branch}"
        )
        return
    if deleted:
        print(f"Deleted merged branch {ctx.branch} on origin.")
    else:
        print(f"Branch {ctx.branch} already deleted on origin.")


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


def _update_fix_task_body(ctx: UpdateDepsContext, failure: DepsFailure) -> str:
    """The fix task's body for an ``update`` failure: no PR, so it stands alone (#3550)."""
    which = f"the `{failure.updater}` updater" if failure.updater else "an updater"
    rerun = "vrg-update-deps"
    if failure.updater:
        rerun += f" --only {failure.updater}"
    return (
        "\n".join(
            [
                f"The mechanized dependency update for `{ctx.version}` failed in {which}, "
                "before validation. Nothing was pushed and no PR was opened, so this "
                "task is the only open record of the failure.",
                "",
                "## How to fix",
                "",
                f"Cut the fix branch from develop: `chore/<this issue number>-dep-update-"
                f"{ctx.version}` from `origin/develop`. Reproduce with the command in the "
                f"failure section below, or re-run `{rerun}` from a clean `develop` "
                "(human-only). Fix the cause, then hand off as usual. Once the fix "
                "merges, the next `vrg-update-deps` run, or the next `vrg-release`, "
                "applies the update.",
                "",
                failure_markdown(failure),
            ]
        )
        + "\n"
    )


def build_fix_task_body(ctx: UpdateDepsContext) -> str:
    """The fix task's body: the open PR, how to cut the fix branch, and the failure."""
    failure = ctx.failure
    if failure is None:
        msg = "build_fix_task_body called without a recorded failure"
        raise ValueError(msg)
    if failure.kind == "update":
        return _update_fix_task_body(ctx, failure)
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


def find_open_fix_task(repo: str, version: str, kind: FailureKind) -> str | None:
    """The URL of the oldest open fix task for *version* and failure *kind*, else None.

    A task matches on its exact title (version-keyed) and on the ``## Failure:
    <kind>`` heading its body carries, so a re-run that fails the same way at
    the same version reuses it (#3560). GitHub search tokenizes on punctuation,
    so the query is a broad net, filtered client-side. A failed or malformed
    lookup raises: never guess, and never silently file a possible duplicate.
    """
    title = fix_task_title(version)
    cmd = (
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        f'"{title}" in:title',
        "--state",
        "open",
        "--json",
        "number,url,title,body",
    )
    try:
        results = github.read_json(*cmd)
    except subprocess.CalledProcessError as exc:
        raise UpdateDepsError(
            phase="fix-task",
            command="gh " + " ".join(cmd),
            message="Could not look for an open fix task; no fix task was filed.",
            detail=error_text(exc),
        ) from exc
    if not isinstance(results, list):
        raise UpdateDepsError(
            phase="fix-task",
            command="gh " + " ".join(cmd),
            message="gh issue list returned unexpected JSON; no fix task was filed.",
            detail=str(results),
        )
    heading = re.compile(rf"^## Failure: {re.escape(kind)}$", re.MULTILINE)
    matches: list[tuple[int, str]] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, object]", raw)
        if item.get("title") == title and heading.search(str(item.get("body", ""))):
            matches.append((int(str(item["number"])), str(item["url"])))
    if not matches:
        return None
    return min(matches)[1]


def _repeat_failure_comment(ctx: UpdateDepsContext, failure: DepsFailure) -> str:
    """The comment that records a repeat failure on the already-open fix task."""
    where = "No PR was opened." if failure.kind == "update" else f"Its PR stays open: {ctx.pr_url}"
    return (
        "\n".join(
            [
                f"The mechanized dependency update for `{ctx.version}` failed again "
                f"({failure.kind}). {where}",
                "",
                failure_markdown(failure),
            ]
        )
        + "\n"
    )


def file_fix_task(ctx: UpdateDepsContext) -> None:
    """File the fix task under the repo's ad-hoc epic and link it from the PR body.

    An ``update`` failure opened no PR, so its task is filed with no PR to link.
    When an open task already covers this version and failure kind (a re-run
    after a transient failure, #3560), the new failure is added to it as a
    comment and that task is linked instead of filing a duplicate.
    """
    no_pr = ctx.failure is not None and ctx.failure.kind == "update"
    if ctx.version is None or (ctx.pr_url is None and not no_pr):
        raise UpdateDepsError(
            phase="fix-task",
            command="file_fix_task",
            message="No version or PR on context — preflight or prepare-pr did not run.",
        )
    failure = ctx.failure
    if failure is None:
        msg = "file_fix_task called without a recorded failure"
        raise ValueError(msg)
    existing = find_open_fix_task(ctx.repo, ctx.version, failure.kind)
    if existing is not None:
        github.run("issue", "comment", existing, "--body", _repeat_failure_comment(ctx, failure))
        _link_fix_task(ctx, existing)
        print(f"Fix task already open for {ctx.version}; added this failure to it: {existing}")
        return
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
    _link_fix_task(ctx, match.group(1))
    print(f"Filed fix task: {ctx.fix_issue_url}")


def _link_fix_task(ctx: UpdateDepsContext, url: str) -> None:
    """Record the fix task on the context and link it from the PR body, if any."""
    ctx.fix_issue_url = url
    if ctx.pr_url is not None:
        github.edit_pr_body(ctx.pr_url, body=build_pr_body(ctx))


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
