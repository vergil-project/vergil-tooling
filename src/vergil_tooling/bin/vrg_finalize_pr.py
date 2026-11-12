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

A PR whose body has more than one ``Closes`` line is a *batch PR*, one
commit per issue (epic vergil-project/.github#1019). Finalize cross-checks
its ``Closes`` issues against GitHub's closing references, checks the
commit-per-issue invariant, updates it by rebase (never by merging the base
in) when it is behind, re-checks it after every update and right before the
merge, and rebase-merges it pinned to the head it checked. ``--strategy
squash`` is refused on a batch PR. Every other PR merges as before.

Output renders through the stage-aware progress framework
(issue #1479): standalone TTY runs get the live display with collapsed
per-stage status lines and a run log at
``.vergil/vrg-finalize-pr-<stamp>.log``; piped runs fall back to the
plain renderer. ``vrg-release`` passes ``--output-format plain``
explicitly because two live displays cannot nest (issue #1470).

After cleanup succeeds, validation runs in the dev container, then
finalize blocks on the CD workflow run its merge triggered — identified
by the merge commit SHA, not "latest run on branch" — and fails if that
run does not succeed (issue #2753; supersedes the non-blocking #303
check). The watch is bounded by ``$VRG_CD_TIMEOUT`` (default 5400s); a run
still in progress at the deadline is reported as "result unknown", never as a
failed run, and still fails the stage (issue #3316). A run cancelled because a
concurrency group gave its place to a higher-priority run is reported as
"superseded", a loud non-fatal warning, never as a failure; any other
cancellation still fails (issue #3834). ``--no-wait-cd`` is the
conscious opt-out from blocking.

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
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vergil_tooling.lib import (
    branch_names,
    config,
    git,
    github,
    linkage,
    pr_merge,
    pr_provenance,
    progress,
    worktrees,
)
from vergil_tooling.lib.cache_prune import clean_branch_images, prune_orphan_branch_images
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.confirm import add_yes_argument, confirm
from vergil_tooling.lib.container import resolve_language
from vergil_tooling.lib.container_cache import provision_dev_image
from vergil_tooling.lib.live_branches import RepoSetError, default_repo_roots, discover_repos
from vergil_tooling.lib.pr_workflow import batch, batch_rules, github_transport
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.release import gate
from vergil_tooling.lib.repo_init import prompt_choice
from vergil_tooling.lib.versions import VersionsError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CD_WORKFLOW_NAME = "CD"

# CD is dispatched asynchronously by the base-branch push the merge produces,
# so the run may not be registered the instant finalize looks. Poll up to
# _CD_DISPATCH_TIMEOUT_SECS for the run whose headSha matches the merge commit
# to appear; if none has by then, treat it as "this change triggered no CD run"
# (path filters / skip) and move on without failing. Once found, watch it up to
# cd_timeout_secs() before bailing with a timeout so finalize never hangs
# indefinitely. _CD_POLL_INTERVAL_SECS paces both loops.
_CD_DISPATCH_TIMEOUT_SECS = 120
# Default CD watch ceiling: 90 minutes. The longest known CD is vergil-containers'
# (rust image jobs alone take ~40 min); run 38973347564 took 43m31s and was
# misreported as a failure under the old fixed 1800s (issue #3316). 90 min is
# roughly 2x that run — headroom for a slow-runner day without letting a truly
# hung run block finalize for hours. A timeout is "result unknown", not a CD
# failure, and is cheap to recover from (re-check the run, then re-run
# ``vrg-finalize-pr --cleanup-only``); override per invocation with
# $VRG_CD_TIMEOUT, mirroring $VRG_CHECKS_TIMEOUT for the merge-gate wait.
_CD_WATCH_TIMEOUT_SECS = 5400
CD_TIMEOUT_ENV = "VRG_CD_TIMEOUT"
_CD_POLL_INTERVAL_SECS = 10

_ETERNAL_BY_MODEL: dict[str, list[str]] = {
    "docs-single-branch": ["develop"],
    "library-release": ["develop", "main"],
    "application-promotion": ["develop", "release", "main"],
}


@cli_main
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

    # A release cascade on a repo that is not released is refused before any
    # merge or cleanup, so nothing changes (issue #4175).
    if args.release:
        refusal = gate.unreleased_refusal(root)
        if refusal is not None:
            flag = "--install" if args.install else "--release"
            print(f"vrg-finalize-pr {flag}: {refusal}", file=sys.stderr)
            return 1

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

    # Surface — and, with --clean-dirty, guardedly clear — any merged/closed
    # worktrees the sweep could not remove (issue #2348). Runs after the
    # pipeline has torn down its live display so the report (and the
    # confirmation prompt) reaches the human on real stdio, and regardless of
    # rc so a stuck worktree is never hidden behind a failed later stage.
    _handle_stuck_worktrees(ctx)

    # Task closure and epic rollup are event-driven now (epic
    # vergil-project/.github#75): the PR's `Closes #N` linkage closes the task on
    # merge, and the on: issues.closed rollup Action closes the parent epic. The
    # old post-pipeline `_close_managed_task` call was removed here in T4. The
    # close-refs stage is the backstop for an auto-close GitHub skipped
    # (issue #4002).

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
        "--clean-dirty",
        action="store_true",
        help="Opt-in: clear a merged/closed worktree whose only dirt is "
        "untracked build/validation output, after showing exactly what will "
        "be deleted and confirming. Never touches modified tracked files or a "
        "reused-branch straggler with unmerged commits (issue #2348).",
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
    parser.add_argument(
        "--no-wait-cd",
        action="store_true",
        help="Do not block on the CD workflow run the merge triggers. By "
        "default finalize waits for that run to complete and fails if it does "
        "not succeed (issue #2753); this is the conscious opt-out for the rare "
        "case where blocking on the post-merge publish is not wanted.",
    )
    add_yes_argument(parser)
    progress.add_progress_args(parser, ())
    return parser.parse_args(argv)


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

    def _release() -> None:
        cmd = ("vrg-release", "--install") if install else ("vrg-release",)
        result = subprocess.run(cmd, cwd=root, check=False)  # noqa: S603,S607
        if result.returncode != 0:
            raise batch.BatchAbortError(f"{' '.join(cmd)} exited {result.returncode}")

    post_steps = [batch.finalize_post_checks_step(root)]
    if release:
        post_steps.append(batch.PostStep("release", _release))

    plan = [f"finalize PR {pr}" for pr in prs]
    plan.append(batch.POST_CHECKS_PLAN_LINE + (", then release" if release else ""))

    report = batch.run_batch(
        prs,
        _finalize_item,
        label=lambda pr: f"PR {pr}",
        plan=plan,
        assume_yes=assume_yes,
        completed=batch.ItemOutcome.MERGED,
        post_steps=post_steps,
    )
    print(batch.format_report(report))
    return 0 if report.all_completed and report.post_failure is None else 1


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
    # SHA of the commit this merge produced on the target branch (mergeCommit
    # oid). The CD workflow the merge triggers runs on this SHA, so the cd-check
    # stage waits on exactly that run rather than "latest run on branch"
    # (issue #2753). None until _stage_merge records it (and on dry-run).
    merge_commit_sha: str | None = None
    deleted: list[str] = field(default_factory=list)
    # Merged/closed worktrees the sweep could not remove — dirty, or a
    # reused branch name with unmerged commits (issue #1719). Collected in
    # _stage_cleanup and surfaced (and optionally cleared) after the pipeline
    # tears down its live display, so the skip reason is impossible to miss
    # instead of buried in the collapsed stage log (issue #2348).
    stuck: list[worktrees.WorktreeStatus] = field(default_factory=list)


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


def _delete_relay_ref(branch: str, *, dry_run: bool) -> None:
    """Drop *branch*'s pr-workflow relay ref alongside the branch (issue #2369).

    ``report-ready`` on a cloud VM parks the PR-handoff payload at
    ``refs/vergil/pr-workflow/<branch>`` (epic #146). Once the branch is
    cleaned up that ref is orphaned, so delete it here. ``delete()`` is a
    no-op when the ref is absent — the common Mac case, where the local-file
    transport was used and no relay ref was ever pushed — so calling it
    unconditionally is safe.
    """
    if dry_run:
        print(f"  [dry-run] delete relay ref for {branch}")
        return
    github_transport.GitHubTransport(branch).delete()


def _prune_orphan_relay_refs(*, dry_run: bool) -> None:
    """Prune relay refs whose branch no longer exists (issue #2369).

    The per-branch cleanup already drops the relay ref alongside the branch it
    deletes. This is the swept safety net: a relay ref whose branch was removed
    out-of-band — merged and branch-pruned on the remote, or abandoned — would
    otherwise linger forever in the reserved namespace. Both reads are
    read-only ``ls-remote`` calls; under ``--dry-run`` no ref is touched.
    """
    if dry_run:
        print("  [dry-run] prune orphaned pr-workflow relay refs")
        return
    live = git.remote_branches()
    for branch in github_transport.list_relay_branches():
        if branch in live:
            continue
        print(f"  Deleting orphaned relay ref for {branch} (branch no longer exists)")
        github_transport.GitHubTransport(branch).delete()


_IMAGE_FAILURE_HINT = "remove it by hand, or with vrg-container-cache"


def _warn_image_failures(failures: list[tuple[str, str]]) -> None:
    """Report each failed cached-image ``rmi`` loudly, without failing finalize.

    Image cleanup runs after the merge has already succeeded, so a refused
    ``rmi`` must not fail the command — but it must not vanish either (#3566).
    """
    for ref, error in failures:
        print(
            f"  WARNING: could not remove cached image {ref}: {error} ({_IMAGE_FAILURE_HINT})",
            file=sys.stderr,
        )


def _clean_branch_images(branch: str, root: Path) -> None:
    """Remove *branch*'s cached dev images of *root*'s repo, warning on any failure (#3566)."""
    try:
        removed, failures = clean_branch_images(branch, root)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(
            f"  WARNING: could not clean cached images for {branch}: "
            f"{' '.join(error_text(exc).split())}",
            file=sys.stderr,
        )
        return
    if removed:
        print(f"  Cleaned {removed} cached container image(s) for {branch}")
    _warn_image_failures(failures)


def _prune_orphan_branch_images(*, dry_run: bool) -> None:
    """Prune cached dev images whose branch is finished everywhere (#2600, #3566).

    The per-branch cleanup already prunes the finalized branch's cached image
    (``clean_branch_images``). This is the swept safety net for the branch that
    was never finalized — abandoned, or its PR closed out of band — whose
    ~1.3–2G cached image would otherwise linger and fill the VM disk.

    The tag names its repository only by directory name (#4162) and every repo
    of a language shares the image name, so "live" is asked of the whole
    platform repo set, exactly as ``vrg-container-cache prune`` asks it — never
    of the finalized repo alone, which would delete every other repo's
    in-progress images. Where no repo set
    can be confirmed (the physical host has no default) the sweep is skipped
    and says so; a failed lookup skips it with a warning. Neither fails
    finalize: the merge has already landed.
    """
    if dry_run:
        print("  [dry-run] prune orphaned cached dev images")
        return
    try:
        roots = default_repo_roots()
        repos = discover_repos(roots)
    except RepoSetError as exc:
        print(f"  Skipping the orphaned-image sweep: {exc}")
        print("  Sweep by hand with: vrg-container-cache prune --root DIR")
        return
    if not repos:
        where = ", ".join(str(r) for r in roots)
        print(
            f"  WARNING: no git repositories under {where}; skipping the orphaned-image "
            "sweep rather than pruning against an empty repo set.",
            file=sys.stderr,
        )
        return
    try:
        removed, failures = prune_orphan_branch_images(repos)
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(
            f"  WARNING: skipped the orphaned-image sweep: {' '.join(error_text(exc).split())}",
            file=sys.stderr,
        )
        return
    if removed:
        print(f"  Pruned {removed} orphaned cached container image(s)")
    _warn_image_failures(failures)


def _delete_branch_and_worktree(
    branch: str, root: Path, *, dry_run: bool, force: bool = False
) -> bool:
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

    ``force`` is the opt-in ``--clean-dirty`` path (issue #2348): it skips
    the dirty guard and passes ``git worktree remove --force`` so a
    worktree dirtied only by untracked build output is discarded. Callers
    must have already vetted that the dirt is untracked-only and the PR
    merged/closed — the default (``force=False``) still refuses any dirty
    worktree, so the standing guards are unweakened.
    """
    wt = worktrees.worktree_for_branch(branch, root)
    if wt is not None:
        if not force and _worktree_is_dirty(wt):
            print(f"  Skipping {branch}: worktree {wt} has uncommitted changes")
            return False
        remove_cmd = ["worktree", "remove", *(["--force"] if force else []), str(wt)]
        print(f"  Removing worktree: {wt}")
        _run(remove_cmd, dry_run=dry_run)
    print(f"  Deleting merged branch: {branch}")
    _run(["branch", "-D", branch], dry_run=dry_run)
    if not dry_run:
        _clean_branch_images(branch, root)
    # Drop the branch's relay ref too, so a cloud-handoff ref never outlives the
    # branch it belonged to (issue #2369). Shared by both cleanup paths.
    _delete_relay_ref(branch, dry_run=dry_run)
    return True


def _worktree_porcelain(wt_path: Path) -> str:
    """Return ``git status --porcelain`` for *wt_path*, or '' if git fails.

    Used by the guarded clear to inspect *what* the dirt is. An empty string
    — whether a clean tree or a git error — makes ``_dirt_is_untracked_only``
    refuse, the safe direction: never delete on unproven-clean evidence.
    """
    result = subprocess.run(  # noqa: S603
        ("git", "-C", str(wt_path), "status", "--porcelain"),  # noqa: S607
        check=False,
        text=True,
        capture_output=True,
    )
    return result.stdout if result.returncode == 0 else ""


def _dirt_is_untracked_only(porcelain: str) -> bool:
    """True when every uncommitted path in *porcelain* is an untracked file.

    ``git status --porcelain`` marks untracked files with a ``??`` status
    code; every other code (`` M``, ``M ``, ``A ``, ``D ``, ``R ``, …) is a
    tracked-file change the guarded clear must never discard. An empty
    porcelain returns ``False`` — there is nothing to clear, and callers
    reach this only for a tree already known dirty.
    """
    lines = [ln for ln in porcelain.splitlines() if ln.strip()]
    return bool(lines) and all(ln.startswith("??") for ln in lines)


_CLEARABLE_STATES = (worktrees.WorktreeState.MERGED, worktrees.WorktreeState.CLOSED)


def _clean_dirty_worktrees(
    stuck: list[worktrees.WorktreeStatus], root: Path, *, dry_run: bool, assume_yes: bool
) -> list[worktrees.WorktreeStatus]:
    """Guarded opt-in clear of untracked-only dirt (issue #2348).

    Runs after the pipeline, on real stdio, so its confirmation prompt reaches
    the human (a stage's stdout is redirected into the collapsed log). The
    guards are strict and never weakened:

    - A reused-branch straggler classifies as NO_PR/DRAFT (its merged verdict
      is withheld, issue #1719), so it is never in a clearable state here and
      its unmerged commits are never at risk.
    - Only a MERGED/CLOSED worktree whose *every* uncommitted path is untracked
      is a candidate; a modified tracked file is refused.
    - Each removal shows exactly what will be deleted and is confirmed.

    Returns the worktrees left in place (refused, or declined), so the caller
    surfaces only what still needs attention.
    """
    remaining: list[worktrees.WorktreeStatus] = []
    for status in stuck:
        branch = status.worktree.branch
        if status.state not in _CLEARABLE_STATES:
            print(
                f"  --clean-dirty: refusing {branch}: not a merged/closed worktree "
                "(branch name reused — has unmerged commits, issue #1719)."
            )
            remaining.append(status)
            continue
        porcelain = _worktree_porcelain(status.worktree.path)
        if not _dirt_is_untracked_only(porcelain):
            print(
                f"  --clean-dirty: refusing {branch}: working tree has modified "
                "tracked files — not untracked-only build output."
            )
            remaining.append(status)
            continue
        paths = [ln[3:].strip() for ln in porcelain.splitlines() if ln.strip()]
        print(f"  --clean-dirty: {branch} ({status.state.value}) — would remove the worktree and:")
        for path in paths:
            print(f"      untracked: {path}")
        # In dry-run the confirm is skipped and _delete_branch_and_worktree
        # (dry_run=True) prints the git commands it would run; otherwise
        # confirm gates the real, irreversible removal.
        if not dry_run and not confirm(
            f"Clear worktree {status.worktree.path.name} and delete {branch}?",
            assume_yes=assume_yes,
            default=False,
        ):
            print(f"  --clean-dirty: left {branch} in place.")
            remaining.append(status)
            continue
        _delete_branch_and_worktree(branch, root, dry_run=dry_run, force=True)
    return remaining


def _report_stuck_worktrees(stuck: list[worktrees.WorktreeStatus], *, clean_dirty: bool) -> None:
    """Prominently report merged/closed worktrees the sweep left behind.

    Printed after the pipeline so it lands below the collapsed stage log where
    the human running finalize cannot miss it (issue #2348)."""
    if not stuck:
        return
    print()
    print(f"⚠  {len(stuck)} merged/closed worktree(s) left in place — needs attention:")
    for status in stuck:
        detail = status.detail or f"{status.state.value}, not removable"
        print(f"   • {status.worktree.branch}: {detail}")
    if not clean_dirty:
        print(
            "   Re-run with --clean-dirty to clear any dirtied only by untracked "
            "build output (each is shown and confirmed first)."
        )


def _handle_stuck_worktrees(ctx: FinalizeContext) -> None:
    """Surface — and, with --clean-dirty, guardedly clear — stuck worktrees.

    The post-pipeline half of issue #2348: ``_stage_cleanup`` collected any
    merged/closed worktree it could not remove into ``ctx.stuck``; here, on
    real stdio, they are optionally cleared (untracked-only dirt) and the
    remainder is surfaced prominently.
    """
    stuck = ctx.stuck
    if not stuck:
        return
    if ctx.args.clean_dirty:
        stuck = _clean_dirty_worktrees(
            stuck, ctx.root, dry_run=ctx.args.dry_run, assume_yes=ctx.args.yes
        )
    _report_stuck_worktrees(stuck, clean_dirty=ctx.args.clean_dirty)


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


_CD_PASSING_CONCLUSIONS = ("success", "skipped", "neutral")


def _list_cd_runs(target_branch: str) -> list[dict[str, object]] | None:
    """List recent CD workflow runs on ``target_branch``.

    Returns a list of run dicts (possibly empty when the workflow exists but
    has not run yet), or ``None`` when the runs cannot be determined — the CD
    workflow does not exist in the repo, ``gh`` errored, or the response was
    malformed. ``None`` means "do not block"; an empty list means "keep
    polling, the run may not have registered yet."
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
            "30",
            "--json",
            "conclusion,databaseId,headSha,status,createdAt,url",
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
    return runs if isinstance(runs, list) else None


def _match_cd_run(runs: list[dict[str, object]], head_sha: str) -> dict[str, object] | None:
    """Return the run in ``runs`` whose ``headSha`` equals ``head_sha``."""
    for run in runs:
        if isinstance(run, dict) and (run.get("headSha") or "") == head_sha:
            return run
    return None


def _view_cd_run(run_id: object) -> dict[str, object] | None:
    """Fetch a CD run's current status/conclusion/url, or ``None`` on error."""
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "gh",
            "run",
            "view",
            str(run_id),
            "--json",
            "status,conclusion,url",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    stdout = result.stdout or ""
    try:
        data = json.loads(stdout) if stdout.strip() else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def cd_timeout_secs() -> int:
    """The CD watch deadline in seconds: ``$VRG_CD_TIMEOUT`` or 5400.

    Mirrors :func:`vergil_tooling.lib.github.checks_timeout_secs`: a malformed
    or non-positive override is a configuration error and raises rather than
    silently falling back — the deadline is what keeps a hung CD run from
    blocking finalize forever, so it cannot be disabled (issue #3316).
    """
    raw = os.environ.get(CD_TIMEOUT_ENV, "").strip()
    if not raw:
        return _CD_WATCH_TIMEOUT_SECS
    try:
        secs = int(raw)
    except ValueError:
        secs = 0
    if secs <= 0:
        msg = f"{CD_TIMEOUT_ENV} must be a positive whole number of seconds, got {raw!r}"
        raise ValueError(msg)
    return secs


@dataclass(frozen=True)
class CDRunProblem:
    """Why the CD run this merge triggered did not confirm a pass.

    Either the run *concluded* non-success (``conclusion`` set — a real CD
    failure) or the watch deadline elapsed while it was still running
    (``conclusion`` is ``None`` — result unknown, never reported as a failed
    run; issue #3316).
    """

    run_id: object
    target_branch: str
    head_sha: str
    url: str
    conclusion: str | None
    waited_secs: int
    # Why a cancelled run could not be cleared as a concurrency supersede, when
    # the lookup itself failed (issue #3834); shown with the failure.
    note: str = ""

    @property
    def timed_out(self) -> bool:
        return self.conclusion is None

    @property
    def run_ref(self) -> str:
        return f"CD workflow run {self.run_id} on {self.target_branch} ({self.head_sha[:7]})"

    def describe(self) -> str:
        if self.timed_out:
            detail = f"is still in progress after {self.waited_secs}s; result unknown."
        else:
            detail = f"ended with conclusion '{self.conclusion}'."
        text = f"{self.run_ref} {detail}\n  {self.url}"
        if self.note:
            text += f"\n  {self.note}"
        return text


@dataclass(frozen=True)
class CDRunSuperseded:
    """The CD run this merge triggered was cancelled by a concurrency supersede.

    GitHub cancelled its jobs because a higher-priority run was waiting on the
    same concurrency group (issue #3834). That is not a CD failure, but it is
    not a pass either: this run published nothing, and whichever run took its
    place is not checked here. GitHub records only the group, never the run
    that superseded it, and that run may be another workflow (a scheduled
    rebuild sharing the group) or a newer commit's CD, so it cannot be
    followed without guessing.
    """

    run_id: object
    target_branch: str
    head_sha: str
    url: str
    group: str

    @property
    def run_ref(self) -> str:
        return f"CD workflow run {self.run_id} on {self.target_branch} ({self.head_sha[:7]})"

    def describe(self) -> str:
        return (
            f"{self.run_ref} was superseded: GitHub cancelled it because a "
            f"higher-priority run was waiting on concurrency group '{self.group}'.\n"
            f"  {self.url}"
        )


# The check-run annotation GitHub writes on each job a concurrency group
# cancels to make way for a higher-priority waiting run. The group is quoted
# for a workflow-level group and bare for a job-level one.
_SUPERSEDE_ANNOTATION_RE = re.compile(
    r"^Canceling since a higher priority waiting request for '?(?P<group>.+?)'? exists$"
)
_RUN_URL_REPO_RE = re.compile(r"github\.com/([^/]+/[^/]+)/actions/runs/\d+")


def _supersede_group(run_id: object, url: str) -> str | None:
    """The concurrency group that superseded cancelled CD run *run_id*, or ``None``.

    A cancelled run is a supersede only when every job ended passing or
    cancelled, at least one was cancelled, and every cancelled job carries
    GitHub's supersede annotation. Any other cancel (by a human, or a run with
    no job to carry the annotation) returns ``None``, and so does a run where
    a job really failed. The annotations are read from the check-runs API (a
    job's id is its check-run id); the job logs do not carry the message.

    *url* is the run's URL, which names the repo the run belongs to. A ``gh``
    or API failure propagates, as does a payload of the wrong shape
    (``ValueError``): the caller reports the run as failed and says why.
    """
    match = _RUN_URL_REPO_RE.search(url)
    if match is None:
        msg = f"cannot read the repository of CD run URL {url!r}"
        raise ValueError(msg)
    repo = match.group(1)
    data = github.read_json("run", "view", str(run_id), "--repo", repo, "--json", "jobs")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        msg = f"gh run view {run_id} returned no jobs list"
        raise ValueError(msg)
    cancelled: list[object] = []
    for job in jobs:
        if not isinstance(job, dict):
            msg = f"gh run view {run_id} returned a job that is not an object: {job!r}"
            raise ValueError(msg)
        conclusion = job.get("conclusion")
        if conclusion == "cancelled":
            cancelled.append(job.get("databaseId"))
        elif conclusion not in _CD_PASSING_CONCLUSIONS:
            return None
    if not cancelled:
        return None
    groups: set[str] = set()
    for job_id in cancelled:
        endpoint = f"repos/{repo}/check-runs/{job_id}/annotations"
        annotations = github.read_json("api", endpoint)
        if not isinstance(annotations, list):
            msg = f"{endpoint} returned no annotations list"
            raise ValueError(msg)
        found = None
        for annotation in annotations:
            message = annotation.get("message") if isinstance(annotation, dict) else None
            hit = _SUPERSEDE_ANNOTATION_RE.match(str(message or "").strip())
            if hit is not None:
                found = hit.group("group")
                break
        if found is None:
            return None
        groups.add(found)
    return ", ".join(sorted(groups))


def _wait_for_cd_run(
    target_branch: str,
    head_sha: str,
    *,
    dispatch_timeout: float = _CD_DISPATCH_TIMEOUT_SECS,
    watch_timeout: float | None = None,
    poll_interval: float = _CD_POLL_INTERVAL_SECS,
) -> CDRunProblem | CDRunSuperseded | None:
    """Block until the CD run triggered by ``head_sha`` completes.

    The merge's base-branch push dispatches CD asynchronously, so this first
    polls ``gh run list`` (up to ``dispatch_timeout``) for the run whose
    ``headSha`` matches the merge commit — not "latest run on branch," which
    can be a stale or unrelated run (issue #2753). Once the run registers it
    watches it (up to ``watch_timeout``, default :func:`cd_timeout_secs` —
    resolved up front so a malformed ``$VRG_CD_TIMEOUT`` fails before any
    waiting) until it concludes.

    Returns a :class:`CDRunProblem` when the run concludes non-success or the
    watch times out (``timed_out``). A run that concluded ``cancelled`` is
    checked by :func:`_supersede_group`: a concurrency supersede returns a
    :class:`CDRunSuperseded` (not a failure, reported loudly by the stage);
    any other cancel, or one that cannot be checked, is a
    :class:`CDRunProblem` (issue #3834). Returns ``None`` — do not fail
    finalize — when:
      - the CD workflow does not exist / cannot be queried,
      - no run was dispatched for this SHA within ``dispatch_timeout``
        (a change CD legitimately skips, e.g. path filters), or
      - the run concluded success / skipped / neutral.
    """
    if watch_timeout is None:
        watch_timeout = cd_timeout_secs()
    # Phase 1: wait for the run this merge triggered to register.
    dispatch_deadline = time.monotonic() + dispatch_timeout
    run: dict[str, object] | None = None
    while True:
        runs = _list_cd_runs(target_branch)
        if runs is None:
            # No CD workflow in this repo, or it cannot be queried — nothing
            # to block on. Preserves graceful behavior for repos without CD.
            return None
        run = _match_cd_run(runs, head_sha)
        if run is not None:
            break
        if time.monotonic() >= dispatch_deadline:
            print(
                f"  No CD workflow run registered for {head_sha[:7]} within "
                f"{int(dispatch_timeout)}s — assuming this change triggers no "
                "CD run and not blocking.",
                file=sys.stderr,
            )
            return None
        time.sleep(poll_interval)

    run_id = run.get("databaseId")
    url = str(run.get("url") or "")
    print(f"  Waiting on CD workflow run {run_id} for {head_sha[:7]} ...")
    if url:
        print(f"  {url}")

    # Phase 2: watch the registered run to completion.
    watch_deadline = time.monotonic() + watch_timeout
    while True:
        view = _view_cd_run(run_id)
        if view is not None:
            url = str(view.get("url") or url)
            if view.get("status") == "completed":
                conclusion = str(view.get("conclusion") or "")
                if conclusion in _CD_PASSING_CONCLUSIONS:
                    return None
                note = ""
                if conclusion == "cancelled":
                    try:
                        group = _supersede_group(run_id, url)
                    except (subprocess.CalledProcessError, ValueError) as exc:
                        # JSONDecodeError is a ValueError. The cancel stays a
                        # failure, with the reason it could not be cleared.
                        group = None
                        note = (
                            "finalize could not tell whether a concurrency supersede "
                            f"cancelled it: {error_text(exc)}"
                        )
                    if group is not None:
                        return CDRunSuperseded(run_id, target_branch, head_sha, url, group)
                return CDRunProblem(
                    run_id,
                    target_branch,
                    head_sha,
                    url,
                    conclusion or "unknown",
                    int(watch_timeout),
                    note,
                )
        if time.monotonic() >= watch_deadline:
            return CDRunProblem(run_id, target_branch, head_sha, url, None, int(watch_timeout))
        time.sleep(poll_interval)


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


def _resolve_strategy(branch: str, override: str | None, *, batch: bool) -> str:
    """Merge strategy for *branch*.

    An explicit ``--strategy`` wins, except that ``squash`` on a batch PR is
    refused: it would collapse the batch's one commit per issue into one
    commit and one changelog entry, the anti-goal of epic
    vergil-project/.github#1019. Otherwise a batch PR rebase-merges, so the
    base receives every issue's commit. A non-batch PR's default is driven by
    the branch prefix: ``release/*`` branches (release PRs and back-merges)
    merge with a merge commit to preserve the develop<->main ancestry the
    release model relies on; squashing them silently breaks that ancestry and
    makes later release branches conflict (issue #1620). Every other branch
    squashes, as before.
    """
    if batch and override == "squash":
        msg = (
            "--strategy squash is refused on a batch PR (more than one Closes line): "
            "a squash collapses its one commit per issue into a single commit and a "
            "single changelog entry. Re-run without --strategy to rebase-merge it."
        )
        raise FinalizeError(msg)
    if override is not None:
        return override
    if batch:
        return "rebase"
    return "merge" if branch.startswith(_RELEASE_BRANCH_PREFIX) else "squash"


def _enter_worktree(head: str, root: Path) -> str:
    """The command lines that put the human in *head*'s worktree, creating it if needed."""
    wt = worktrees.worktree_for_branch(head, root)
    if wt is not None:
        return f"  cd {wt}"
    dirname = branch_names.expected_worktree_dirname(head) or head.replace("/", "-")
    wt = root / ".worktrees" / dirname
    return (
        f"  cd {root} && git fetch origin && git worktree add .worktrees/{dirname} {head}\n"
        f"  cd {wt}"
    )


def _rebase_steps(pr: str, head: str, base: str, root: Path) -> str:
    """The commands that rebase *pr*'s branch onto *base* and update the PR.

    They are ``pr-watch``'s steps in raw git: rebase onto *base*, validate,
    force-push with a lease. The ``--ff-only`` step first picks up the branch
    as origin has it, and the rebase drops any merge commit GitHub's
    update-branch button added. The push names the branch, because a worktree
    created with ``-b … origin/<base>`` tracks the base, not itself, and uses
    a lease, so it replaces only the head last fetched.
    """
    return (
        f"{_enter_worktree(head, root)}\n"
        f"  git fetch origin\n"
        f"  git merge --ff-only origin/{head}\n"
        f"  git rebase origin/{base}\n"
        f"  vrg-container-run -- vrg-validate\n"
        f"  git push --force-with-lease origin {head}\n"
        f"  cd {root} && vrg-finalize-pr {pr}"
    )


def _conflict_resolution_msg(pr: str, head: str, base: str, root: Path, *, batch: bool) -> str:
    """The exact commands that resolve *pr*'s conflict with *base*.

    Submitting lifts the freeze (``freeze.is_frozen``; spec §5.10), so the
    agent's ``pr-watch`` can resolve the conflict, or the human can, with the
    raw-git form of ``pr-watch``'s steps: rebase onto *base*, validate and
    force-push with a lease. The commands carry no inline ``#`` comments
    because interactive zsh does not treat them as comments.

    A batch PR must also keep one commit per issue and no merge commit, so
    its message warns against merging *base* in (epic
    vergil-project/.github#1019).

    A ``release/*`` branch (a release PR or a back-merge) is the exception:
    it merges with a merge commit to keep the develop<->main ancestry
    (issue #1620), which a rebase would break, and it is no PR-workflow
    branch, so ``pr-watch`` never resolves it. The human merges *base* in
    with raw git instead. The ``--ff-only`` step first picks up any commits
    GitHub added to the branch (update-branch) so the push fast-forwards,
    and ``--no-commit`` stops the merge for validation before the commit.
    """
    if not batch and head.startswith(_RELEASE_BRANCH_PREFIX):
        return (
            f"PR {pr} has merge conflicts with {base}. It is a release branch, which "
            f"merges with a merge commit, so merge {base} in — do not rebase it — then "
            f"validate, commit and push with raw git. After the --no-commit merge stops, "
            f"resolve each conflicted file and git add it:\n"
            f"{_enter_worktree(head, root)}\n"
            f"  git fetch origin\n"
            f"  git merge --ff-only origin/{head}\n"
            f"  git merge --no-commit origin/{base}\n"
            f"  vrg-container-run -- vrg-validate\n"
            f"  git commit --no-edit\n"
            f"  git push origin {head}\n"
            f"  cd {root} && vrg-finalize-pr {pr}"
        )
    intro = (
        f"PR {pr} has merge conflicts with {base}. Submitting lifted the branch's "
        f"freeze, so the agent's pr-watch can resolve it (it rebases onto {base} and "
        f"pushes with a lease), or you can, with the commands below."
    )
    if batch:
        return (
            f"{intro} It is a batch PR, one commit per issue, so rebase it onto {base} — "
            f"do not merge {base} in, which adds a merge commit the batch check refuses. "
            f"When the rebase stops on a conflict, resolve each conflicted file, git add "
            f"it and run git rebase --continue:\n"
            f"{_rebase_steps(pr, head, base, root)}"
        )
    return (
        f"{intro} Rebase it onto {base}; when the rebase stops on a conflict, resolve "
        f"each conflicted file, git add it and run git rebase --continue:\n"
        f"{_rebase_steps(pr, head, base, root)}"
    )


def _read_pr_body(pr: str) -> str:
    """Return *pr*'s body; a failed or malformed read refuses."""
    try:
        info = github.read_json("pr", "view", pr, "--json", "body")
    except (subprocess.CalledProcessError, ValueError) as exc:
        msg = f"could not read the body of PR {pr}: {error_text(exc)}"
        raise FinalizeError(msg) from exc
    body = info.get("body") if isinstance(info, dict) else None
    if not isinstance(body, str):
        msg = f"could not read the body of PR {pr}: no body in the response: {info!r}"
        raise FinalizeError(msg)
    return body


def _batch_closes(body: str) -> list[str]:
    """The body's ``Closes`` refs, in body order, when it has more than one; else ``[]``.

    A PR whose body has more than one ``Closes`` line is a batch PR (epic
    vergil-project/.github#1019). ``Ref`` lines never count, and nor do
    manual links in GitHub's Development sidebar, which are not in the body.
    """
    closes = [ref for keyword, ref in linkage.extract_tracking_refs(body) if keyword == "Closes"]
    return closes if len(closes) > 1 else []


def _batch_issues(pr: str, closes: list[str]) -> list[str]:
    """Cross-check a batch PR's ``Closes`` refs against GitHub; return them as ``#N``.

    Each ref must be in the PR's own repo, listed once, and among GitHub's
    closing references for the PR, so GitHub closes every issue the commits
    are checked against. Anything else refuses, naming both lists.
    """
    refs = _read_closing_refs(pr)
    repo = refs.repo
    numbers: list[int] = []
    for ref in closes:
        ref_repo, _, number = ref.rpartition("#")
        if ref_repo and ref_repo.lower() != repo.lower():
            msg = (
                f"batch PR {refs.ref} closes {ref}, an issue in another repo; every "
                f"issue in a batch is in the PR's repo ({repo})"
            )
            raise FinalizeError(msg)
        if int(number) in numbers:
            msg = f"batch PR {refs.ref} lists Closes #{number} more than once"
            raise FinalizeError(msg)
        numbers.append(int(number))
    listed = {issue.ref.lower() for issue in refs.issues}
    missing = [f"{repo}#{n}" for n in numbers if f"{repo}#{n}".lower() not in listed]
    if missing:
        body_refs = ", ".join(f"{repo}#{n}" for n in numbers)
        github_refs = ", ".join(issue.ref for issue in refs.issues) or "none"
        msg = (
            f"batch PR {refs.ref}: its body closes {body_refs}, but GitHub's closing "
            f"references are {github_refs}; missing {', '.join(missing)}. GitHub would "
            "not close every issue of the batch, so it is not merged. Fix the PR body's "
            "Closes lines, then re-run."
        )
        raise FinalizeError(msg)
    return [f"#{n}" for n in numbers]


# The batch rule whose fix is a rebase onto the base (batch_rules.check_commit_per_issue).
_MERGE_COMMIT_RULE = "no merge commits"


def _make_batch_check(
    pr: str, issues: list[str], *, head: str, base: str, root: Path
) -> Callable[[], str]:
    """The batch check ``wait_and_merge`` runs: fetch, check the invariant, return the SHA.

    Each run fetches *base* and *head* from origin, so it checks the PR's head
    as it is now, then runs the commit-per-issue check from *base* to that
    head. A refusal names the rule and the commit; a merge commit (GitHub's
    update-branch button) also names the rebase that fixes it. Submitting
    lifted the freeze (``freeze.is_frozen``; spec §5.10), so that message
    names the agent's ``pr-watch`` or the human as the one to run it.
    """

    def check() -> str:
        try:
            git.run(
                "fetch",
                "origin",
                f"+refs/heads/{base}:refs/remotes/origin/{base}",
                f"+refs/heads/{head}:refs/remotes/origin/{head}",
            )
            sha = git.read_output("rev-parse", "--verify", f"refs/remotes/origin/{head}^{{commit}}")
        except (subprocess.CalledProcessError, OSError) as exc:
            msg = (
                f"could not fetch batch PR {pr}'s head {head} for its batch check: "
                f"{error_text(exc)}"
            )
            raise FinalizeError(msg) from exc
        try:
            batch_rules.check_commit_per_issue(issues, base=f"origin/{base}", head=sha)
        except batch_rules.BatchRuleError as exc:
            msg = f"PR {pr} is not merged: {exc}"
            if exc.rule == _MERGE_COMMIT_RULE:
                msg += (
                    f". Submitting lifted the branch's freeze, so the agent's pr-watch "
                    f"can fix it (it rebases onto {base} and pushes with a lease), or you "
                    f"can, with the commands below:\n"
                    f"{_rebase_steps(pr, head, base, root)}"
                )
            raise FinalizeError(msg) from exc
        print(f"Batch check passed at {sha}: one commit per issue ({', '.join(issues)}).")
        return sha

    return check


def _stage_merge(ctx: FinalizeContext) -> None:
    """Merge the PR (or confirm it is already merged) and record its branch.

    A batch PR (more than one ``Closes`` line in its body) is cross-checked
    against GitHub's closing references, checked one commit per issue,
    updated by rebase when behind, re-checked after every update and right
    before the merge, and rebase-merged at the head it checked (epic
    vergil-project/.github#1019). Every other PR merges as before.
    """
    args = ctx.args
    branch = github.head_ref(args.pr)
    closes = _batch_closes(_read_pr_body(args.pr))
    is_batch = bool(closes)
    strategy = _resolve_strategy(branch, args.strategy, batch=is_batch)
    if github.pr_state(args.pr) == "MERGED":
        print(f"PR {args.pr} already merged.")
    elif args.dry_run:
        kind = f"batch PR ({', '.join(closes)}) " if is_batch else "PR "
        print(f"  [dry-run] wait for green, then merge {kind}{args.pr} (--{strategy})")
    else:
        base: str | None = None
        try:
            if is_batch:
                base = github.base_ref(args.pr)
                issues = _batch_issues(args.pr, closes)
                batch_check = _make_batch_check(
                    args.pr, issues, head=branch, base=base, root=ctx.root
                )
                batch_check()
                pr_merge.wait_and_merge(args.pr, strategy=strategy, batch_check=batch_check)
            else:
                pr_merge.wait_and_merge(args.pr, strategy=strategy)
        except pr_merge.MergeConflictError as exc:
            target = base if base is not None else github.base_ref(args.pr)
            msg = _conflict_resolution_msg(args.pr, branch, target, ctx.root, batch=is_batch)
            raise FinalizeError(msg) from exc
        except pr_merge.MergeAbortError as exc:
            raise FinalizeError(str(exc)) from exc
    ctx.merged_branch = branch
    # Record the merge commit SHA so the cd-check stage waits on exactly the
    # CD run this merge triggered (issue #2753). Skipped on dry-run, where no
    # merge commit exists.
    if not args.dry_run:
        ctx.merge_commit_sha = github.merge_commit_sha(args.pr) or None


# GitHub closes a PR's closing-reference issues asynchronously after the merge,
# usually within a second or two. Before closing one itself, finalize gives
# GitHub this long and reads the states again, so an auto-close still in
# flight is not reported as one GitHub skipped (issue #4002).
_AUTO_CLOSE_GRACE_SECS = 10


def _read_closing_refs(pr: str) -> github.PRClosingReferences:
    try:
        return github.pr_closing_references(pr)
    except (subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        msg = f"could not read the closing issue references of PR {pr}: {error_text(exc)}"
        raise FinalizeError(msg) from exc


def _stage_close_refs(ctx: FinalizeContext) -> None:
    """Close any issue the merged PR closes that GitHub left open (issue #4002).

    GitHub sometimes skips the closing-keyword auto-close, leaving a merged
    task open, which blocks epic rollup. Only the issues GitHub itself lists
    as the PR's closing references are touched, never ones it merely
    mentions. Each issue closed is reported; a failed read or close fails the
    stage, after every close has been tried.
    """
    args = ctx.args
    if args.dry_run:
        print(
            f"  [dry-run] after the merge, close any closing-reference issue of PR {args.pr} "
            "that GitHub left open"
        )
        return
    refs = _read_closing_refs(args.pr)
    if refs.state != "MERGED":
        msg = f"PR {refs.ref} is {refs.state}, not MERGED; its closing references were not checked"
        raise FinalizeError(msg)
    if not refs.issues:
        print(f"PR {refs.ref} has no closing issue references.")
        return
    if any(issue.state == "OPEN" for issue in refs.issues):
        time.sleep(_AUTO_CLOSE_GRACE_SECS)
        refs = _read_closing_refs(args.pr)
    still_open = [issue for issue in refs.issues if issue.state == "OPEN"]
    for issue in refs.issues:
        if issue.state != "OPEN":
            print(f"  {issue.ref}: closed")
    merged = f" (merged {refs.merged_at})" if refs.merged_at else ""
    comment = f"Completed by PR {refs.ref}{merged}; GitHub did not auto-close it."
    failures: list[str] = []
    for issue in still_open:
        try:
            github.close_issue(issue.repo, issue.number, comment=comment)
        except subprocess.CalledProcessError as exc:
            print(f"ERROR: could not close {issue.ref}: {error_text(exc)}", file=sys.stderr)
            failures.append(issue.ref)
            continue
        print(f"  {issue.ref}: GitHub left it open after the merge; closed it as completed")
    if failures:
        msg = (
            f"could not close {', '.join(failures)}, left open after PR {refs.ref} merged; "
            "close each by hand"
        )
        raise FinalizeError(msg)


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
    # with the same logic that backs vrg-repo-status, so "cruft" and
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
            if status.needs_attention:
                # Finished-but-stuck: merged/closed but dirty, or a reused
                # branch name (issue #1719). Collect it so it is surfaced
                # after the pipeline instead of buried in the collapsed stage
                # log (issue #2348).
                ctx.stuck.append(status)
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

    print("Checking for orphaned pr-workflow relay refs...")
    _prune_orphan_relay_refs(dry_run=args.dry_run)

    print("Checking for orphaned cached dev images...")
    _prune_orphan_branch_images(dry_run=args.dry_run)

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


def _stage_provision(ctx: FinalizeContext) -> None:
    """Provision the target-branch dev image up front, before validation.

    ``_stage_cleanup`` has just advanced the target branch — the one
    deterministic reason its cached image needs rebuilding. Warm it here, once,
    so the validation stage (and the next PR's work) reuse a warm image instead
    of triggering a cold rebuild *inside* an operation. That lazy just-in-time
    rebuild — late, and possibly under parallel load — is what let a build-time
    glitch masquerade as "validation failed after a clean merge" (issue #2462).
    """
    args = ctx.args
    if args.dry_run:
        print("  [dry-run] provision dev image for the target branch")
        return
    print("Provisioning dev image for post-finalization validation...")
    lang = resolve_language(ctx.root)
    try:
        # quiet_warmup: this stage runs under the live progress display, so the
        # warmup build is captured (surfaced on failure) rather than streamed
        # raw over the progress tree (#2906).
        image, source = provision_dev_image(ctx.root, lang, quiet_warmup=True)
    except (RuntimeError, VersionsError) as exc:
        raise FinalizeError(f"dev-image provisioning failed: {exc}") from exc
    if source == "env":
        print(f"  Using DOCKER_DEV_IMAGE override: {image}")
    else:
        print(f"  Image ready: {image}")


# Signatures of a container that never started (as opposed to a command that
# ran and failed). Matched case-insensitively against the runtime's stderr so a
# runtime that does not use the reserved exit 125 is still classified from its
# diagnostic text — the orphaned image-cache snapshot is the common cause.
_LAUNCH_FAILURE_SIGNATURES = (
    "parent snapshot",
    "failed to prepare",
    "no such image",
    "does not exist: not found",
)


def _is_container_launch_failure(exc: subprocess.CalledProcessError) -> bool:
    """Return True when *exc* is the runtime failing to start the container.

    ``vrg-container-run`` execs the container runtime, so its exit code is the
    runtime's. Exit 125 is the runtime's reserved "could not create/start the
    container" code — distinct from the containerized command's own exit codes
    (``vrg-validate`` exits 1 on a real validation failure). A stale image-cache
    snapshot is the common cause; match its signature too (issue #3016).
    """
    if exc.returncode == 125:
        return True
    text = f"{exc.stderr or ''}\n{exc.output or ''}".lower()
    return any(sig in text for sig in _LAUNCH_FAILURE_SIGNATURES)


def _validation_failure_msg(branch: str, exc: subprocess.CalledProcessError) -> str:
    """The message for a genuine validation failure — the target branch is broken."""
    return (
        f"post-finalization validation failed (exit {exc.returncode}) — "
        f"fix {branch} before creating the next PR"
    )


def _launch_failure_msg(branch: str, exc: subprocess.CalledProcessError) -> str:
    """The message for a container that would not launch even after a rebuild.

    Explicitly *not* a target-branch regression: validation never ran. Surfaces
    the runtime's own stderr so the operator sees the actual cause.
    """
    detail = (exc.stderr or exc.output or "").strip() or "(no runtime diagnostic captured)"
    return (
        f"post-finalization validation could not run: the dev container failed to "
        f"launch (exit {exc.returncode}) even after rebuilding the image. This is a "
        f"stale image-cache / container-runtime problem, not a {branch} regression.\n"
        f"{detail}"
    )


def _rebuild_failure_msg(branch: str, exc: BaseException, retry: tuple[str, ...]) -> str:
    """The message for a dev-image rebuild that failed, so validation never ran.

    Like a CD watch timeout (issue #3316), an unrun validation is an unknown
    result, not a pass: it fails the stage. Carries the original error text in
    full (a failed cache build folds its captured output into it) and the
    command to retry once the cause is fixed (issue #3605).
    """
    return (
        f"post-finalization validation did not run; result unknown: could not "
        f"rebuild the {branch} dev image after the container failed to launch.\n"
        f"{error_text(exc)}\n"
        f"Free space or fix the cause, then retry with: {' '.join(retry)}"
    )


def _reprovision_target_image(ctx: FinalizeContext, retry: tuple[str, ...]) -> None:
    """Force a clean rebuild of the target-branch dev image (issue #3016).

    Drop any cached image for the target branch so the rebuild cannot reuse a
    corrupt one, then provision fresh. Used to recover from a container-launch
    failure discovered during validation.

    A failed rebuild — the low-disk guard refusing it (#3571), a failed cache
    build, or an unresolvable version window — raises :class:`FinalizeError`
    naming the image, the cause and the *retry* command (issue #3605).
    """
    branch = ctx.args.target_branch
    _clean_branch_images(branch, ctx.root)
    lang = resolve_language(ctx.root)
    try:
        provision_dev_image(ctx.root, lang, quiet_warmup=True)
    except (RuntimeError, VersionsError, subprocess.CalledProcessError) as exc:
        raise FinalizeError(_rebuild_failure_msg(branch, exc, retry)) from exc


def _stage_validation(ctx: FinalizeContext) -> None:
    """Run canonical validation to catch problems on the target branch
    before the next PR is created.

    Streams through ``progress.run`` — a bare ``subprocess.run`` with
    inherited stdout would write raw lines under the live display and
    strand stale frames (issue #1470).

    A non-zero exit is not automatically a target-branch regression: exit 125
    (or a runtime snapshot error) means the container never started, so
    validation never ran. That is usually a stale image-cache snapshot whose
    parent layer was orphaned; rebuild the dev image and retry once before
    concluding anything about the branch (issue #3016). If that rebuild itself
    fails, validation did not run and its result is unknown, so the stage fails
    without a pointless retry (issues #3316, #3605).
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
        return
    except subprocess.CalledProcessError as exc:
        if not _is_container_launch_failure(exc):
            raise FinalizeError(_validation_failure_msg(args.target_branch, exc)) from exc

    # The container never launched — validation did not run. Rebuild the dev
    # image (self-healing the orphaned cache) and retry the validation once.
    print(
        "  WARNING: the validation container failed to launch (stale image cache); "
        "rebuilding the dev image and retrying once...",
        file=sys.stderr,
    )
    # A failed rebuild raises here: there is no new image to retry against.
    _reprovision_target_image(ctx, cmd)
    try:
        progress.run(cmd)
    except subprocess.CalledProcessError as exc:
        if _is_container_launch_failure(exc):
            raise FinalizeError(_launch_failure_msg(args.target_branch, exc)) from exc
        raise FinalizeError(_validation_failure_msg(args.target_branch, exc)) from exc


def _stage_cd_check(ctx: FinalizeContext) -> None:
    """Block on the CD workflow run this merge triggered (issue #2753).

    The base-branch push a merge produces dispatches CD asynchronously
    (docker build/publish, docs, release). Rather than glance at the latest
    run — which may be a stale or unrelated one — this identifies the run by
    the merge commit SHA, waits for it to complete, and fails finalize if it
    concludes non-success so a post-merge publish failure is caught here
    rather than discovered later. ``--no-wait-cd`` is the conscious opt-out.
    """
    args = ctx.args
    if args.dry_run:
        print("  [dry-run] wait on the CD workflow run triggered by the merge")
        return
    if args.no_wait_cd:
        print("  Skipping CD wait (--no-wait-cd).")
        return

    # In the PR path the merge stage recorded the exact merge commit. In the
    # cleanup-only path (release / batch tail) no merge happened in this
    # invocation, so the CD run to watch is the one on the current tip of the
    # target branch, which cleanup has just switched to and synced.
    head_sha = ctx.merge_commit_sha
    if not head_sha:
        try:
            head_sha = git.read_output("rev-parse", args.target_branch).strip()
        except subprocess.CalledProcessError:
            head_sha = ""
    if not head_sha:
        # Could not resolve the SHA to associate a CD run with — cleanup has
        # already succeeded, so warn loudly rather than fail finalize over the
        # post-merge wait, and do not silently pretend CD passed.
        print(
            f"  WARNING: could not resolve the tip of {args.target_branch} to "
            "wait on its CD run — skipping the post-merge CD wait.",
            file=sys.stderr,
        )
        return

    problem = _wait_for_cd_run(args.target_branch, head_sha)
    if problem is None:
        return
    if isinstance(problem, CDRunSuperseded):
        # Superseded is not failed, but it is not a pass: this run published
        # nothing, and the run that took its place is not checked here. Say so
        # loudly and mark the stage ⚠ rather than fail it (issue #3834).
        print(
            "WARNING: the CD workflow run triggered by this merge was superseded, not failed.",
            file=sys.stderr,
        )
        print(f"  {problem.describe()}", file=sys.stderr)
        print(
            "  This merge's CD result is not confirmed. GitHub does not record which run\n"
            f"  took its place in '{problem.group}': it may be a newer commit's CD or\n"
            "  another workflow sharing the group (e.g. a scheduled rebuild). Check the\n"
            "  latest run in that group in the Actions tab.",
            file=sys.stderr,
        )
        progress.mark_warn(
            f"CD run {problem.run_id} superseded (concurrency group "
            f"'{problem.group}'); this merge's CD not confirmed"
        )
        return
    if problem.timed_out:
        # Still running is not failed: say so, and say how to find out. The
        # stage still fails — an unknown result is not a pass (issue #3316).
        secs = problem.waited_secs
        print(
            f"ERROR: CD run still in progress after {secs}s; result unknown.",
            file=sys.stderr,
        )
        print(f"  {problem.describe()}", file=sys.stderr)
        print(
            "  Finalize stopped watching before the run finished — this is not "
            "a CD failure.\n"
            f"  Re-check the run with: gh run view {problem.run_id}  (or open the "
            "URL above).\n"
            "  Once it has succeeded, re-run: vrg-finalize-pr --cleanup-only\n"
            f"  If this CD is legitimately slower, raise the watch limit with "
            f"{CD_TIMEOUT_ENV}=<seconds> (currently {secs}s).",
            file=sys.stderr,
        )
        msg = f"CD run still in progress after {secs}s; result unknown"
        raise FinalizeError(msg)
    print(
        "ERROR: the CD workflow run triggered by this merge did not succeed.",
        file=sys.stderr,
    )
    print(f"  {problem.describe()}", file=sys.stderr)
    print(
        "  The post-merge publish did not complete — investigate the run above.",
        file=sys.stderr,
    )
    raise FinalizeError("CD workflow run triggered by the merge did not succeed")


def build_stages(*, include_pr: bool, include_post_checks: bool = True) -> tuple[Stage, ...]:
    """Assemble the pipeline for the resolved mode.

    provenance/merge run only when a PR was given or inferred; cleanup always
    runs. provision, validation, and cd-check run unless *include_post_checks*
    is False (the batch path defers them to one end-of-batch run, issue #1673).
    provision warms the target-branch image (fail_fast, before it is used);
    validation is fail_defer so a validation failure still lets cd-check run and
    surface the CD status. cd-check is fail_fast: a post-merge CD run that did
    not succeed is a hard finalize failure (issue #2753), and it is the last
    stage so nothing is lost by stopping there.
    """
    stages: list[Stage] = []
    if include_pr:
        stages.append(Stage("provenance", _stage_provenance, "fail_fast"))
        stages.append(Stage("merge", _stage_merge, "fail_fast"))
        # fail_defer: an issue left open must fail finalize, but the PR is
        # merged by now, so its cleanup still runs (issue #4002).
        stages.append(Stage("close-refs", _stage_close_refs, "fail_defer"))
    stages.append(Stage("cleanup", _stage_cleanup, "fail_fast"))
    if include_post_checks:
        # Provision the environment before it is used. fail_fast: if the image
        # cannot be built there is no point running validation against a broken
        # or absent one (issue #2462).
        stages.append(Stage("provision", _stage_provision, "fail_fast"))
        stages.append(Stage("validation", _stage_validation, "fail_defer"))
        stages.append(Stage("cd-check", _stage_cd_check, "fail_fast"))
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


if __name__ == "__main__":
    sys.exit(main())
