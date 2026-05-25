"""Finalize a repository after a PR merge.

Switches to the target branch, fast-forward pulls, deletes merged local
branches, and prunes stale remote-tracking references. After
validation succeeds, also checks the most recent CD workflow run on the
target branch and fails if it did not succeed (issue #303 — docs
publish is async and used to fail silently).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import config, git
from vergil_tooling.lib.container_cache import clean_branch_images

_CD_WORKFLOW_NAME = "CD"

_ETERNAL_BY_MODEL: dict[str, list[str]] = {
    "docs-single-branch": ["develop"],
    "library-release": ["develop", "main"],
    "application-promotion": ["develop", "release", "main"],
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Finalize a repository after a PR merge.")
    parser.add_argument("--target-branch", default="develop", help="Target branch to switch to")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without making changes"
    )
    return parser.parse_args(argv)


def _run(args: list[str], *, dry_run: bool) -> None:
    if dry_run:
        print(f"  [dry-run] git {' '.join(args)}")
    else:
        git.run(*args)


def _worktree_is_dirty(wt_path: Path) -> bool:
    """Return True if *wt_path* has modified or untracked files."""
    result = subprocess.run(  # noqa: S603
        ("git", "-C", str(wt_path), "status", "--porcelain"),  # noqa: S607
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _worktree_for_branch(branch: str, repo_root: Path) -> Path | None:
    """Return the worktree path that has *branch* checked out, or None.

    Constrains the search to worktrees inside ``repo_root/.worktrees/``
    — the canonical location per the worktree convention. Worktrees
    elsewhere (developer-managed, outside the convention) are
    deliberately ignored: auto-removing them would surprise the user
    in cases the convention doesn't account for. Issue #315.
    """
    output = git.read_output("worktree", "list", "--porcelain")
    canonical_root = (repo_root / ".worktrees").resolve()

    current_path: Path | None = None
    target_ref = f"refs/heads/{branch}"
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ").strip())
        elif line.startswith("branch ") and current_path is not None:
            ref = line.removeprefix("branch ").strip()
            if ref == target_ref:
                resolved = current_path.resolve()
                # Only auto-remove worktrees inside the canonical
                # `.worktrees/` directory.
                try:
                    resolved.relative_to(canonical_root)
                except ValueError:
                    return None
                return resolved
    return None


def _check_cd_workflow_status(target_branch: str) -> str | None:
    """Inspect the most recent CD workflow run on ``target_branch`` and
    return a one-line message if it failed, None if it succeeded, is in
    progress, or doesn't exist.

    The CD workflow is async relative to the merge that triggers it, so
    a failure here doesn't block any PR — but it does mean the site or
    release artifacts may be stale. This check surfaces such failures
    during finalize so they can be investigated immediately. Issue #303.

    Returns None when:
      - no CD workflow exists in the repo
      - the latest run succeeded or is still in progress
      - the JSON response is malformed (defensive)
    """
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gh",
            "run",
            "list",
            "--workflow",
            _CD_WORKFLOW_NAME,
            "--branch",
            target_branch,
            "--limit",
            "1",
            "--json",
            "conclusion,databaseId,headSha,createdAt,url",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout or ""
    try:
        runs = json.loads(stdout) if stdout.strip() else []
    except json.JSONDecodeError:
        return None
    if not runs:
        return None
    run = runs[0]
    conclusion = run.get("conclusion") or ""
    if conclusion in ("", "success", "skipped", "neutral"):
        return None
    sha = (run.get("headSha") or "")[:7]
    return (
        f"CD workflow run {run.get('databaseId')} on "
        f"{target_branch} ({sha}) ended with conclusion '{conclusion}'.\n"
        f"  {run.get('url') or ''}"
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not git.is_main_worktree():
        main_root = git.main_worktree_root()
        print(
            f"ERROR: vrg-finalize-repo must be run from the main worktree at {main_root},\n"
            "  not from a secondary worktree. The script removes worktrees during cleanup\n"
            "  and cannot safely do so when the calling shell's CWD is inside one.",
            file=sys.stderr,
        )
        return 1

    root = git.repo_root()

    try:
        st_config = config.read_config(root)
        model = st_config.project.branching_model
    except FileNotFoundError:
        model = ""
    except config.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    eternal = {"gh-pages"}
    if model in _ETERNAL_BY_MODEL:
        eternal.update(_ETERNAL_BY_MODEL[model])
    else:
        print("WARNING: branching_model not found; protecting develop and main.", file=sys.stderr)
        eternal.update(("develop", "main"))

    current = git.current_branch()
    if current != args.target_branch:
        print(f"Switching to {args.target_branch}...")
        _run(["checkout", args.target_branch], dry_run=args.dry_run)
    else:
        print(f"Already on {args.target_branch}.")

    print(f"Pulling latest from origin/{args.target_branch}...")
    _run(["fetch", "--tags", "--force", "origin", args.target_branch], dry_run=args.dry_run)
    _run(["pull", "--ff-only", "origin", args.target_branch], dry_run=args.dry_run)

    print("Checking for merged local branches...")
    deleted: list[str] = []
    for branch in git.merged_branches(args.target_branch):
        if branch in eternal:
            continue
        # If the branch is still checked out in a `.worktrees/` worktree
        # (typical: the worktree we did the PR's work in), `git branch -D`
        # refuses to delete it — there's no force past "branch is
        # checked out somewhere." Auto-remove the worktree first.
        # Constrained to the canonical `.worktrees/` location so user-
        # created worktrees elsewhere are never silently removed.
        # Issue #315.
        wt = _worktree_for_branch(branch, root)
        if wt is not None:
            if _worktree_is_dirty(wt):
                print(f"  Skipping {branch}: worktree {wt} has uncommitted changes")
                continue
            print(f"  Removing worktree: {wt}")
            _run(["worktree", "remove", str(wt)], dry_run=args.dry_run)
        # `git branch -D` (force) rather than `-d` because `--merged`
        # already vetted these branches as reachable from the target;
        # `-d`'s redundant safety check rejects branches whose tips
        # were rewritten by rebase + force-push during a CI fixup loop
        # (the upstream-tracking ref is gone after `fetch --prune`).
        # Trusting our own filter avoids the disagreement. Issue #307.
        print(f"  Deleting merged branch: {branch}")
        _run(["branch", "-D", branch], dry_run=args.dry_run)
        deleted.append(branch)
        if not args.dry_run:
            removed = clean_branch_images(branch)
            if removed:
                print(f"  Cleaned {removed} cached container image(s) for {branch}")

    print("Pruning stale remote-tracking references...")
    if args.dry_run:
        print("  [dry-run] git remote prune origin")
    else:
        git.run("remote", "prune", "origin")

    # -- working-tree cleanliness gate (issue #472) ----------------------------
    if not args.dry_run:
        dirty = git.working_tree_status()
        if dirty:
            print()
            print(
                f"ERROR: {args.target_branch} working tree is not clean.",
                file=sys.stderr,
            )
            for line in dirty.splitlines():
                print(f"  {line}", file=sys.stderr)
            print(
                "\n  Clean up these files before starting the next issue.",
                file=sys.stderr,
            )
            return 1

    # -- post-finalization validation ------------------------------------------
    # Run canonical validation to catch problems on the target branch before
    # the next PR is created.  Failures are reported as warnings — the
    # finalization itself already succeeded.

    validation_failed = False
    if not args.dry_run:
        print()
        print("Running post-finalization validation via vrg-container-run...")
        repo_root = Path(git.repo_root())
        if (repo_root / "pyproject.toml").is_file():
            cmd: tuple[str, ...] = ("vrg-container-run", "--", "uv", "run", "vrg-validate")
        else:
            cmd = ("vrg-container-run", "--", "vrg-validate")

        result = subprocess.run(cmd, check=False)  # noqa: S603
        if result.returncode != 0:
            validation_failed = True
    else:
        print("  [dry-run] vrg-container-run -- [uv run] vrg-validate")

    # Docs-publish sanity check (issue #303). Runs after validation
    # so a real validation failure stays the headline; a docs failure
    # is a softer warning since docs publishing is async and doesn't
    # block subsequent merges.
    docs_failure: str | None = None
    if not args.dry_run:
        docs_failure = _check_cd_workflow_status(args.target_branch)

    print()
    print("Finalization complete.")
    print(f"  Branch: {args.target_branch}")
    print(f"  Deleted: {' '.join(deleted) if deleted else '(none)'}")
    print("  Remotes: pruned")

    if validation_failed:
        print()
        print("ERROR: post-finalization validation failed.", file=sys.stderr)
        print(f"  The {args.target_branch} branch has issues that should be", file=sys.stderr)
        print("  fixed before creating the next PR.", file=sys.stderr)
        return 1

    if docs_failure is not None:
        print()
        print(
            "ERROR: most recent CD workflow run did not succeed.",
            file=sys.stderr,
        )
        print(f"  {docs_failure}", file=sys.stderr)
        print(
            "  CD workflow is async — investigate before the next merge so",
            file=sys.stderr,
        )
        print("  the site doesn't drift further from develop.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
