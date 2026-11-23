"""Finalize a batch of pull requests: watch them merge, then settle develop once.

``vrg-submit-pr`` opens each PR with auto-merge on, and GitHub merges it once
its required checks pass. ``vrg-finalize-pr`` never merges a PR and never
turns auto-merge on (epic vergil-project/.github#1218, spec §6.3). It:

1. **Watches** every listed PR (``lib/finalize_watch.py``), holding no lock,
   until each ends ``merged`` or ``failed: …`` (CI, conflict, closed,
   auto-merge off, provenance, not a develop PR), or the batch deadline
   (``$VRG_FINALIZE_DEADLINE`` seconds, default 7200) passes and it is
   ``timed out``. A PR that merged before the run started counts as merged.
   The watch runs the provenance check on each PR, and turns auto-merge off
   on an open PR with a violation, which ends ``failed: provenance``.
2. **Settles** develop once, only if at least one PR merged. ``close-refs``
   closes any issue a merged PR closes that GitHub left open (issue #4002),
   before the lock is taken, since it touches only GitHub. Then, under the
   checkout lock (``lib/checkout_lock.py``, spec §6.4), it syncs the main
   worktree to ``origin/develop``, cleans up merged branches and worktrees,
   provisions the dev image, and runs one ``vrg-container-run --
   vrg-validate`` on the synced head. The lock is let go before the CD
   report, so it never covers a wait on GitHub.
3. **Reports** the synced head's CD run as ``CD: <state> <url>``, without
   waiting on it (spec §6.5). ``--wait-cd`` blocks on that run instead,
   bounded by ``$VRG_CD_TIMEOUT`` (default 5400s), judging its failed jobs as
   the release's develop-CD gate does (a docs-only failure is a warning). A
   run still in progress at the deadline is "result unknown", never a failed
   run, and still fails the CD check (issue #3316); a run cancelled by a
   concurrency supersede is "superseded", a loud warning, never a failure
   (issue #3834).
4. **Reports** every PR as one row of a table (PR, branch, type, outcome,
   title), then the reason and what to do next for each unmerged PR, and the
   validate's result. A develop that
   did not validate is said loudly, with its head SHA, the commits the sync
   brought in, the validate's log path and its last lines.
5. **Releases**, with ``--release``, only on full success: ``vrg-release``,
   which installs as part of every release. It starts after the
   lock is let go, so the release's own preflight can take it, and it
   fast-forwards develop itself (spec §6.6).

**Full success** means every PR merged, the validate passed and, with
``--wait-cd``, CD is green. A PR that merged with a provenance violation is
reported loudly and blocks full success, so no chained release runs: a human
must look at it before any release. A provenance violation always fails;
there is no override (vergil-tooling#4695).

**Selecting the PRs.** ``vrg-finalize-pr <pr>[,<pr>…]`` takes a comma list of
PR numbers or URLs (one PR is a list of one); ``--all`` takes every open PR
whose branch has a worktree in ``.worktrees/``; with neither, the run picks
interactively from those PRs (a real terminal is needed) and offers the
settle phase alone when there are none.

**``--cleanup-only``** settles develop with no PRs and no watch: the sync,
cleanup, provision and validate under the lock, then the CD report. It never
reads stdin. ``vrg-release`` runs it, and so does a human after an automated
job merges on GitHub. Its exit codes are a watched run's.

**Exit status:**

- 0: full success;
- 1: a tool or usage error, including a settle step other than the validate
  that failed (close-refs, the sync and cleanup), or a removed flag;
- 3: develop is healthy, but not every PR merged (or one merged with a
  provenance violation);
- 4: develop did not validate: the validate failed or could not run (the dev
  image could not be provisioned included), or, with ``--wait-cd``, CD did
  not succeed. 4 takes precedence over 1 and 3;
- 5: full success, but the chained release failed.

**Removed flags** are refused by name before any side effect:
``--strategy`` (submit chooses the merge method), ``--no-wait-cd`` (not
waiting is the default), ``--skip-post-checks`` (one run watches the whole
batch, then settles develop once), ``--install`` and ``--no-install``
(install is part of every release, spec §4.8 of epic
vergil-project/.github#1440) and ``--allow-provenance-violation`` (a
provenance violation always fails; fix its cause or the classifier).

Output of the watch and the settle phase renders through the stage-aware
progress framework (issue #1479): standalone TTY runs get the live display
with collapsed per-stage status lines and a run log at
``.vergil/vrg-finalize-pr-<stamp>.log``; piped runs fall back to the plain
renderer. The watch is a stage of its own, ``watch`` (issue #4694): on a TTY
its status table is redrawn in place each poll and the stage collapses to one
line such as ``✓ watch  3m12s — 4 merged, 0 failed`` (``⚠`` when not every PR
merged cleanly); piped, the table is printed only when some PR's state
changes, with a heartbeat line every few minutes. The run log, shared by the
watch and the settle phase, keeps every changed table with a timestamp. ``vrg-release`` passes
``--output-format plain`` explicitly because two live displays cannot nest
(issue #1470). The release runs as a subprocess after that display has closed,
so the two renderers never nest either.
"""

from __future__ import annotations

import argparse
import contextlib
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import yaml

from vergil_tooling.lib import (
    config,
    finalize_watch,
    git,
    github,
    progress,
    text_table,
    trust,
    worktrees,
)
from vergil_tooling.lib.cache_prune import clean_branch_images, prune_orphan_branch_images
from vergil_tooling.lib.cd_runs import (
    _CD_DISPATCH_TIMEOUT_SECS,
    _CD_PASSING_CONCLUSIONS,
    _CD_POLL_INTERVAL_SECS,
    _CD_WORKFLOW_NAME,
    CD_TIMEOUT_ENV,
    CDRunLookupError,
    CDRunProblem,
    CDRunSuperseded,
    _poll_for_run,
    _supersede_group,
    cd_timeout_secs,
    classify_jobs,
    wait_completed,
)
from vergil_tooling.lib.checkout_lock import checkout_lock
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.confirm import add_yes_argument, confirm
from vergil_tooling.lib.container import PackageJsonError, resolve_language
from vergil_tooling.lib.container_cache import provision_dev_image
from vergil_tooling.lib.live_branches import RepoSetError, default_repo_roots, discover_repos
from vergil_tooling.lib.open_prs import classify_open
from vergil_tooling.lib.pr_workflow import github_transport
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.progress import Stage, StageResult, StageStatus
from vergil_tooling.lib.release import gate
from vergil_tooling.lib.repo_init import prompt_choice
from vergil_tooling.lib.trusted_authors import TrustedAuthorsNotConfiguredError
from vergil_tooling.lib.versions import VersionsError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vergil_tooling.lib.finalize_watch import PRResult

