"""PR, fix-task and worktree helpers for vrg-update-deps."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

from vergil_tooling.lib import finalize_watch, git, github, pr_create, pr_merge, pr_path, trust
from vergil_tooling.lib.labels import ensure_labels
from vergil_tooling.lib.managed_worktree import remove_worktree
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.update_deps import changes, report
from vergil_tooling.lib.update_deps.context import DEPS_LABEL, DepsFailure, UpdateDepsError
from vergil_tooling.lib.update_deps.failure import failure_markdown, tail_output

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.update_deps.context import FailureKind, UpdateDepsContext

# The release gate parses this line out of the PR body (spec §5.3).
FIX_TASK_PREFIX = "Fix task: "
# vrg-issue-create prints "Created <url>, linked under epic <slug>." on success.
_CREATED_RE = re.compile(r"Created (https://github\.com/\S+/issues/\d+),")
# Upper bound on the open issues the fix-task search lists. Reaching it is a
# refusal: a matching task beyond it would be filed again (#4038).
_FIX_TASK_SEARCH_LIMIT = 100
# GitHub's answer, with a 422, to a DELETE of a ref that does not exist.
_NO_REFERENCE = "Reference does not exist"


def fix_task_title(version: str) -> str:
    """The fix task's exact title (spec §5.2)."""
    return f"Fix failed dependency update for {version}"


def _pr_title(ctx: UpdateDepsContext) -> str:
    return f"chore(deps): dependency update for {ctx.version}"


def build_pr_body(ctx: UpdateDepsContext) -> str:
    """Build the PR body: updates, package changes, warnings, held-back report, any failure."""
    lines = ["Mechanized dependency update (`vrg-update-deps`).", "", "## Updated", ""]
    lines.extend(f"- **{r.updater}** — {r.summary}" for r in ctx.results if r.changed)
    lines.extend(["", changes.render_markdown(ctx.results)])
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
    title = _pr_title(ctx)
    spec = pr_create.PRSpec(
        head=ctx.branch,
        base="develop",
        title=title,
        head_oid=git.read_output("rev-parse", ctx.branch),
    )
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(build_pr_body(ctx))
        body_path = Path(handle.name)
    try:
        created = pr_create.create_or_adopt(
            lambda: github.create_pr(
                base="develop",
                title=title,
                body_file=str(body_path),
                labels=[DEPS_LABEL],
                retry=False,
            ),
            spec,
            opener="this run",
            rerun="run vrg-update-deps again",
        )
    except pr_create.PRCreateError as exc:
        raise UpdateDepsError(phase="prepare-pr", command="gh pr create", message=str(exc)) from exc
    finally:
        body_path.unlink(missing_ok=True)
    ctx.pr_url = created.url
    if created.adopted:
        # gh applies --label in a call after the create, which the failure may
        # have cut off; the release gate depends on the label (spec §5.3).
        github.run("pr", "edit", created.url, "--add-label", DEPS_LABEL)
    print(f"Opened PR: {ctx.pr_url}")


def _require_pr_url(ctx: UpdateDepsContext, phase: str) -> str:
    if ctx.pr_url is None:
        raise UpdateDepsError(
            phase=phase,
            command=phase,
            message="No PR URL on context — prepare-pr did not run.",
        )
    return ctx.pr_url


