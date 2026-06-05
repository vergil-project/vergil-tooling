"""Finalize a pull request: provenance check, merge, and cleanup.

Two modes, keyed on whether a PR argument is given:

- ``vrg-finalize-pr <PR>`` — run the pre-merge provenance check, merge
  the PR (or confirm it is already merged), then run the cleanup below.
  This replaces the manual web merge + post-merge repo cleanup.
- ``vrg-finalize-pr`` (no PR) — cleanup only, the backward-compatible
  release path: switch to the target branch, fast-forward pull, delete
  merged local branches, and prune stale remote-tracking references.

After validation succeeds, also checks the most recent CD workflow run
on the target branch and fails if it did not succeed (issue #303 — docs
publish is async and used to fail silently).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib import config, git, github, pr_provenance, worktrees
from vergil_tooling.lib.container_cache import clean_branch_images
from vergil_tooling.lib.repo_init import prompt_choice, prompt_yes_no

_CD_WORKFLOW_NAME = "CD"

_ETERNAL_BY_MODEL: dict[str, list[str]] = {
    "docs-single-branch": ["develop"],
    "library-release": ["develop", "main"],
    "application-promotion": ["develop", "release", "main"],
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Finalize a pull request.")
    parser.add_argument(
        "pr",
        nargs="?",
        default=None,
        help="PR number or URL to merge and finalize. Omit for cleanup-only (release path).",
    )
    parser.add_argument("--target-branch", default="develop", help="Target branch to switch to")
    parser.add_argument(
        "--strategy",
        default="squash",
        choices=["merge", "squash", "rebase"],
        help="Merge strategy for the PR (feature PRs default to squash)",
    )
    parser.add_argument(
        "--allow-provenance-violation",
        action="store_true",
        help="Proceed despite provenance violations (conscious human override)",
    )
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


def _infer_pr(root: Path, target_branch: str) -> str | None:
    """Resolve which PR to finalize when none was given; always confirm.

    Returns the PR URL to finalize, or None when the user confirmed
    cleanup-only. Raises SystemExit(0) on decline, and SystemExit with
    a message via require_tty when stdin is not interactive — these
    prompts are the human touch point of the workflow, and the
    explicit-PR argument is the scriptable path.
    """
    worktrees.require_tty("vrg-finalize-pr without a PR argument")

    pairs: list[tuple[worktrees.Worktree, dict[str, str]]] = []
    for wt in worktrees.list_worktrees(root):
        pr = github.pr_for_branch(wt.branch)
        if pr is None:
            # No silent exclusions: every skipped worktree says why.
            print(f"  {wt.path.name}: no open PR for {wt.branch} — not a candidate")
            continue
        pairs.append((wt, pr))

    if not pairs:
        confirmed = prompt_yes_no(
            f"No open PRs found in worktrees. Run cleanup only (switch to "
            f"{target_branch}, pull, prune branches/worktrees)?",
            default=False,
        )
        if not confirmed:
            print("Aborted.")
            raise SystemExit(0)
        return None

    if len(pairs) == 1:
        wt, pr = pairs[0]
    else:
        labels = [f"PR #{p['number']} ({w.branch}): {p['title']}" for w, p in pairs]
        chosen = prompt_choice("Multiple PRs ready to finalize", labels)
        wt, pr = pairs[labels.index(chosen)]

    if not prompt_yes_no(f"Finalize PR #{pr['number']} ({pr['title']})?", default=False):
        print("Aborted.")
        raise SystemExit(0)
    return pr["url"]


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


def _finalize_specific_pr(args: argparse.Namespace) -> int:
    """Run the pre-merge provenance check, then merge (or confirm merged).

    Returns 0 to continue to cleanup, nonzero to abort.
    """
    print(f"Checking provenance for PR {args.pr}...")
    result = pr_provenance.check_pr(args.pr)

    for adv in result.advisories:
        print(
            f"  ADVISORY: {adv.login} ({adv.role.value}) performed '{adv.action}' "
            "— permitted but advisory.",
            file=sys.stderr,
        )

    if result.violations:
        print(f"ERROR: PR {args.pr} has provenance violations:", file=sys.stderr)
        for v in result.violations:
            print(
                f"  {v.login} ({v.role.value}) performed forbidden action '{v.action}'.",
                file=sys.stderr,
            )
        if not args.allow_provenance_violation:
            print(
                "\n  Aborting merge. Re-run with --allow-provenance-violation to\n"
                "  override consciously — you hold every right, but the violation\n"
                "  is in front of you.",
                file=sys.stderr,
            )
            return 1
        print(
            "  Overriding provenance violations per --allow-provenance-violation.",
            file=sys.stderr,
        )

    if github.pr_state(args.pr) == "MERGED":
        print(f"PR {args.pr} already merged.")
    elif args.dry_run:
        print(f"  [dry-run] merge PR {args.pr} (--{args.strategy})")
    else:
        print(f"Merging PR {args.pr} (--{args.strategy})...")
        github.merge(args.pr, strategy=args.strategy)

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if not git.is_main_worktree():
        main_root = git.main_worktree_root()
        print(
            f"ERROR: vrg-finalize-pr must be run from the main worktree at {main_root},\n"
            "  not from a secondary worktree. The script removes worktrees during cleanup\n"
            "  and cannot safely do so when the calling shell's CWD is inside one.",
            file=sys.stderr,
        )
        return 1

    root = git.repo_root()

    if args.pr is None:
        try:
            args.pr = _infer_pr(root, args.target_branch)
        except SystemExit as exc:
            if exc.code == 0:
                return 0
            raise

    if args.pr is not None:
        rc = _finalize_specific_pr(args)
        if rc != 0:
            return rc

    try:
        vergil_config = config.read_config(root)
        model = vergil_config.project.branching_model
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
        wt = worktrees.worktree_for_branch(branch, root)
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