_ETERNAL_BY_MODEL: dict[str, list[str]] = {
    "docs-single-branch": ["develop"],
    "library-release": ["develop", "main"],
    "application-promotion": ["develop", "release", "main"],
}

# Exit codes (spec §6.3).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_ALL_MERGED = 3
EXIT_DEVELOP_BROKEN = 4
EXIT_RELEASE_FAILED = 5
# What progress.run_pipeline returns when the run is interrupted.
EXIT_INTERRUPTED = 130

NO_WAIT_CD_REMOVED = (
    "vrg-finalize-pr: --no-wait-cd was removed; not waiting on CD is now the "
    "default (use --wait-cd to wait)"
)
STRATEGY_REMOVED = (
    "vrg-finalize-pr: --strategy was removed; finalize no longer merges. "
    "vrg-submit-pr now chooses the merge method when it turns auto-merge on "
    "(squash for a single-issue PR, rebase for a batch PR)"
)
SKIP_POST_CHECKS_REMOVED = (
    "vrg-finalize-pr: --skip-post-checks was removed; one run now watches every "
    "listed PR, then settles develop once (pass the PRs as a comma list)"
)
INSTALL_REMOVED = "vrg-finalize-pr: --install was removed; install is part of every release"
NO_INSTALL_REMOVED = "vrg-finalize-pr: --no-install was removed; install is part of every release"
ALLOW_PROVENANCE_VIOLATION_REMOVED = (
    "vrg-finalize-pr: --allow-provenance-violation was removed; a provenance "
    "violation always fails. Fix its cause, or fix the classifier if the "
    "violation is a false one"
)

# The ``vrg-release`` command the cascade runs, the one source for it: install
# is part of every release, so ``--release`` always runs it plain. Every hint
# that names the command prints this same tuple, so a hint cannot drift from
# what runs.
RELEASE_CMD: tuple[str, ...] = ("vrg-release",)

# How many trailing lines of a failed validate's output the report repeats.
VALIDATE_TAIL_LINES = 40

# The run log a failed validate's full output is written to, under .vergil/.
_VALIDATE_LOG_COMMAND = "vrg-finalize-validate"

# The report's PR table, laid out like vrg-repo-status's PR tables (issue #4708).
_REPORT_COLUMNS = ("PR", "BRANCH", "TYPE", "OUTCOME", "TITLE")