def arm_and_wait(ctx: UpdateDepsContext, *, holder_pass: str | None = None) -> None:
    """Arm the PR through the one PR path, then wait for GitHub to merge it (spec §5.4).

    :func:`pr_path.arm` works the merge method out itself (``MERGE``, from the
    ``dependency-update`` label) and pins it to the head this run pushed.
    *holder_pass* is the release lock's token when vrg-release's own
    ``update-deps`` stage runs the update: the release holds the lock, and
    its update is the holder's, so the path arms it under the lock.
    Standalone ``vrg-update-deps`` passes none. Then:

    - ``armed``: auto-merge is on, and :func:`pr_merge.wait_until_merged`
      watches the PR until it merges.
    - ``merged``: the path merged it directly (it could already merge); no wait.
    - ``refused``: nothing was turned on. The path's reason is recorded as a
      merge failure (:func:`record_merge_failure`), with no wait.
    - ``held``: a release holds the lock, and the path left the PR unarmed.
      It is reported (``held for release <v>; the unlock sweep arms it``) and
      marked on *ctx* (``ctx.held``), with no wait and no failure recorded:
      the release's unlock sweep arms it (spec §5.4, §5.5).

    A wait that ends without a merge (a closed PR, a failed check, a conflict,
    the deadline) is recorded the same way, not raised (spec §5.2). A step of
    the path that cannot be completed raises :class:`UpdateDepsError`.

    The wait's deadline is ``$VRG_FINALIZE_DEADLINE`` (7200 seconds by
    default), the watch deadline of every other armed PR; a malformed value
    raises before anything is armed. A merged PR is marked on *ctx*: the
    finalize stage deletes its branch once the worktree holding it is gone
    (:func:`delete_merged_branch`).
    """
    pr_url = _require_pr_url(ctx, "merge")
    if ctx.branch is None:
        raise UpdateDepsError(
            phase="merge",
            command="arm_and_wait",
            message="No branch on context — preflight did not run.",
        )
    try:
        deadline = finalize_watch.deadline_secs()
    except ValueError as exc:
        raise UpdateDepsError(phase="merge", command="arm_and_wait", message=str(exc)) from exc
    head = git.read_output("rev-parse", ctx.branch)
    try:
        result = pr_path.arm(pr_url, clone=ctx.repo_root, head=head, holder_pass=holder_pass)
    except pr_path.PRPathError as exc:
        raise UpdateDepsError(
            phase="merge",
            command="pr_path.arm",
            message=f"Arming the dependency-update PR failed: {exc}",
        ) from exc
    if result.outcome == "refused":
        record_merge_failure(ctx, f"The one PR path refused {result.pr_url}: {result.detail}")
        return
    if result.outcome == "held":
        print(f"{pr_url}: {result.detail}; the unlock sweep arms it")
        ctx.held = result.detail
        return
    if result.outcome == "armed":
        try:
            pr_merge.wait_until_merged(pr_url, deadline_secs=deadline)
        except pr_merge.MergeAbortError as exc:
            record_merge_failure(ctx, str(exc))
            return
    ctx.merged = True


def delete_merged_branch(ctx: UpdateDepsContext) -> None:
    """Delete a merged dependency-update PR's branch on origin and locally (#3330, #4952).

    Called only once the PR is confirmed merged: by this run's merge, or by
    preflight finding the PR already merged on a re-run. A branch whose PR did
    not merge is never deleted here, so a resume can pick it up.

    GitHub's delete-on-merge setting usually has deleted it on origin already,
    but it can skip its after-merge processing. Deliberately not ``gh pr merge
    --delete-branch``: ``github.merge`` is shared with other callers, and gh
    also deletes the *local* branch, switching off it first — from this linked
    worktree (``develop`` is held by the root checkout) that can fail and make
    gh exit non-zero after a successful merge, which would read as a merge
    failure. The same split as the release gate's close-then-delete (#3332).

    A failed deletion raises :class:`UpdateDepsError`: the run fails, so
    ``vrg-release`` leaves its ``update-deps`` stage not done and a
    ``--resume``, finding the PR merged, tries the deletion again.
    """
    if ctx.branch is None:
        raise UpdateDepsError(
            phase="finalize",
            command="delete_merged_branch",
            message="No branch on context — preflight did not run.",
        )
    pr_url = ctx.pr_url or ctx.landed_pr_url
    endpoint = f"repos/{ctx.repo}/git/refs/heads/{ctx.branch}"
    try:
        deleted = github.delete_if_exists(endpoint)
    except subprocess.CalledProcessError as exc:
        if not _reference_absent(exc):
            raise _branch_delete_failed(ctx, pr_url, endpoint, exc) from exc
        # GitHub answers a DELETE of a ref already gone (delete-on-merge got
        # there first) with 422 "Reference does not exist", not only 404.
        deleted = False
    if deleted:
        print(f"Deleted merged branch {ctx.branch} on origin.")
    else:
        print(f"Branch {ctx.branch} already deleted on origin.")
    if not git.ref_exists(f"refs/heads/{ctx.branch}"):
        return
    try:
        git.run("branch", "-D", ctx.branch)
    except subprocess.CalledProcessError as exc:
        raise UpdateDepsError(
            phase="finalize",
            command=f"git branch -D {ctx.branch}",
            message=(
                f"The dependency-update PR {pr_url} merged, but deleting its local "
                f"branch {ctx.branch} failed: {error_text(exc)}. Delete it by hand "
                f"(git branch -D {ctx.branch}) or re-run."
            ),
            detail=error_text(exc),
        ) from exc
    print(f"Deleted merged branch {ctx.branch} locally.")


