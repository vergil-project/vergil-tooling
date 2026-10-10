"""Phase 1: Prepare release — tracking issue, branch, changelog, PR."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import changelog, git, github, version
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import known_pr_state
from vergil_tooling.lib.release.tracking import create_tracking_issue

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def prepare(ctx: ReleaseContext) -> None:
    """Create tracking issue, changelog, and PR to main on the release branch.

    The release branch and its managed worktree are created by preflight
    (and chdir'd into), so this phase never switches HEAD in the root
    checkout (#1578).

    Sub-step idempotent for resume (#1612): the tracking issue is created only
    if not already adopted; if the release PR already exists the rest is
    skipped and ``release_pr_url`` hydrated; the changelog commit is skipped if
    it is already on the branch; the push is naturally idempotent.
    """
    if ctx.issue_number is None:
        create_tracking_issue(ctx)
        print(f"Tracking issue created: {ctx.issue_url}")

    branch = ctx.release_branch
    if branch is None:
        raise ReleaseError(
            phase="prepare",
            command="prepare",
            message="No release branch on context — preflight did not run.",
        )

    existing = github.pr_for_branch(branch)
    if existing is not None:
        ctx.release_pr_url = str(existing["url"])
        print(f"Release PR already exists: {ctx.release_pr_url}")
        return

    # Resume-safe against an already-merged release PR (#2998). `pr_for_branch`
    # returns open PRs only, so a failure at/after merge-release (e.g. a
    # confirm-main failure) leaves the merged release PR invisible to the check
    # above — without this, prepare would re-push and `gh pr create` would fail
    # ("No commits between main and <branch>"). Adopt the merged PR so
    # `merge_release`'s MERGED-skip carries the pipeline forward. A merged PR
    # for the branch name means the release branch's work is done: prepare
    # never pushes or re-creates the branch in that case (#3172) — it either
    # adopts the PR or fails loud.
    merged = github.closed_pr_for_branch(branch)
    if merged is not None and known_pr_state(str(merged["url"])) == "MERGED":
        _adopt_merged_pr(ctx, branch, merged)
        return

    if ctx.version_override is not None:
        print(f"Applying version override: {ctx.version_override}")
        version.bump(ctx.work_root, ctx.version_override)
        git.run("add", "-A")
        git.run("commit", "-m", f"chore(release): bump version to {ctx.version}")

    _generate_changelog(ctx)

    print(f"Pushing branch: {branch}")
    git.run("push", "-u", "origin", branch)

    ctx.release_pr_url = _create_pr(ctx)
    print(f"Release PR created: {ctx.release_pr_url}")


def _adopt_merged_pr(ctx: ReleaseContext, branch: str, merged: dict[str, str]) -> None:
    """Adopt an already-merged release PR, reconciling the local branch tip.

    * PR head == local tip: adopt as-is.
    * PR head descends from the local tip: ``merge-release`` updated the
      BEHIND PR on GitHub (a server-side update-branch merge), so the local
      branch is stale. Fetch the PR head and fast-forward the local branch to
      it so later stages see the real tip, then adopt (#3172).
    * Otherwise (no head, or an unrelated tip — the #1719 reused-branch-name
      straggler): fail loud. Never fall through to a push, which would
      re-create the deleted remote release branch at a stale tip.
    """
    url = str(merged["url"])
    head = str(merged.get("headRefOid") or "")
    local = git.read_output("rev-parse", branch)
    if not head:
        raise ReleaseError(
            phase="prepare",
            command=f"gh pr list --head {branch} --state closed",
            message=(
                f"Release PR {url} for '{branch}' is already merged, but GitHub "
                f"reported no head commit for it, so the local tip ({local}) "
                f"cannot be reconciled. Refusing to push or re-create the branch."
            ),
        )
    if head != local:
        git.run("fetch", "origin", f"pull/{merged['number']}/head")
        if not git.is_ancestor(local, head):
            raise ReleaseError(
                phase="prepare",
                command=f"git merge-base --is-ancestor {local} {head}",
                message=(
                    f"Release PR {url} for '{branch}' is already merged, but its "
                    f"head {head} does not descend from the local '{branch}' tip "
                    f"{local}. Refusing to push or re-create the branch. Reconcile "
                    f"the local branch with the merged PR head (or remove the stale "
                    f"branch) before resuming."
                ),
            )
        print(
            f"Release PR head {head} is ahead of local '{branch}' {local} "
            f"(updated on GitHub) — fast-forwarding."
        )
        git.run("merge", "--ff-only", head)
    ctx.release_pr_url = url
    print(f"Release PR already merged — adopting for resume: {ctx.release_pr_url}")


def _prepare_commit_exists(ctx: ReleaseContext) -> bool:
    """True if the ``prepare`` commit is already on the release branch."""
    subjects = git.read_output("log", "--format=%s", "origin/develop..HEAD").splitlines()
    return f"chore(release): prepare {ctx.version}" in subjects


def _generate_changelog(ctx: ReleaseContext) -> None:
    if _prepare_commit_exists(ctx):
        print(f"Changelog already prepared for v{ctx.version} — skipping.")
        return
    print(f"Generating changelog for v{ctx.version}")
    changelog.generate_changelog(ctx.work_root, ctx.version)
    git.run("add", "CHANGELOG.md")

    notes_path = changelog.generate_release_notes(ctx.work_root, ctx.version)
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