_NEXT_STEP = "hand it to an agent, then rerun `vrg-finalize-pr {pr}`"
_INVESTIGATE = "investigate before re-enabling auto-merge"
_NEXT_STEP_BY_OUTCOME: dict[str, str] = {
    "failed: auto-merge off": _INVESTIGATE,
    "failed: provenance": _INVESTIGATE,
    "failed: cancelled": "re-run its cancelled checks, then rerun `vrg-finalize-pr {pr}`",
    "failed: not a develop PR": (
        "finalize watches develop PRs only; a release PR goes through vrg-release"
    ),
}


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    refusal = _flag_refusal(args)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return EXIT_ERROR

    if not git.is_main_worktree():
        main_root = git.main_worktree_root()
        print(
            f"ERROR: vrg-finalize-pr must be run from the main worktree at {main_root},\n"
            "  not from a secondary worktree. The script removes worktrees during cleanup\n"
            "  and cannot safely do so when the calling shell's CWD is inside one.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    root = git.repo_root()

    # A release cascade on a repo that is not released is refused before any
    # watch or cleanup, so nothing changes (issue #4175).
    if args.release:
        refusal = gate.unreleased_refusal(root)
        if refusal is not None:
            print(f"vrg-finalize-pr --release: {refusal}", file=sys.stderr)
            return EXIT_ERROR

    if args.cleanup_only:
        return _run_settle_only(args, root)

    if args.all_prs:
        try:
            prs = _resolve_open_prs(root)
        except (
            trust.TrustReadError,
            TrustedAuthorsNotConfiguredError,
            worktrees.BranchPRError,
        ) as exc:
            print(f"vrg-finalize-pr --all: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if not prs:
            print("vrg-finalize-pr --all: no open PRs found in worktrees.")
            return EXIT_OK
    elif args.pr is not None:
        prs = _parse_pr_list(args.pr)
        if not prs:
            print(f"vrg-finalize-pr: no PR in {args.pr!r}", file=sys.stderr)
            return EXIT_ERROR
    else:
        try:
            picked = _infer_pr(root, args.target_branch, assume_yes=args.yes)
        except SystemExit as exc:
            if exc.code == 0:
                return EXIT_OK
            raise
        except (
            trust.TrustReadError,
            TrustedAuthorsNotConfiguredError,
            worktrees.BranchPRError,
        ) as exc:
            print(f"vrg-finalize-pr: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if picked is None:
            return _run_settle_only(args, root)
        prs = [picked]
    return _run_watched(args, root, prs)


def _flag_refusal(args: argparse.Namespace) -> str | None:
    """The refusal for a removed flag or a contradictory set of flags, or ``None``.

    Checked before any side effect, so a caller still passing a removed flag
    is told what replaced it rather than having it silently accepted (spec
    §6.10).
    """
    if args.no_wait_cd:
        return NO_WAIT_CD_REMOVED
    if args.strategy is not None:
        return STRATEGY_REMOVED
    if args.skip_post_checks:
        return SKIP_POST_CHECKS_REMOVED
    if args.install:
        return INSTALL_REMOVED
    if args.no_install:
        return NO_INSTALL_REMOVED
    if args.allow_provenance_violation:
        return ALLOW_PROVENANCE_VIOLATION_REMOVED
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Watch a batch of auto-merging PRs, then settle develop once."
    )
    # A PR list, --all and --cleanup-only each name what the run settles.
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "pr",
        nargs="?",
        default=None,
        help="PR number or URL, or a comma list of them, to watch. Omit to pick interactively.",
    )
    target.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Settle develop with no PRs: sync, cleanup, provision and validate, "
        "then the CD report. Never prompts or reads stdin.",
    )
    target.add_argument(
        "--all",
        dest="all_prs",
        action="store_true",
        help="Watch every open PR whose branch has a worktree in .worktrees/.",
    )
    parser.add_argument("--target-branch", default="develop", help="Target branch to switch to")
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
        help="On full success, chain into vrg-release (install is part of every release)",
    )
    parser.add_argument(
        "--wait-cd",
        action="store_true",
        help="Block on the CD workflow run for the synced head and fail if it "
        "does not succeed. By default finalize reports that run's link and "
        "state without waiting.",
    )
    # Removed flags. Kept hidden so main can refuse each by name, rather than
    # argparse rejecting it as unknown.
    parser.add_argument("--no-wait-cd", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--strategy", default=None, help=argparse.SUPPRESS)
    parser.add_argument("--skip-post-checks", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-install", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-provenance-violation", action="store_true", help=argparse.SUPPRESS)
    add_yes_argument(parser)
    progress.add_progress_args(parser, ())
    return parser.parse_args(argv)


def _parse_pr_list(value: str) -> list[str]:
    """Split a comma-separated PR argument into trimmed, non-empty tokens."""
    return [tok.strip() for tok in value.split(",") if tok.strip()]


def _resolve_open_prs(root: Path) -> list[str]:
    """Return the URLs of every open PR in canonical worktrees, branch-sorted.

    Deterministic order (by branch name) so a batch is reproducible. Skips
    worktrees with no open PR, printing why — no silent exclusions. Only a
    same-repo PR with a trusted author counts (``worktrees.open_pr_for_branch``):
    a fork PR on the same branch name is reported, never adopted.
    """
    urls: list[str] = []
    for wt in sorted(worktrees.list_worktrees(root), key=lambda w: w.branch):
        pr = worktrees.open_pr_for_branch(wt.branch)
        if pr is None:
            print(f"  {wt.path.name}: no open PR for {wt.branch} — skipping")
            continue
        urls.append(pr.url)
    return urls


def _run_watched(args: argparse.Namespace, root: Path, prs: list[str]) -> int:
    """Watch *prs*, settle develop once if any merged, report, then maybe release."""
    if args.dry_run:
        print(f"[dry-run] would watch until each merges or fails: {', '.join(prs)}")
        print("[dry-run] the settle phase below runs only if at least one PR merges")
        settle = _settle(args, root, merged_prs=list(prs))
        return _maybe_release(args, root, _settle_exit(settle))
    log = progress.RunLog("vrg-finalize-pr", root)
    try:
        try:
            results = progress.run_stage(
                "watch",
                lambda: finalize_watch.watch(prs),
                args=args,
                log=log,
                describe=_describe_watch,
            )
        except (
            finalize_watch.FinalizeWatchError,
            TrustedAuthorsNotConfiguredError,
            ValueError,
        ) as exc:
            # A provenance check with no trusted-authors configuration stops
            # the watch like any other failed read: one line, never a traceback.
            print(f"vrg-finalize-pr: the watch stopped: {exc}", file=sys.stderr)
            return EXIT_ERROR
        merged = [r.pr for r in results if r.outcome == "merged"]
        if not merged:
            _print_pr_report(results)
            print("\nNo PR merged: the settle phase was skipped.")
            print(f"   full log → {log.path}")
            return _maybe_release(args, root, _watch_exit(results))
        settle = _settle(args, root, merged_prs=merged, log=log)
    finally:
        log.close()
    _print_pr_report(results)
    _print_settle_report(settle)
    rc = _settle_exit(settle)
    if rc == EXIT_OK:
        rc = _watch_exit(results)
    return _maybe_release(args, root, rc)


def _run_settle_only(args: argparse.Namespace, root: Path) -> int:
    """Settle develop with no PRs and no watch (``--cleanup-only``), then maybe release."""
    settle = _settle(args, root, merged_prs=[])
    _print_settle_report(settle)
    return _maybe_release(args, root, _settle_exit(settle))


def _describe_watch(results: Sequence[PRResult]) -> tuple[StageStatus, str]:
    """The watch stage's status line: ``ok`` only when the watch alone allows full success."""
    status: StageStatus = "ok" if _watch_exit(results) == EXIT_OK else "warn"
    return status, finalize_watch.summarize(results)


def _blocking_violation(result: PRResult) -> bool:
    """Whether *result* merged with a provenance violation, which blocks full success.

    A violation that has already merged cannot be undone, so a human must
    look at it before any release (vergil-tooling#4662, #4695).
    """
    return result.outcome == "merged" and result.provenance_violation


def _watch_exit(results: Sequence[PRResult]) -> int:
    """0 when every PR merged with no provenance violation, else 3."""
    if all(r.outcome == "merged" and not _blocking_violation(r) for r in results):
        return EXIT_OK
    return EXIT_NOT_ALL_MERGED


def _pr_number(result: PRResult) -> str:
    """The PR cell: ``#<n>`` from the PR's URL."""
    return f"#{result.pr.rstrip('/').rsplit('/', 1)[-1]}"


def _outcome_cell(result: PRResult) -> str:
    """The OUTCOME cell: the watch's outcome, flagged when a merged PR broke provenance."""
    if _blocking_violation(result):
        return "merged, PROVENANCE VIOLATION"
    return result.outcome


def _pr_row(result: PRResult) -> tuple[str, ...]:
    """One report row: PR, BRANCH, TYPE, OUTCOME and TITLE, typed as ``vrg-repo-status`` does."""
    _, type_label = classify_open(result.title, result.branch, result.labels)
    return (
        _pr_number(result),
        result.branch,
        type_label,
        _outcome_cell(result),
        text_table.truncate(result.title),
    )


def _pr_notes(result: PRResult) -> list[str]:
    """The lines under the table for *result*: why it did not merge and what to do next."""
    number = _pr_number(result)
    if _blocking_violation(result):
        return [
            f"{number} PROVENANCE VIOLATION ({result.detail}): this run is not a full "
            "success, and a human must look before any release"
        ]
    if result.outcome == "merged":
        return []
    reason = f"{result.outcome}: {result.detail}" if result.detail else result.outcome
    step = _NEXT_STEP_BY_OUTCOME.get(result.outcome, _NEXT_STEP).format(pr=result.pr)
    return [f"{number} {result.pr} ({reason})", f"  next: {step}"]


def _print_pr_report(results: Sequence[PRResult]) -> None:
    """One table row per watched PR, then a note for each PR that needs a human.

    The table follows ``vrg-repo-status``'s PR tables (issue #4708): PR,
    BRANCH, TYPE, OUTCOME and TITLE. Each PR that did not merge gets its
    reason and next step beneath it, and a merged PR with a provenance
    violation is named loudly.
    """
    print("\nvrg-finalize-pr report")
    print(text_table.render(_REPORT_COLUMNS, [_pr_row(r) for r in results], indent="  "))
    notes = [line for r in results for line in _pr_notes(r)]
    if notes:
        print()
        for line in notes:
            print(f"  {line}")


@dataclass(frozen=True)
class SettleOutcome:
    """How the settle phase ended: its stages' results and what they recorded."""

    ctx: FinalizeContext
    results: tuple[StageResult, ...]
    rc: int

    def status(self, stage: str) -> str | None:
        """The status of the stage named *stage*, or ``None`` when it never ran."""
        for result in self.results:
            if result.name == stage:
                return result.status
        return None

    def error(self, stage: str) -> str:
        """The recorded error of the stage named *stage*, or ``""``."""
        for result in self.results:
            if result.name == stage:
                return result.error or ""
        return ""


def _settle(
    args: argparse.Namespace,
    root: Path,
    *,
    merged_prs: list[str],
    log: progress.RunLog | None = None,
) -> SettleOutcome:
    """Run the settle phase's stages and surface any stuck worktrees.

    *log* is the run log the watch wrote to, which the settle phase goes on
    writing; with none, the pipeline opens its own.

    The checkout lock is taken by the ``checkout-lock`` stage, after
    close-refs, and let go by the CD check, or by the ``with`` block here when
    a stage before it stops the pipeline. Either way it is let go before this
    returns, so a chained release can take it.
    """
    purpose = "vrg-finalize-pr settle" if merged_prs else "vrg-finalize-pr --cleanup-only"
    ctx = FinalizeContext(args=args, root=root, merged_prs=merged_prs, lock_purpose=purpose)
    results: list[StageResult] = []
    with ctx.lock_stack:
        rc = progress.run_pipeline(
            ctx,
            build_stages(close_refs=bool(merged_prs)),
            command="vrg-finalize-pr",
            label="vrg-finalize-pr",
            args=args,
            repo_root=root,
            results_sink=results,
            log=log,
            # A failed settle exits 1, 4 or 130, depending on which stage
            # failed (see _settle_exit). One status given before any stage
            # runs cannot say which, so the summary names none (#4712).
            failure_exit=None,
        )

    # Surface — and, with --clean-dirty, guardedly clear — any merged/closed
    # worktrees the sweep could not remove (issue #2348). Runs after the
    # pipeline has torn down its live display so the report (and the
    # confirmation prompt) reaches the human on real stdio, and regardless of
    # rc so a stuck worktree is never hidden behind a failed later stage.
    _handle_stuck_worktrees(ctx)
    return SettleOutcome(ctx=ctx, results=tuple(results), rc=rc)


def _validate_failed(settle: SettleOutcome) -> bool:
    """Whether the validate failed or could not run (a provision failure stops it)."""
    return settle.status("validation") == "failed" or settle.status("provision") == "failed"


def _develop_broken(settle: SettleOutcome) -> bool:
    """Whether nothing has shown develop healthy: the validate failed or could not run.

    A provision failure stops the pipeline before the validate, so it could
    not run. With ``--wait-cd``, a CD check that failed counts too.
    """
    if _validate_failed(settle):
        return True
    return bool(settle.ctx.args.wait_cd) and settle.status("cd-check") == "failed"


def _settle_exit(settle: SettleOutcome) -> int:
    """The settle phase's exit code: 4 when develop is broken, 1 for any other failure."""
    if settle.rc == EXIT_INTERRUPTED:
        return settle.rc
    if _develop_broken(settle):
        return EXIT_DEVELOP_BROKEN
    if settle.rc != 0:
        return EXIT_ERROR
    return EXIT_OK


def _print_settle_report(settle: SettleOutcome) -> None:
    """State the validate's result and the CD run; say loudly when develop is broken."""
    ctx = settle.ctx
    branch = ctx.args.target_branch
    if ctx.args.dry_run:
        return
    validation = settle.status("validation")
    if validation == "ok":
        print(f"  Validate: passed on {branch} at {ctx.synced_to or '(unknown head)'}")
    elif validation is None:
        why = settle.error("provision") or "an earlier settle stage failed"
        print(f"  Validate: did not run ({why})")
    if ctx.cd_line:
        print(f"  {ctx.cd_line.strip()}")
    if not _develop_broken(settle):
        return
    print()
    if _validate_failed(settle):
        print(f"ERROR: {branch} DID NOT VALIDATE. Fix it before anything else merges or releases.")
    else:
        print(
            f"ERROR: {branch}'s CD did not succeed (--wait-cd). "
            "Fix it before anything else merges or releases."
        )
    print(f"  Head: {ctx.synced_to or '(unknown)'}")
    print("  Commits the sync brought in:")
    for line in _synced_commits(ctx):
        print(f"    {line}")
    if settle.status("validation") == "failed":
        print(f"  Validate: {settle.error('validation')}")
    elif settle.status("provision") == "failed":
        print(f"  Validate could not run: {settle.error('provision')}")
    if settle.status("cd-check") == "failed" and ctx.args.wait_cd:
        print(f"  CD: {settle.error('cd-check')}")
    if ctx.validate_log is not None:
        print(f"  Validate log: {ctx.validate_log}")
    for label, tail in ctx.validate_tails:
        print(f"  Last {len(tail)} lines of the validate's {label}:")
        for line in tail:
            print(f"    {line}")


def _synced_commits(ctx: FinalizeContext) -> list[str]:
    """The commits the sync brought in, one ``<sha> <subject>`` line each."""
    if not ctx.synced_from or not ctx.synced_to:
        return ["(unknown: the sync did not record its range)"]
    if ctx.synced_from == ctx.synced_to:
        return [f"(none: {ctx.args.target_branch} was already at this head)"]
    try:
        log = git.read_output("log", "--oneline", f"{ctx.synced_from}..{ctx.synced_to}")
    except subprocess.CalledProcessError as exc:
        return [f"(could not list them: {error_text(exc)})"]
    return log.splitlines() or ["(none)"]


def _maybe_release(args: argparse.Namespace, root: Path, rc: int) -> int:
    """Chain into the release on full success with ``--release``; 5 when it fails."""
    if rc != EXIT_OK or not args.release:
        if args.release and rc != EXIT_OK:
            print(f"\nRelease skipped: the run is not a full success (exit {rc}).")
        return rc
    if args.dry_run:
        print(f"\n[dry-run] would chain into: {shlex.join(RELEASE_CMD)}")
        return EXIT_OK
    if _chain_release(root, RELEASE_CMD) != 0:
        return EXIT_RELEASE_FAILED
    return EXIT_OK


class FinalizeError(Exception):
    """Stage failure with a human-readable reason.

    Raised by stage functions to mark the stage failed; run_pipeline
    records the message in the stage result and the final summary.
    """


@dataclass
class FinalizeContext:
    """State threaded through the settle phase's stages."""

    args: argparse.Namespace
    root: Path
    # The PRs the watch saw merge, whose closing references and branches the
    # settle phase handles. Empty for --cleanup-only.
    merged_prs: list[str] = field(default_factory=list)
    lock_purpose: str = "vrg-finalize-pr --cleanup-only"
    # The target branch's head before and after the sync, for the CD check
    # and for the report of a develop that did not validate.
    synced_from: str = ""
    synced_to: str = ""
    deleted: list[str] = field(default_factory=list)
    # Merged/closed worktrees the sweep could not remove — dirty, or a
    # reused branch name with unmerged commits (issue #1719). Collected in
    # _stage_cleanup and surfaced (and optionally cleared) after the pipeline
    # tears down its live display, so the skip reason is impossible to miss
    # instead of buried in the collapsed stage log (issue #2348).
    stuck: list[worktrees.WorktreeStatus] = field(default_factory=list)
    # The CD line the cd-check stage reported, repeated in the final report.
    cd_line: str = ""
    # A failed validate's full output log and its last lines.
    validate_log: Path | None = None
    # One (label, lines) pair per stream that had output, stdout first.
    validate_tails: list[tuple[str, list[str]]] = field(default_factory=list)
    # The checkout lock, taken by the checkout-lock stage. _settle closes it
    # after the pipeline; cd-check closes it first, so the lock never covers
    # a wait on CD (spec §6.4).
    lock_stack: contextlib.ExitStack = field(default_factory=contextlib.ExitStack)


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
    pairs: list[tuple[worktrees.Worktree, trust.PRRef]] = []
    for wt in worktrees.list_worktrees(root):
        pr = worktrees.open_pr_for_branch(wt.branch)
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
        labels = [f"PR #{p.number} ({w.branch}): {p.title}" for w, p in pairs]
        chosen = prompt_choice("Multiple PRs ready to finalize", labels)
        wt, pr = pairs[labels.index(chosen)]

    if not confirm(
        f"Finalize PR #{pr.number} ({pr.title})?",
        assume_yes=assume_yes,
        default=False,
    ):
        print("Aborted.")
        raise SystemExit(0)
    return pr.url


NO_CD_WORKFLOW_LINE = "  CD: no CD workflow in this repo"
NO_CD_RUN_LINE = "  CD: no CD run for this head"


def _declares_cd_workflow(root: Path) -> bool | None:
    """Whether a workflow under *root*'s ``.github/workflows`` is named ``CD``.

    ``True`` when a ``*.yml`` or ``*.yaml`` there declares ``name: CD``, the
    workflow ``gh run list --workflow CD`` looks up; ``False`` when none does
    (no such directory included). ``None`` when a workflow file cannot be read
    or parsed and none read declares it, so the answer is not known: the
    caller then reports the failed lookup as it is rather than reading it as
    a repo with no CD.
    """
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return False
    files = sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])
    unknown = False
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except OSError, UnicodeDecodeError, yaml.YAMLError:
            unknown = True
            continue
        if isinstance(data, dict) and data.get("name") == _CD_WORKFLOW_NAME:
            return True
    return None if unknown else False