def _reference_absent(exc: subprocess.CalledProcessError) -> bool:
    """Whether a failed ref DELETE says the ref does not exist (422 "Reference does not exist")."""
    # ``gh api -i`` puts the status line and body on stdout, its error on stderr.
    detail = f"{exc.stderr or ''}{exc.stdout or ''}{error_text(exc)}"
    return "422" in detail and _NO_REFERENCE in detail


def _branch_delete_failed(
    ctx: UpdateDepsContext, pr_url: str | None, endpoint: str, exc: subprocess.CalledProcessError
) -> UpdateDepsError:
    return UpdateDepsError(
        phase="finalize",
        command=f"gh api -X DELETE {endpoint}",
        message=(
            f"The dependency-update PR {pr_url} merged, but deleting its branch "
            f"{ctx.branch} on origin failed: {error_text(exc)}. Delete it by hand "
            f"(git push origin --delete {ctx.branch}) or re-run."
        ),
        detail=error_text(exc),
    )


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
    the same version reuses it (#3560). Only a task with a trusted body is
    adopted (:func:`_oldest_adoptable_fix_task`). GitHub search tokenizes on
    punctuation, so the query is a broad net, filtered client-side. A failed or
    malformed lookup raises: never guess, and never silently file a possible
    duplicate.
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
        "--limit",
        str(_FIX_TASK_SEARCH_LIMIT),
        "--json",
        "number,url,title",
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
    if len(results) >= _FIX_TASK_SEARCH_LIMIT:
        raise UpdateDepsError(
            phase="fix-task",
            command="gh " + " ".join(cmd),
            message=(
                f"The fix-task search matched {_FIX_TASK_SEARCH_LIMIT}+ open issues, so the "
                "list may be truncated; no fix task was filed."
            ),
        )
    candidates: list[tuple[int, str]] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        item = cast("dict[str, object]", raw)
        if item.get("title") == title:
            candidates.append((int(str(item["number"])), str(item["url"])))
    return _oldest_adoptable_fix_task(repo, sorted(candidates), kind)


def _oldest_adoptable_fix_task(
    repo: str, candidates: Sequence[tuple[int, str]], kind: FailureKind
) -> str | None:
    """The URL of the oldest of *candidates* whose trusted body has *kind*'s heading.

    Each body is read through :func:`trust.read_issue`, so a task is adopted
    only when its author and every editor of its body are trusted; the
    search's own copy of the body is never judged. An untrusted task is never
    adopted, and is reported on stderr, metadata only (GHSA-777j-j57r-92ff).
    A read failure raises :class:`UpdateDepsError`: no fix task is filed.
    """
    heading = re.compile(rf"^## Failure: {re.escape(kind)}$", re.MULTILINE)
    for number, url in candidates:
        try:
            content = trust.read_issue(repo, number)
        except (trust.TrustReadError, TrustedAuthorsNotConfiguredError) as exc:
            raise UpdateDepsError(
                phase="fix-task",
                command=f"read_issue {repo}#{number}",
                message="Could not read who wrote an open fix task; no fix task was filed.",
                detail=str(exc),
            ) from exc
        trust.report_skipped(content.skipped, stream=sys.stderr)
        if content.body is not None and heading.search(content.body):
            return url
    return None


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
    remove_worktree(
        ctx.worktree_path,
        branch=ctx.branch or "(no branch recorded)",
        reason="the vrg-update-deps run is finished",
    )
    if ctx.branch is not None:
        git.run("branch", "-D", ctx.branch)
