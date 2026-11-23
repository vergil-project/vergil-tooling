"""Phase 4: Back-merge main to develop with version bump."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, pr_create, version
from vergil_tooling.lib.release.branch_pr import branch_prs
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import known_pr_state, wait_and_merge

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

_PHASE = "back-merge-bump"
_BUMP_SUBJECT = "chore(release): bump version to {}"


def back_merge_and_bump(ctx: ReleaseContext) -> None:
    """Create back-merge branch from main, bump version, PR to develop.

    Runs inside the release's managed worktree (#1578): ``checkout -b``
    moves the worktree's own HEAD, never the root checkout's. The root's
    ``develop`` is fast-forwarded to the back-merge later, by the
    finalize cleanup stage, from the root checkout.

    Resume-safe (#1612, #3328): if the back-merge PR already exists it is
    adopted — merged when still open, skipped when already merged — and
    ``bump_pr_url`` / ``bump_merge_sha`` / ``next_version`` are hydrated
    rather than re-creating the branch. A closed, unmerged PR is not adopted.
    The context is never persisted: a resume re-runs this stage, so every
    path must record ``bump_merge_sha`` for confirm-develop (#3329).

    With no PR yet, the post branch an earlier run left behind is reused
    rather than re-created (#4415): see :func:`_checkout_post_branch`.
    """
    branch = f"release/post-{ctx.version}"

    # Only a same-repo PR with a trusted author is adopted (GHSA-r4j2-cwcp-xg56):
    # a fork PR on the post branch's name is skipped and reported.
    prs = branch_prs(ctx.repo, branch, phase=_PHASE)
    if prs.open_pr is not None:
        ctx.bump_pr_url = prs.open_pr.url
        if known_pr_state(ctx.bump_pr_url) == "MERGED":
            print(f"Back-merge PR already merged: {ctx.bump_pr_url}")
        else:
            print(f"Back-merge PR already open — merging: {ctx.bump_pr_url}")
            wait_and_merge(ctx.bump_pr_url, phase="back-merge-bump")
        ctx.bump_merge_sha = _merge_commit(ctx.bump_pr_url)
        ctx.next_version = _post_branch_version(ctx, branch)
        return

    # Resume-safe against an already-merged back-merge PR (#3328), the bump
    # counterpart of prepare's #2998. A failure after the back-merge merged
    # (e.g. a confirm-develop failure) leaves no open PR, and `checkout -b`
    # below fails on the existing branch. Adopt it instead. A merged PR's head
    # branch may already be deleted, so the bumped version is read from the
    # PR's own pull ref, which GitHub keeps. A closed, unmerged PR is not
    # adopted.
    merged = prs.latest_closed
    if merged is not None and merged.state == "MERGED":
        ctx.bump_pr_url = merged.url
        print(f"Back-merge PR already merged: {ctx.bump_pr_url}")
        ctx.bump_merge_sha = _merge_commit(ctx.bump_pr_url)
        ctx.next_version = _merged_pr_version(ctx, str(merged.number))
        return

    print("Fetching main...")
    git.run("fetch", "--tags", "--force", "origin", "main")

    # Re-runnable after a failure part-way through (#4415): an earlier run may
    # have left the post branch, a partial bump or the bump commit behind.
    next_ver = _checkout_post_branch(ctx, branch)
    if next_ver is None:
        next_ver = version.bump(ctx.work_root)
        print(f"Bumped version to {next_ver}")
        git.run("add", "-A")
        git.run("commit", "-m", _BUMP_SUBJECT.format(next_ver))

    # Idempotent: a no-op when origin already has this tip, which
    # _checkout_post_branch has checked is the only remote state it accepts.
    git.run("push", "-u", "origin", branch)

    pr_url = _create_bump_pr(ctx, next_ver, branch)
    print(f"Back-merge PR created: {pr_url}")

    wait_and_merge(pr_url, phase="back-merge-bump")

    ctx.bump_pr_url = pr_url
    ctx.bump_merge_sha = _merge_commit(pr_url)
    ctx.next_version = next_ver


def _checkout_post_branch(ctx: ReleaseContext, branch: str) -> str | None:
    """Check out *branch* for the bump, creating it or reusing an earlier run's.

    Returns the bumped version when the branch already carries this stage's
    bump commit (the bump is then skipped), else None. With no local or
    remote *branch*, it is created at ``origin/main``, as on a first run.
    An existing branch is reused only when it holds nothing but this stage's
    own work (see :func:`_resume_post_branch`); any other state raises a
    ``ReleaseError`` naming the branch, and nothing is reset or pushed over.
    """
    remote_tip = _remote_tip(branch)
    if not git.ref_exists(f"refs/heads/{branch}"):
        if remote_tip is None:
            print(f"Creating branch: {branch}")
            git.run("checkout", "-b", branch, "origin/main")
            return None
        _require_clean(branch, "check out the pushed branch")
        print(f"Branch {branch} is on origin at {remote_tip} — checking it out to resume.")
        git.run("checkout", "-b", branch, remote_tip)
        return _resume_post_branch(ctx, branch, remote_tip)

    local_tip = git.commit_sha(f"refs/heads/{branch}")
    if remote_tip is not None and remote_tip != local_tip:
        raise ReleaseError(
            phase=_PHASE,
            command=f"git ls-remote --heads origin {branch}",
            message=(
                f"Branch '{branch}' exists locally at {local_tip} and on origin at "
                f"{remote_tip}. An earlier back-merge-bump pushes exactly its local "
                f"tip, so the two should match. Refusing to push over either side; "
                f"reconcile them before resuming."
            ),
        )
    if git.current_branch() != branch:
        _require_clean(branch, "switch to it")
        print(f"Switching to existing branch: {branch}")
        git.run("checkout", branch)
    return _resume_post_branch(ctx, branch, local_tip)


def _resume_post_branch(ctx: ReleaseContext, branch: str, tip: str) -> str | None:
    """Check that the checked-out *branch*, at *tip*, holds only this stage's work.

    Accepted states, from an earlier run that failed part-way:

    * at ``origin/main`` with no commit of its own: the bump still has to run.
      A partial bump left uncommitted in the worktree (a bump killed before
      it could restore its files, since a failing bump restores them
      itself) is discarded first, so the bump is not applied twice; see
      :func:`_discard_partial_bump`.
    * one commit on ``origin/main``, this stage's bump to the version after
      main's, with a clean worktree: the bump is done; its version is returned.

    Anything else (a branch that does not contain ``origin/main``, other or
    extra commits, a bump to another version, uncommitted changes on top of
    the bump commit) raises a ``ReleaseError`` and changes nothing.
    """
    main_tip = git.commit_sha("origin/main")
    if not git.is_ancestor(main_tip, tip):
        raise ReleaseError(
            phase=_PHASE,
            command=f"git merge-base --is-ancestor origin/main {branch}",
            message=(
                f"Branch '{branch}' (at {tip}) does not contain origin/main "
                f"({main_tip}), so it is not an earlier back-merge-bump's branch. "
                f"Refusing to reuse or reset it; remove or reconcile it before resuming."
            ),
        )
    expected = version.next_version(version.show(ctx.repo_root, ref=main_tip))
    subject = _BUMP_SUBJECT.format(expected)
    commits = git.read_output("log", "--format=%H %s", f"{main_tip}..{tip}").splitlines()

    if not commits:
        print(f"Reusing branch {branch}: at origin/main, the bump not yet committed.")
        _discard_partial_bump(ctx, branch, expected)
        return None

    if len(commits) == 1 and commits[0].partition(" ")[2] == subject:
        tip_version = version.show(ctx.repo_root, ref=tip)
        if tip_version != expected:
            raise ReleaseError(
                phase=_PHASE,
                command=f"git show {tip}:{version.VERSION_FILE}",
                message=(
                    f"Branch '{branch}' has the commit '{subject}', but its "
                    f"{version.VERSION_FILE} reads {tip_version}. Refusing to reuse it."
                ),
            )
        _require_clean(branch, "reuse its bump commit")
        print(f"Reusing branch {branch}: version already bumped to {expected}.")
        return expected

    listed = "\n".join(f"  {line}" for line in commits)
    raise ReleaseError(
        phase=_PHASE,
        command=f"git log origin/main..{branch}",
        message=(
            f"Branch '{branch}' has commits that are not back-merge-bump's own "
            f"(at most one, '{subject}'):\n{listed}\n"
            f"Refusing to reuse or reset it; remove or reconcile it before resuming."
        ),
    )


def _discard_partial_bump(ctx: ReleaseContext, branch: str, expected: str) -> None:
    """Restore a partial version bump an earlier run left uncommitted.

    The bump is redone from ``origin/main``, so the leftover must be nothing
    but that bump: changes to tracked files :func:`version.bump` writes, with
    VERSION among them already at *expected*. Anything else (an untracked
    file, another path, VERSION at another value) is refused, never discarded.
    """
    changed = _changed_paths()
    untracked = git.read_output("ls-files", "--others", "--exclude-standard").splitlines()
    if not changed and not untracked:
        return
    allowed = version.bump_paths(ctx.work_root)
    unexpected = sorted({*(p for p in changed if p not in allowed), *untracked})
    version_file = ctx.work_root / version.VERSION_FILE
    current = version_file.read_text().strip() if version_file.is_file() else None
    if unexpected or version.VERSION_FILE not in changed or current != expected:
        raise ReleaseError(
            phase=_PHASE,
            command="git status --porcelain",
            message=(
                f"Branch '{branch}' is at origin/main but its worktree has uncommitted "
                f"changes that are not a partial bump to {expected} "
                f"(changed: {', '.join(changed) or 'none'}; "
                f"untracked: {', '.join(untracked) or 'none'}; "
                f"{version.VERSION_FILE}: {current}). Refusing to discard them; "
                f"commit, move or remove them before resuming."
            ),
        )
    print(f"Discarding a partial bump to {expected} left by an earlier run: {', '.join(changed)}")
    git.run("checkout", "HEAD", "--", *changed)


def _changed_paths() -> list[str]:
    """Tracked paths whose index or worktree content differs from HEAD."""
    return git.read_output("diff", "--name-only", "--no-renames", "HEAD").splitlines()


def _require_clean(branch: str, action: str) -> None:
    """Refuse to *action* on *branch* while the worktree has uncommitted changes."""
    changed = _changed_paths()
    untracked = git.read_output("ls-files", "--others", "--exclude-standard").splitlines()
    if changed or untracked:
        raise ReleaseError(
            phase=_PHASE,
            command="git status --porcelain",
            message=(
                f"Cannot {action} for '{branch}': the release worktree has uncommitted "
                f"changes (changed: {', '.join(changed) or 'none'}; "
                f"untracked: {', '.join(untracked) or 'none'}). Refusing to carry or "
                f"discard them; commit, move or remove them before resuming."
            ),
        )


def _remote_tip(branch: str) -> str | None:
    """The commit *branch* is at on origin, fetched locally, or None if absent."""
    listing = git.read_output("ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if not listing:
        return None
    tip = listing.split()[0]
    git.run("fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    return tip


def _merge_commit(pr_url: str) -> str | None:
    """The commit the merged back-merge PR produced on develop, or None.

    ``mergeCommit.oid`` is the ``headSha`` develop's push hands to CD, so
    confirm-develop polls for the run of exactly this commit (#3329). None
    when GitHub reports no merge commit; confirm-develop then fails loudly
    rather than fall back to develop's tip.
    """
    return github.merge_commit_sha(pr_url) or None


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


def _create_bump_pr(ctx: ReleaseContext, next_ver: str, branch: str) -> str:
    """Open the back-merge PR for the pushed *branch*, adopting one a failed create opened.

    A ``gh pr create`` that GitHub carried out can still fail, and a blind
    retry is then refused with "already exists" (#4699): the open PR is
    adopted when it matches, and any other fails the stage, naming it.
    """
    title = f"chore(release): back-merge {ctx.version} and bump to {next_ver}"
    body = (
        f"## Summary\n\n"
        f"Back-merge main after release {ctx.version} "
        f"and bump to {next_ver}.\n\n"
        f"Ref #{ctx.issue_number}\n\n"
        f"Generated with `vrg-release`\n"
    )
    spec = pr_create.PRSpec(
        head=branch, base="develop", title=title, head_oid=git.read_output("rev-parse", branch)
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        return pr_create.create_or_adopt(
            lambda: github.create_pr(base="develop", title=title, body_file=tmp_path, retry=False),
            spec,
            opener="this release",
            rerun="resume the release",
        ).url
    except pr_create.PRCreateError as exc:
        raise ReleaseError(phase=_PHASE, command="gh pr create", message=str(exc)) from exc
    finally:
        Path(tmp_path).unlink(missing_ok=True)