def _wait_for_cd_run(
    target_branch: str,
    head_sha: str,
    *,
    root: Path,
    dispatch_timeout: float = _CD_DISPATCH_TIMEOUT_SECS,
    watch_timeout: float | None = None,
    poll_interval: float = _CD_POLL_INTERVAL_SECS,
) -> CDRunProblem | CDRunSuperseded | None:
    """Block until the CD run triggered by ``head_sha`` completes.

    The merge's base-branch push dispatches CD asynchronously, so this first
    polls ``gh run list`` (up to ``dispatch_timeout``) for the run whose
    ``headSha`` matches the synced head — not "latest run on branch," which
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
      - the CD runs cannot be listed: a repo with no CD workflow
        (:func:`_declares_cd_workflow` on *root*) is reported as
        ``CD: no CD workflow in this repo``, any other failure as
        ``CD: unknown`` and marked ⚠ (:func:`_report_cd_lookup_failure`),
      - no run was dispatched for this SHA within ``dispatch_timeout``
        (a change CD legitimately skips, e.g. path filters), or
      - the run concluded success / skipped / neutral.
    """
    if watch_timeout is None:
        watch_timeout = cd_timeout_secs()
    # Phase 1: wait for the run this merge triggered to register.
    try:
        run = _poll_for_run(
            target_branch,
            head_sha,
            repo=None,
            dispatch_timeout=dispatch_timeout,
            poll_interval=poll_interval,
        )
    except CDRunLookupError as exc:
        # Nothing to block on: a repo with no CD workflow passes quietly, as
        # it always has; any other failed lookup is said, not hidden.
        _report_cd_lookup_failure(exc, head_sha, root=root)
        return None
    if run is None:
        print(
            f"  No CD workflow run registered for {head_sha[:7]} within "
            f"{int(dispatch_timeout)}s — assuming this change triggers no "
            "CD run and not blocking.",
            file=sys.stderr,
        )
        return None

    run_id = run.get("databaseId")
    url = str(run.get("url") or "")
    print(f"  Waiting on CD workflow run {run_id} for {head_sha[:7]} ...")
    if url:
        print(f"  {url}")

    # Phase 2: watch the registered run to completion.
    view = wait_completed(run_id, timeout=watch_timeout, poll_interval=poll_interval)
    if view is None:
        return CDRunProblem(run_id, target_branch, head_sha, url, None, int(watch_timeout))
    url = str(view.get("url") or url)
    conclusion = str(view.get("conclusion") or "")
    if conclusion in _CD_PASSING_CONCLUSIONS:
        return None
    note = ""
    if conclusion == "cancelled":
        try:
            group = _supersede_group(run_id, url)
        except (subprocess.CalledProcessError, ValueError) as exc:
            # JSONDecodeError is a ValueError. The cancel stays a failure,
            # with the reason it could not be cleared.
            group = None
            note = (
                "finalize could not tell whether a concurrency supersede "
                f"cancelled it: {error_text(exc)}"
            )
        if group is not None:
            return CDRunSuperseded(run_id, target_branch, head_sha, url, group)
    return CDRunProblem(
        run_id, target_branch, head_sha, url, conclusion or "unknown", int(watch_timeout), note
    )


