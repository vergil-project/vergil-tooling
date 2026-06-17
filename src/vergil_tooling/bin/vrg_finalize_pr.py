"""Finalize a pull request: provenance check, merge, and cleanup.

Three modes:

- ``vrg-finalize-pr <PR>`` — run the pre-merge provenance check, merge
  the PR (or confirm it is already merged), then run the cleanup below.
  This replaces the manual web merge + post-merge repo cleanup.
- ``vrg-finalize-pr`` (no PR) — interactive: infer which PR to finalize
  from open PRs in ``.worktrees/`` worktrees, confirm via prompts, then
  run the cleanup. Requires a real terminal on both stdin and stdout.
- ``vrg-finalize-pr --cleanup-only`` — non-interactive release path:
  skip inference and merge entirely, never read stdin, and run only the
  cleanup: switch to the target branch, fast-forward sync, delete
  merged local branches, and prune stale remote-tracking references.
  This is what ``vrg-release`` invokes (issue #1448).

Output renders through the stage-aware progress framework
(issue #1479): standalone TTY runs get the live display with collapsed
per-stage status lines and a run log at
``.vergil/vrg-finalize-pr-<stamp>.log``; piped runs fall back to the
plain renderer. ``vrg-release`` passes ``--output-format plain``
explicitly because two live displays cannot nest (issue #1470).

After cleanup succeeds, validation runs in the dev container, then the
most recent CD workflow run on the target branch is checked and the
command fails if it did not succeed (issue #303 — docs publish is async
and used to fail silently).

``--release`` chains straight into ``vrg-release`` after the finalize
pipeline completes **successfully** (issue #1634) — the fast-turnaround
cascade that merges, cleans up, and cuts a release in one command. The
chain runs only on success, and only after ``run_pipeline`` has torn down
its live display, so the two renderers never nest (cf. issue #1470). It
runs as a subprocess, not ``exec``, so the caller (e.g. ``vrg-submit-pr``)
regains control to report how far the cascade got.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vergil_tooling.lib import (
    config,
    git,
    github,
    pr_merge,
    pr_provenance,
    progress,
    worktrees,
)
from vergil_tooling.lib.confirm import add_yes_argument, confirm
from vergil_tooling.lib.container_cache import clean_branch_images
from vergil_tooling.lib.pr_workflow import batch
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.repo_init import prompt_choice

if TYPE_CHECKING:
    from pathlib import Path

_CD_WORKFLOW_NAME = "CD"

_ETERNAL_BY_MODEL: dict[str, list[str]] = {
    "docs-single-branch": ["develop"],
    "library-release": ["develop", "main"],
    "application-promotion": ["develop", "release", "main"],
}


def _parse_pr_list(value: str) -> list[str]:
    """Split a comma-separated PR argument into trimmed, non-empty tokens."""
    return [tok.strip() for tok in value.split(",") if tok.strip()]


def _resolve_open_prs(root: Path) -> list[str]:
    """Return the URLs of every open PR in canonical worktrees, branch-sorted.

    Deterministic order (by branch name) so a batch is reproducible. Skips
    worktrees with no open PR, printing why — no silent exclusions.
    """
    urls: list[str] = []
    for wt in sorted(worktrees.list_worktrees(root), key=lambda w: w.branch):
        pr = github.pr_for_branch(wt.branch)
        if pr is None:
            print(f"  {wt.path.name}: no open PR for {wt.branch} — skipping")
            continue
        urls.append(pr["url"])
    return urls


def _run_finalize_batch(
    prs: list[str],
    *,
    root: Path,
    release: bool,
    install: bool,
    assume_yes: bool,
) -> int:
    """Finalize *prs* serially, fail-fast, then validate + release once.

    Each item shells out to ``vrg-finalize-pr <pr> --skip-post-checks`` (merge
    + cleanup, no validation). On full success, one end-of-batch
    ``vrg-finalize-pr --cleanup-only`` runs validation + the CD check, then a
    single ``vrg-release [--install]`` if requested (issue #1673).
    """

    def _finalize_item(pr: str) -> None:
        result = subprocess.run(  # noqa: S603
            ("vrg-finalize-pr", pr, "--skip-post-checks"),  # noqa: S607
            cwd=root,
            check=False,
        )
        if result.returncode != 0:
            msg = f"vrg-finalize-pr {pr} --skip-post-checks exited {result.returncode}"
            raise batch.BatchAbortError(msg)

    def _validate() -> None:
        result = subprocess.run(  # noqa: S603
            ("vrg-finalize-pr", "--cleanup-only"),  # noqa: S607
            cwd=root,
            check=False,
        )
        if result.returncode != 0:
            raise batch.BatchAbortError(f"end-of-batch validation exited {result.returncode}")

    def _release() -> None:
        cmd = ("vrg-release", "--install") if install else ("vrg-release",)
        result = subprocess.run(cmd, cwd=root, check=False)  # noqa: S603,S607
        if result.returncode != 0:
            raise batch.BatchAbortError(f"{' '.join(cmd)} exited {result.returncode}")

    post_steps = [batch.PostStep("validation", _validate)]
    if release:
        post_steps.append(batch.PostStep("release", _release))

    plan = [f"finalize PR {pr}" for pr in prs]
    plan.append("then: validate develop once" + (", then release" if release else ""))

    report = batch.run_batch(
        prs,
        _finalize_item,
        label=lambda pr: f"PR {pr}",
        plan=plan,
        assume_yes=assume_yes,
        post_steps=post_steps,
    )
    print(batch.format_report(report))
    return 0 if report.all_merged and report.post_failure is None else 1


class FinalizeError(Exception):
    """Stage failure with a human-readable reason.

    Raised by stage functions to mark the stage failed; run_pipeline
    records the message in the stage result and the final summary.
    """


@dataclass
class FinalizeContext:
    """State threaded through the pipeline stages."""

    args: argparse.Namespace
    root: Path
    merged_branch: str | None = None
    deleted: list[str] = field(default_factory=list)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Finalize a pull request.")
    # A PR argument and --cleanup-only contradict each other: the flag
    # promises no merge and no prompts, the argument requests a merge.
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "pr",
        nargs="?",
        default=None,
        help="PR number or URL to merge and finalize. Omit to infer interactively.",
    )
    target.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Skip PR inference and merge; run cleanup without prompting "
        "or reading stdin (non-interactive release path).",
    )
    target.add_argument(
        "--all",
        dest="all_prs",
        action="store_true",
        help="Finalize every open PR found in .worktrees/ as a serial batch (issue #1673).",
    )
    parser.add_argument("--target-branch", default="develop", help="Target branch to switch to")
    parser.add_argument(
        "--strategy",
        default=None,
        choices=["merge", "squash", "rebase"],
        help=(
            "Merge strategy for the PR. Default by branch prefix: 'release/*' "
            "branches merge with a merge commit (preserve develop<->main "
            "ancestry); all others squash."
        ),
    )
    parser.add_argument(
        "--allow-provenance-violation",
        action="store_true",
        help="Proceed despite provenance violations (conscious human override)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="After a successful finalize, chain straight into vrg-release "
        "(the full submit-finalize-release cascade)",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Extend the cascade one step further: implies --release and passes "
        "--install through, so vrg-release runs the consumer-refresh install "
        "step (issue #1643)",
    )
    parser.add_argument(
        "--skip-post-checks",
        action="store_true",
        help="Skip the post-merge validation and CD-status stages (and any "
        "release chain). Used by the batch orchestrator, which runs those "
        "once at the end of the batch (issue #1673).",
    )
    add_yes_argument(parser)
    progress.add_progress_args(parser, ())
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


def _delete_branch_and_worktree(branch: str, root: Path, *, dry_run: bool) -> bool:
    """Remove *branch* and its canonical worktree; True if the branch was deleted.

    Shared by the explicit-target step (the PR branch just merged, which
    a squash merge hides from ``git branch --merged``) and the ancestry
    sweep for stragglers.

    If the branch is still checked out in a `.worktrees/` worktree
    (typical: the worktree we did the PR's work in), `git branch -D`
    refuses to delete it — there's no force past "branch is checked out
    somewhere." Auto-remove the worktree first, constrained to the
    canonical `.worktrees/` location so user-created worktrees elsewhere
    are never silently removed. Issue #315.

    `git branch -D` (force) rather than `-d` because the callers have
    already vetted these branches as merged; `-d`'s redundant safety
    check rejects branches whose tips were rewritten by rebase +
    force-push during a CI fixup loop (the upstream-tracking ref is gone
    after `fetch --prune`). Trusting our own filter avoids the
    disagreement. Issue #307.
    """
    wt = worktrees.worktree_for_branch(branch, root)
    if wt is not None:
        if _worktree_is_dirty(wt):
            print(f"  Skipping {branch}: worktree {wt} has uncommitted changes")
            return False
        print(f"  Removing worktree: {wt}")
        _run(["worktree", "remove", str(wt)], dry_run=dry_run)
    print(f"  Deleting merged branch: {branch}")
    _run(["branch", "-D", branch], dry_run=dry_run)
    if not dry_run:
        removed = clean_branch_images(branch)
        if removed:
            print(f"  Cleaned {removed} cached container image(s) for {branch}")
    return True


def _infer_pr(root: Path, target_branch: str, *, assume_yes: bool = False) -> str | None:
    """Resolve which PR to finalize when none was given; always confirm.

    Returns the PR URL to finalize, or None when the user confirmed
    cleanup-only. Raises SystemExit(0) on decline, and SystemExit with
    a message via require_tty when stdin is not interactive — these
    prompts are the human touch point of the workflow, and the
    explicit-PR argument is the scriptable path.

    With *assume_yes* (``--yes``) the single-PR finalize confirm and the
    cleanup-only confirm are pre-answered "yes". The multiple-PR choice is
    a disambiguation, not a yes/no, so it is still presented even under
    ``--yes`` — there is no single obvious answer to skip to.
    """
    pairs: list[tuple[worktrees.Worktree, dict[str, str]]] = []
    for wt in worktrees.list_worktrees(root):
        pr = github.pr_for_branch(wt.branch)
        if pr is None:
            # No silent exclusions: every skipped worktree says why.
            print(f"  {wt.path.name}: no open PR for {wt.branch} — not a candidate")
            continue
        pairs.append((wt, pr))

    # A prompt is unavoidable only when more than one PR forces a choice;
    # every other interaction here is a single yes/no that --yes pre-answers.
    # Demand a TTY only when an interactive prompt will actually be shown.
    must_disambiguate = len(pairs) > 1
    if must_disambiguate or not assume_yes:
        worktrees.require_tty("vrg-finalize-pr without a PR argument")

    if not pairs:
        confirmed = confirm(
            f"No open PRs found in worktrees. Run cleanup only (switch to "
            f"{target_branch}, sync, prune branches/worktrees)?",
            assume_yes=assume_yes,
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

    if not confirm(
        f"Finalize PR #{pr['number']} ({pr['title']})?",
        assume_yes=assume_yes,
        default=False,
    ):
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


def _stage_provenance(ctx: FinalizeContext) -> None:
    """Pre-merge provenance check (issue #1289)."""
    args = ctx.args
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
            msg = (
                "provenance violations found — re-run with "
                "--allow-provenance-violation to override consciously"
            )
            raise FinalizeError(msg)
        print(
            "  Overriding provenance violations per --allow-provenance-violation.",
            file=sys.stderr,
        )


_RELEASE_BRANCH_PREFIX = "release/"


def _resolve_strategy(branch: str, override: str | None) -> str:
    """Merge strategy for *branch*.

    An explicit ``--strategy`` always wins. Otherwise the default is driven by
    the branch prefix: ``release/*`` branches (release PRs and back-merges)
    merge with a merge commit to preserve the develop<->main ancestry the
    release model relies on; squashing them silently breaks that ancestry and
    makes later release branches conflict (issue #1620). Every other branch
    squashes, as before.
    """
    if override is not None:
        return override
    return "merge" if branch.startswith(_RELEASE_BRANCH_PREFIX) else "squash"


def _stage_merge(ctx: FinalizeContext) -> None:
    """Merge the PR (or confirm it is already merged) and record its branch."""
    args = ctx.args
    branch = github.head_ref(args.pr)
    strategy = _resolve_strategy(branch, args.strategy)
    if github.pr_state(args.pr) == "MERGED":
        print(f"PR {args.pr} already merged.")
    elif args.dry_run:
        print(f"  [dry-run] wait for green, then merge PR {args.pr} (--{strategy})")
    else:
        try:
            pr_merge.wait_and_merge(args.pr, strategy=strategy)
        except pr_merge.MergeAbortError as exc:
            raise FinalizeError(str(exc)) from exc
    ctx.merged_branch = branch


def _stage_cleanup(ctx: FinalizeContext) -> None:
    """Switch to the target branch, sync, and prune merged branches,
    worktrees, and remote-tracking references."""
    args = ctx.args
    root = ctx.root

    try:
        vergil_config = config.read_config(root)
        model = vergil_config.project.branching_model
    except FileNotFoundError:
        model = ""
    except config.ConfigError as exc:
        raise FinalizeError(str(exc)) from exc

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
    # ff-merge the remote-tracking ref instead of `pull`: FETCH_HEAD is
    # written non-atomically, so a `pull` racing a concurrent fetch can
    # see two merge candidates and abort (issue #1499). The
    # remote-tracking ref is updated atomically.
    _run(["merge", "--ff-only", f"origin/{args.target_branch}"], dry_run=args.dry_run)

    deleted = ctx.deleted

    # Explicit-target cleanup: the just-merged PR branch. The default
    # squash strategy rewrites history onto the target, so the branch is
    # never an ancestor and `git branch --merged` cannot see it — without
    # this step the flagship flow would merge and then silently fail to
    # clean up the very worktree it inferred the PR from.
    if ctx.merged_branch and ctx.merged_branch not in eternal:
        if git.read_output("branch", "--list", ctx.merged_branch):
            print(f"Cleaning up merged PR branch {ctx.merged_branch}...")
            if _delete_branch_and_worktree(ctx.merged_branch, root, dry_run=args.dry_run):
                deleted.append(ctx.merged_branch)
        else:
            print(f"  Merged PR branch {ctx.merged_branch} has no local branch — skipping.")

    # Straggler sweep. Candidates come from two sources, because a squash
    # merge rewrites the branch's work onto the target as a new commit:
    # the branch tip is never an ancestor, so `git branch --merged` (the
    # ancestry arm) cannot see squash-merged branches (issue #1552). The
    # worktree arm closes that gap by classifying every canonical worktree
    # with the same logic that backs vrg-worktree-status, so "cruft" and
    # "removed" match by construction.
    print("Checking for merged local branches...")
    worktree_by_branch = {wt.branch: wt for wt in worktrees.list_worktrees(root)}

    # Worktree arm (squash-merge-aware). classify_worktree's removable
    # verdict (MERGED/CLOSED and not dirty) replaces the ancestry guards
    # here and is race-safe against parallel agents (issue #1445): a branch
    # with no merged/closed PR — including a freshly created one — is never
    # removable.
    for branch, wt in worktree_by_branch.items():
        if branch in eternal or branch in deleted:
            continue
        status = worktrees.gather_worktree_status(wt, target=args.target_branch)
        if not status.removable:
            print(f"  Skipping {branch}: {status.state.value} (not removable)")
            continue
        if _delete_branch_and_worktree(branch, root, dry_run=args.dry_run):
            deleted.append(branch)

    # Ancestry arm. Catches branches with no canonical worktree (worktree
    # already gone, or merge-commit/rebase merges whose tip IS an ancestor).
    # `git branch --merged` classifies a branch as merged when its tip is an
    # ancestor of the target — which a branch just created from the target's
    # tip satisfies trivially, so the two guards below (issue #1445) gate the
    # removal as strictly as the worktree arm above.
    for branch in git.merged_branches(args.target_branch):
        if branch in eternal or branch in deleted or branch in worktree_by_branch:
            continue
        # Guard 1 — skip zero-commit branches. A tip equal to the
        # target's carries no merged work, so deleting it saves nothing;
        # it is also exactly what an in-flight branch looks like before
        # its first commit.
        if git.commit_sha(branch) == git.commit_sha(args.target_branch):
            print(
                f"  Skipping {branch}: tip matches {args.target_branch} "
                "(zero-commit branch, nothing to clean up)"
            )
            continue
        # Guard 2 — require merge evidence. Ancestry alone cannot
        # distinguish a merged branch from one created off an older
        # target tip; only sweep branches whose head has a closed or
        # merged PR. The just-merged PR branch is handled by the
        # explicit-target step above, which keeps its own behavior.
        if github.closed_pr_for_branch(branch) is None:
            print(f"  Skipping {branch}: no closed or merged PR for this branch")
            continue
        if _delete_branch_and_worktree(branch, root, dry_run=args.dry_run):
            deleted.append(branch)

    print("Pruning stale remote-tracking references...")
    if args.dry_run:
        print("  [dry-run] git remote prune origin")
    else:
        git.run("remote", "prune", "origin")

    # -- working-tree cleanliness gate (issue #472) ----------------------------
    if not args.dry_run:
        dirty = git.working_tree_status()
        if dirty:
            print(
                f"ERROR: {args.target_branch} working tree is not clean.",
                file=sys.stderr,
            )
            for line in dirty.splitlines():
                print(f"  {line}", file=sys.stderr)
            msg = (
                f"{args.target_branch} working tree is not clean — "
                "clean up these files before starting the next issue"
            )
            raise FinalizeError(msg)

    # Replaces the old end-of-main summary block: the framework owns the
    # run footer, so the branch/deleted/pruned details live here where
    # they still reach the renderer and the run log.
    print(
        f"Cleanup complete: branch {args.target_branch}; "
        f"deleted {' '.join(deleted) if deleted else '(none)'}; remotes pruned."
    )


def _stage_validation(ctx: FinalizeContext) -> None:
    """Run canonical validation to catch problems on the target branch
    before the next PR is created.

    Streams through ``progress.run`` — a bare ``subprocess.run`` with
    inherited stdout would write raw lines under the live display and
    strand stale frames (issue #1470).
    """
    args = ctx.args
    if args.dry_run:
        print("  [dry-run] vrg-container-run -- [uv run] vrg-validate")
        return
    print("Running post-finalization validation via vrg-container-run...")
    if (ctx.root / "pyproject.toml").is_file():
        cmd: tuple[str, ...] = ("vrg-container-run", "--", "uv", "run", "vrg-validate")
    else:
        cmd = ("vrg-container-run", "--", "vrg-validate")
    try:
        progress.run(cmd)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"post-finalization validation failed (exit {exc.returncode}) — "
            f"fix {args.target_branch} before creating the next PR"
        )
        raise FinalizeError(msg) from exc


def _stage_cd_check(ctx: FinalizeContext) -> None:
    """Docs-publish sanity check (issue #303). CD is async relative to the
    merge that triggers it, so a failure doesn't block any PR — but it
    means the site or release artifacts may be stale."""
    args = ctx.args
    if args.dry_run:
        print("  [dry-run] check most recent CD workflow run")
        return
    failure = _check_cd_workflow_status(args.target_branch)
    if failure is None:
        return
    print("ERROR: most recent CD workflow run did not succeed.", file=sys.stderr)
    print(f"  {failure}", file=sys.stderr)
    print(
        "  CD workflow is async — investigate before the next merge so",
        file=sys.stderr,
    )
    print("  the site doesn't drift further from develop.", file=sys.stderr)
    raise FinalizeError("most recent CD workflow run did not succeed")


def build_stages(*, include_pr: bool, include_post_checks: bool = True) -> tuple[Stage, ...]:
    """Assemble the pipeline for the resolved mode.

    provenance/merge run only when a PR was given or inferred; cleanup always
    runs. validation and cd-check run unless *include_post_checks* is False
    (the batch path defers them to one end-of-batch run, issue #1673); they
    are fail_defer so a validation failure still surfaces the CD status —
    matching the pre-pipeline control flow.
    """
    stages: list[Stage] = []
    if include_pr:
        stages.append(Stage("provenance", _stage_provenance, "fail_fast"))
        stages.append(Stage("merge", _stage_merge, "fail_fast"))
    stages.append(Stage("cleanup", _stage_cleanup, "fail_fast"))
    if include_post_checks:
        stages.append(Stage("validation", _stage_validation, "fail_defer"))
        stages.append(Stage("cd-check", _stage_cd_check, "fail_defer"))
    return tuple(stages)


def _chain_release(root: Path, *, install: bool = False) -> int:
    """Hand off to ``vrg-release`` after a successful finalize (issue #1634).

    Runs only when the finalize pipeline succeeded. By the time
    ``run_pipeline`` returns it has closed its live display (its ``finally``
    block calls ``renderer.close()``), so ``vrg-release`` starts a fresh
    renderer with no nesting — the two live displays never overlap
    (cf. issue #1470). Run as a subprocess inheriting the TTY, not ``exec``,
    so a caller like ``vrg-submit-pr`` regains control to report the
    cascade outcome.

    With *install*, passes ``--install`` through so ``vrg-release`` also runs
    the consumer-refresh install step (issue #1643).

    A failure here never un-merges the PR — it was already merged and
    cleaned up — so report it clearly and let the human re-run vrg-release
    alone.
    """
    cmd: tuple[str, ...] = ("vrg-release", "--install") if install else ("vrg-release",)
    print()
    print(f"--{'install' if install else 'release'}: handing off to {' '.join(cmd)}")
    result = subprocess.run(  # noqa: S603
        cmd,  # noqa: S607
        cwd=root,
        check=False,
    )
    if result.returncode != 0:
        rerun = "vrg-release --install" if install else "vrg-release"
        print(
            f"vrg-finalize-pr: release failed (exit {result.returncode}); "
            "the PR was already merged and cleaned up and is unaffected.\n"
            f"  Re-run the release alone with: {rerun}",
            file=sys.stderr,
        )
    return result.returncode


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # --install extends the cascade one step past --release; normalize once so
    # the release-chain logic below sees release on (issue #1643).
    if args.install:
        args.release = True

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

    # Batch mode (issue #1673): an explicit comma-list or --all finalizes
    # several PRs serially. A single PR (or none) falls through to the
    # unchanged single-PR pipeline below.
    if args.all_prs:
        prs = _resolve_open_prs(root)
        if not prs:
            print("vrg-finalize-pr --all: no open PRs found in worktrees.")
            return 0
        return _run_finalize_batch(
            prs, root=root, release=args.release, install=args.install, assume_yes=args.yes
        )
    if args.pr is not None and "," in args.pr:
        prs = _parse_pr_list(args.pr)
        return _run_finalize_batch(
            prs, root=root, release=args.release, install=args.install, assume_yes=args.yes
        )

    # --cleanup-only is the scriptable release path: no inference, no
    # prompts, no stdin reads — args.pr stays None and only the
    # cleanup/validation/cd-check stages run (issue #1448).
    if args.pr is None and not args.cleanup_only:
        try:
            args.pr = _infer_pr(root, args.target_branch, assume_yes=args.yes)
        except SystemExit as exc:
            if exc.code == 0:
                return 0
            raise

    # Inference and its prompts above need the real TTY; everything
    # below runs under the progress pipeline, which owns stdout/stderr
    # (issue #1479).
    ctx = FinalizeContext(args=args, root=root)
    rc = progress.run_pipeline(
        ctx,
        build_stages(
            include_pr=args.pr is not None,
            include_post_checks=not args.skip_post_checks,
        ),
        command="vrg-finalize-pr",
        label="vrg-finalize-pr",
        args=args,
        repo_root=root,
    )

    # --release cascade (issue #1634): chain into vrg-release only after a
    # clean finalize. A non-zero pipeline must not trigger a release, and the
    # hand-off happens here — after run_pipeline has closed its live display —
    # so the two renderers never nest (cf. issue #1470). --skip-post-checks
    # never chains: release is the batch orchestrator's single end-of-batch
    # step (issue #1673).
    if rc != 0 or not args.release or args.skip_post_checks:
        return rc
    if args.dry_run:
        target = "vrg-release --install" if args.install else "vrg-release"
        print(f"\n[dry-run] would chain into: {target}")
        return 0
    return _chain_release(root, install=args.install)


if __name__ == "__main__":
    sys.exit(main())