def _classify_failed_jobs(run_id: object) -> tuple[list[str], list[str]]:
    """Split failed CD run *run_id*'s jobs into ``(blocking, deferred)`` failures.

    The split is :func:`classify_jobs`'s, the one the release's develop-CD gate
    uses. A ``gh`` failure (``subprocess.CalledProcessError``) or a payload of
    the wrong shape (``ValueError``) propagates.
    """
    data = github.read_json("run", "view", str(run_id), "--json", "jobs")
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        msg = f"gh run view {run_id} returned no jobs list"
        raise ValueError(msg)
    return classify_jobs(jobs)


def _report_cd_lookup_failure(exc: CDRunLookupError, head_sha: str, *, root: Path) -> str:
    """Report a CD runs listing that failed; return the line reported.

    A repo whose workflows declare no CD (:func:`_declares_cd_workflow`) has
    no CD to report: one plain line, no warning. Any other failure is
    ``CD: unknown`` with the reason, and marks the stage ⚠.
    """
    if _declares_cd_workflow(root) is False:
        print(NO_CD_WORKFLOW_LINE)
        return NO_CD_WORKFLOW_LINE
    line = f"  CD: unknown (the CD runs could not be listed: {exc})"
    print(line, file=sys.stderr)
    progress.mark_warn(f"CD state of {head_sha[:7]} unknown: the CD runs could not be listed")
    return line


def _report_cd_run(
    target_branch: str,
    head_sha: str,
    *,
    root: Path,
    dispatch_timeout: float = _CD_DISPATCH_TIMEOUT_SECS,
    poll_interval: float = _CD_POLL_INTERVAL_SECS,
) -> str:
    """Print the CD run for *head_sha* and its state now, without waiting on it.

    The default CD check (spec §6.5 of epic vergil-project/.github#1218). The
    run is found by its commit, polling up to *dispatch_timeout* for it to
    register, and reported as one line, ``CD: <state> <url>``, the state being
    queued, in progress, succeeded, failed, cancelled or superseded. No run
    registered in time is ``CD: no CD run for this head``, and a repo with no
    CD workflow (*root* declares none) is ``CD: no CD workflow in this repo``.
    A state that is not green, and any other lookup that fails, marks the
    stage ⚠ and never fails it: the release's develop-CD gate is what
    refuses a develop whose CD is not green. Returns the ``CD:`` line it
    printed.
    """
    try:
        run = _poll_for_run(
            target_branch,
            head_sha,
            repo=None,
            dispatch_timeout=dispatch_timeout,
            poll_interval=poll_interval,
        )
    except CDRunLookupError as exc:
        return _report_cd_lookup_failure(exc, head_sha, root=root)
    if run is None:
        print(NO_CD_RUN_LINE)
        return NO_CD_RUN_LINE

    url = str(run.get("url") or "")
    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    note = ""
    if status != "completed":
        # Every status short of in_progress (queued, pending, waiting,
        # requested) reads as queued.
        state = "in progress" if status == "in_progress" else "queued"
    elif conclusion in _CD_PASSING_CONCLUSIONS:
        state = "succeeded"
    elif conclusion == "cancelled":
        try:
            group = _supersede_group(run.get("databaseId"), url)
        except (subprocess.CalledProcessError, ValueError) as exc:
            group = None
            note = (
                "finalize could not tell whether a concurrency supersede "
                f"cancelled it: {error_text(exc)}"
            )
        state = "cancelled" if group is None else "superseded"
    else:
        state = "failed"
    line = f"  CD: {state} {url}".rstrip()
    print(line)
    if note:
        print(f"  {note}", file=sys.stderr)
    if state in ("failed", "cancelled", "superseded"):
        progress.mark_warn(f"CD run {run.get('databaseId')} {state} for {head_sha[:7]}")
    return line


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
    """Close any issue a merged PR closes that GitHub left open (issue #4002).

    GitHub sometimes skips the closing-keyword auto-close, leaving a merged
    task open, which blocks epic rollup. Only the issues GitHub itself lists
    as a PR's closing references are touched, never ones it merely mentions.
    Each issue closed is reported. Every merged PR is handled, then any
    failed read or close fails the stage, naming each PR it failed on. It
    runs before the checkout lock is taken: it touches only GitHub.
    """
    failures: list[str] = []
    for pr in ctx.merged_prs:
        try:
            _close_refs_of(pr, dry_run=ctx.args.dry_run)
        except FinalizeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            failures.append(str(exc))
    if failures:
        raise FinalizeError("; ".join(failures))


def _close_refs_of(pr: str, *, dry_run: bool) -> None:
    """Close the closing-reference issues of merged *pr* that GitHub left open."""
    if dry_run:
        print(f"  [dry-run] close any closing-reference issue of PR {pr} that GitHub left open")
        return
    refs = _read_closing_refs(pr)
    if refs.state != "MERGED":
        msg = f"PR {refs.ref} is {refs.state}, not MERGED; its closing references were not checked"
        raise FinalizeError(msg)
    if not refs.issues:
        print(f"PR {refs.ref} has no closing issue references.")
        return
    if any(issue.state == "OPEN" for issue in refs.issues):
        time.sleep(_AUTO_CLOSE_GRACE_SECS)
        refs = _read_closing_refs(pr)
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


def _head_of(branch: str) -> str:
    """The commit *branch* points at; a failed read fails the stage."""
    try:
        return git.read_output("rev-parse", branch).strip()
    except subprocess.CalledProcessError as exc:
        msg = f"could not read the head of {branch}: {error_text(exc)}"
        raise FinalizeError(msg) from exc


def _confirmed_merged_branch(repo: str, number: int, branch: str) -> bool:
    """True when *repo*#*number* is a merged same-repo PR with a trusted author on *branch*.

    The branch read from the PR is only a candidate: it is looked up through
    :func:`trust.prs_for_branch`, and only a merged PR with *number* itself
    counts, so a fork PR from a branch of the same name never names a local
    branch to delete. The skipped PRs are reported on stderr, metadata only;
    a read the layer cannot complete raises :class:`trust.TrustReadError`.
    """
    prs, skipped = trust.prs_for_branch(repo, branch, state="merged")
    trust.report_skipped(skipped, stream=sys.stderr)
    return any(pr.number == number for pr in prs)


def _merged_branches(ctx: FinalizeContext) -> list[str]:
    """The head branch names of the PRs the watch saw merge; a failed read fails the stage.

    A branch is named only once the trust layer confirms its PR
    (:func:`_confirmed_merged_branch`); a PR it does not confirm is reported
    and its branch is left alone.
    """
    if ctx.args.dry_run:
        if ctx.merged_prs:
            print("  [dry-run] delete each merged PR's local branch and worktree")
        return []
    branches: list[str] = []
    for pr in ctx.merged_prs:
        try:
            repo, number = github.pr_repo_and_number(pr, "the merged-branch cleanup")
            branch = github.head_ref(pr).strip()
        except subprocess.CalledProcessError as exc:
            msg = f"could not read the head branch of merged PR {pr}: {error_text(exc)}"
            raise FinalizeError(msg) from exc
        if not branch:
            continue
        try:
            confirmed = _confirmed_merged_branch(repo, number, branch)
        except (trust.TrustReadError, TrustedAuthorsNotConfiguredError) as exc:
            msg = f"could not confirm merged PR {pr} through the trust layer: {exc}"
            raise FinalizeError(msg) from exc
        if not confirmed:
            print(
                f"  Merged PR {pr} is not a same-repo PR with a trusted author on its "
                "branch; its local branch and worktree are kept."
            )
            continue
        branches.append(branch)
    return branches


def _stage_lock(ctx: FinalizeContext) -> None:
    """Take the checkout lock for the sync, cleanup, provision and validate (spec §6.4).

    Waits, saying so, while another run holds it. The lock goes on
    ``ctx.lock_stack``: the CD check lets go of it, or ``_settle`` does when a
    stage before the CD check stops the pipeline.
    """
    ctx.lock_stack.enter_context(checkout_lock(ctx.root, purpose=ctx.lock_purpose))


def _stage_cleanup(ctx: FinalizeContext) -> None:
    """Switch to the target branch, sync, and prune merged branches,
    worktrees, and remote-tracking references.

    Records the target branch's head before and after the sync on *ctx*.
    The checkout-lock stage before it holds the lock (``lib/checkout_lock.py``)
    around this stage; the stage never takes it itself.
    """
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
    if not args.dry_run:
        ctx.synced_from = _head_of(args.target_branch)
    _run(["fetch", "--tags", "--force", "origin", args.target_branch], dry_run=args.dry_run)
    # ff-merge the remote-tracking ref instead of `pull`: FETCH_HEAD is
    # written non-atomically, so a `pull` racing a concurrent fetch can
    # see two merge candidates and abort (issue #1499). The
    # remote-tracking ref is updated atomically.
    _run(["merge", "--ff-only", f"origin/{args.target_branch}"], dry_run=args.dry_run)
    if not args.dry_run:
        ctx.synced_to = _head_of(args.target_branch)
        print(f"  {args.target_branch} is at {ctx.synced_to or '(unknown head)'}")

    deleted = ctx.deleted

    # Explicit-target cleanup: the merged PRs' branches. A squash merge
    # rewrites history onto the target, so such a branch is never an
    # ancestor and `git branch --merged` cannot see it, and a branch with no
    # canonical worktree is not seen by the worktree arm below either.
    for branch in _merged_branches(ctx):
        if branch in eternal:
            continue
        if git.read_output("branch", "--list", branch):
            print(f"Cleaning up merged PR branch {branch}...")
            if _delete_branch_and_worktree(branch, root, dry_run=args.dry_run):
                deleted.append(branch)
        else:
            print(f"  Merged PR branch {branch} has no local branch — skipping.")

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
        # explicit-target step above, which keeps its own behavior. A fork
        # PR closed on the same branch name is no evidence.
        if worktrees.closed_pr_for_branch(branch) is None:
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

    The checkout-lock stage holds the lock (``lib/checkout_lock.py``) around
    this stage; the stage never takes it itself.
    """
    args = ctx.args
    if args.dry_run:
        print("  [dry-run] provision dev image for the target branch")
        return
    print("Provisioning dev image for post-finalization validation...")
    try:
        lang = resolve_language(ctx.root)
    except PackageJsonError as exc:
        raise FinalizeError(f"dev-image provisioning failed: {exc}") from exc
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
    try:
        lang = resolve_language(ctx.root)
    except PackageJsonError as exc:
        raise FinalizeError(f"dev-image provisioning failed: {exc}") from exc
    try:
        provision_dev_image(ctx.root, lang, quiet_warmup=True)
    except (RuntimeError, VersionsError, subprocess.CalledProcessError) as exc:
        raise FinalizeError(_rebuild_failure_msg(branch, exc, retry)) from exc


def _stage_validation(ctx: FinalizeContext) -> None:
    """Run canonical validation to catch problems on the target branch
    before the next PR is created.

    Any failure, a gate that failed or a validate that could not run, is
    recorded for the report before the stage fails: the full output goes to
    its own run log (``.vergil/vrg-finalize-validate-<stamp>.log``) and its
    last :data:`VALIDATE_TAIL_LINES` lines of each stream onto *ctx* (spec §6.3).

    The checkout-lock stage holds the lock (``lib/checkout_lock.py``) around
    this stage; the stage never takes it itself.
    """
    try:
        _run_validation(ctx)
    except (FinalizeError, OSError) as exc:
        _record_validate_failure(ctx, exc)
        raise


def _record_validate_failure(ctx: FinalizeContext, exc: BaseException) -> None:
    """Write a failed validate's output to its own run log and keep its tails on *ctx*.

    ``progress.run`` captures stdout and stderr apart, so their interleaving
    is lost. Each stream is therefore kept, logged and tailed under its own
    label, so stderr's warnings never push the end of stdout, where a failing
    gate reports, out of the tail. With no output at all, the tail is the
    failure's own message.
    """
    streams: list[tuple[str, list[str]]] = []
    cause = exc.__cause__
    if isinstance(cause, subprocess.CalledProcessError):
        for label, stream in (("stdout", cause.output), ("stderr", cause.stderr)):
            if stream:
                streams.append((label, str(stream).splitlines()))
    if not streams:
        streams.append(("failure", str(exc).splitlines()))
    log = progress.RunLog(_VALIDATE_LOG_COMMAND, ctx.root)
    try:
        log.write(str(exc))
        for label, lines in streams:
            log.write(f"=== {label} ===")
            for line in lines:
                log.write(line)
    finally:
        log.close()
    ctx.validate_log = log.path
    ctx.validate_tails = [(label, lines[-VALIDATE_TAIL_LINES:]) for label, lines in streams]


def _run_validation(ctx: FinalizeContext) -> None:
    """Run ``vrg-validate`` in the dev container; raise :class:`FinalizeError` on failure.

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
    """Report the CD workflow run for the synced head; with ``--wait-cd``, block on it.

    The base-branch push a merge produces dispatches CD asynchronously
    (docker build/publish, docs, release). Rather than glance at the latest
    run — which may be a stale or unrelated one — this identifies the run by
    the synced head's SHA (issue #2753).

    By default it prints the run's link and its state at that moment and does
    not wait (spec §6.5 of epic vergil-project/.github#1218): the release's
    develop-CD gate is what refuses to cut from a develop whose CD is not
    green. With ``--wait-cd`` it waits for the run to complete and fails
    finalize if it concludes non-success, judging the failed jobs with
    :func:`classify_jobs` as the release gate does.

    It first lets go of the checkout lock: the lock covers local checkout
    work only, never a wait on CD (spec §6.4). The ``CD:`` line it reports is
    kept on ``ctx.cd_line`` for the final report.
    """
    ctx.lock_stack.close()
    args = ctx.args
    if args.dry_run:
        verb = "wait on" if args.wait_cd else "report"
        print(f"  [dry-run] {verb} the CD workflow run for the synced head")
        return

    # The CD run to report is the one on the head the cleanup stage synced:
    # every PR this run saw merge is in it, and so is anything merged by
    # anyone else.
    head_sha = ctx.synced_to
    if not head_sha:
        try:
            head_sha = git.read_output("rev-parse", args.target_branch).strip()
        except subprocess.CalledProcessError:
            head_sha = ""
    if not head_sha:
        # Could not resolve the SHA to associate a CD run with — cleanup has
        # already succeeded, so warn loudly rather than fail finalize over the
        # CD check, and do not silently pretend CD passed.
        print(
            f"  WARNING: could not resolve the tip of {args.target_branch} to "
            "find its CD run — skipping the CD check.",
            file=sys.stderr,
        )
        ctx.cd_line = f"CD: unknown (could not resolve the tip of {args.target_branch})"
        return

    if not args.wait_cd:
        ctx.cd_line = _report_cd_run(args.target_branch, head_sha, root=ctx.root)
        return

    problem = _wait_for_cd_run(args.target_branch, head_sha, root=ctx.root)
    if problem is None:
        ctx.cd_line = f"CD: no CD failure for {head_sha[:7]} (--wait-cd; the run is named above)"
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
        ctx.cd_line = f"CD: superseded, not confirmed: {problem.describe()}"
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
            "  Once it has succeeded, re-run: vrg-finalize-pr --cleanup-only --wait-cd\n"
            f"  If this CD is legitimately slower, raise the watch limit with "
            f"{CD_TIMEOUT_ENV}=<seconds> (currently {secs}s).",
            file=sys.stderr,
        )
        msg = f"CD run still in progress after {secs}s; result unknown"
        raise FinalizeError(msg)
    blocking: list[str] = []
    if problem.conclusion != "cancelled":
        # Judge the failed jobs as the release's develop-CD gate does: a
        # failure only of a deferred (docs) job is a loud warning, not a stop.
        try:
            blocking, deferred = _classify_failed_jobs(problem.run_id)
        except (subprocess.CalledProcessError, ValueError) as exc:
            # The run failed either way; say why its jobs could not be judged.
            print(
                f"  Could not read the jobs of CD run {problem.run_id} to classify "
                f"the failure: {error_text(exc)}",
                file=sys.stderr,
            )
        else:
            if deferred and not blocking:
                print(
                    f"WARNING: CD run {problem.run_id} failed only in deferred publish "
                    f"job(s): {', '.join(deferred)}.",
                    file=sys.stderr,
                )
                print(f"  {problem.describe()}", file=sys.stderr)
                progress.mark_warn(
                    f"CD run {problem.run_id} failed only in deferred job(s): {', '.join(deferred)}"
                )
                ctx.cd_line = (
                    f"CD: failed only in deferred job(s) {', '.join(deferred)}: "
                    f"{problem.describe()}"
                )
                return
    print(
        "ERROR: the CD workflow run triggered by this merge did not succeed.",
        file=sys.stderr,
    )
    print(f"  {problem.describe()}", file=sys.stderr)
    if blocking:
        print(f"  Failed job(s): {', '.join(blocking)}", file=sys.stderr)
    print(
        "  The post-merge publish did not complete — investigate the run above.",
        file=sys.stderr,
    )
    raise FinalizeError("CD workflow run triggered by the merge did not succeed")


def build_stages(*, close_refs: bool) -> tuple[Stage, ...]:
    """Assemble the settle phase's pipeline (spec §6.3).

    close-refs runs only when the watch saw a PR merge (*close_refs*), before
    the checkout lock is taken: it touches only GitHub. fail_defer: an issue
    left open must fail finalize, but the PRs are merged, so develop is still
    settled (issue #4002). checkout-lock then takes the lock (spec §6.4) for
    cleanup (the sync included), provision and validation. provision warms the
    target-branch image (fail_fast, before it is used: issue #2462);
    validation is fail_defer so a validation failure still lets cd-check run
    and surface the CD status. cd-check lets go of the lock first, and is
    fail_fast: with ``--wait-cd``, a CD run that did not succeed is a hard
    finalize failure (issue #2753), and it is the last stage so nothing is
    lost by stopping there.
    """
    stages: list[Stage] = []
    if close_refs:
        stages.append(Stage("close-refs", _stage_close_refs, "fail_defer"))
    stages.append(Stage("checkout-lock", _stage_lock, "fail_fast"))
    stages.append(Stage("cleanup", _stage_cleanup, "fail_fast"))
    stages.append(Stage("provision", _stage_provision, "fail_fast"))
    stages.append(Stage("validation", _stage_validation, "fail_defer"))
    stages.append(Stage("cd-check", _stage_cd_check, "fail_fast"))
    return tuple(stages)


def _chain_release(root: Path, cmd: tuple[str, ...]) -> int:
    """Hand off to ``vrg-release`` after a fully successful finalize (issue #1634).

    By the time it runs, the settle phase's pipeline has closed its live
    display (``run_pipeline``'s ``finally`` block calls ``renderer.close()``),
    so ``vrg-release`` starts a fresh renderer with no nesting (cf. issue
    #1470), and the checkout lock has been let go, so the release's preflight
    can take it (spec §6.4). Run as a subprocess inheriting the TTY, not
    ``exec``, so a caller like ``vrg-submit-pr`` regains control to report
    the cascade outcome.

    A failure here never un-merges a PR — they were already merged and
    develop settled — so report it clearly and let the human finish it with
    ``vrg-release --resume``, or re-run vrg-release alone if it stopped
    before opening its tracking issue.
    """
    print()
    print(f"--release: handing off to {shlex.join(cmd)}")
    result = subprocess.run(  # noqa: S603
        cmd,  # noqa: S607
        cwd=root,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"vrg-finalize-pr: release failed (exit {result.returncode}); "
            "the merged PRs and the settled develop are unaffected.\n"
            "  Once it has opened its tracking issue, finish it with: vrg-release --resume\n"
            f"  If it stopped before that, re-run the release alone with: {shlex.join(cmd)}",
            file=sys.stderr,
        )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
