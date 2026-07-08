"""PR submission wrapper that constructs standards-compliant PR bodies.

Supports two modes:
- **Template mode** (no CLI args): reads the PR workflow state file
  (``.vergil/pr-workflow.json``); shows a summary, prompts for
  confirmation, pushes the branch, and creates the PR.
- **CLI argument mode** (args provided): existing direct invocation
  for human emergency use.

Both modes ensure the branch is pushed using the human's host
credentials before creating the PR. Because the human is the superset
of any agent's rights, this carries workflow-touching pushes that the
agent's own credentials would be rejected for.

Agent identities are blocked — PR submission is a Chief Steward
(human) operation.

``--finalize`` chains straight into the ``vrg-finalize-pr``
wait-and-merge flow after the PR is created (issue #1491) — for cases
where the human has already decided to merge on green. The chain runs
only after the PR exists, so a submit failure leaves no half-finalized
state, and a finalize failure reports the created PR so the human can
re-run ``vrg-finalize-pr`` alone.

``--release`` extends that one step further (issue #1634): it implies
``--finalize`` and passes ``--release`` through, so a clean finalize
cascades into ``vrg-release`` — the whole submit -> finalize -> release
sequence from one command. ``--install`` extends it one link further still
(issue #1643): it implies ``--release`` and passes ``--install`` through, so
``vrg-release`` runs the consumer-refresh install commands rather than only
printing them — submit -> finalize -> release -> install. Each hop runs as a
subprocess (not ``exec``) so control returns here for the final summary,
which states how far the cascade got: submitted, submitted and finalized,
submitted/finalized/released, or submitted/finalized/released/installed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib import epics, git, github, identity_mode, worktrees
from vergil_tooling.lib.confirm import add_yes_argument, confirm
from vergil_tooling.lib.linkage import ALLOWED_LINKAGES, normalize_linkage
from vergil_tooling.lib.pr_body import build_pr_body, resolve_issue_ref
from vergil_tooling.lib.pr_workflow import batch, submission
from vergil_tooling.lib.pr_workflow.errors import AlreadySubmittedError, WorkflowError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Create a standards-compliant pull request.")
    parser.add_argument(
        "--issue", default=None, help="Issue reference: number or owner/repo#number"
    )
    parser.add_argument("--summary", default=None, help="One-line PR summary")
    parser.add_argument(
        "--linkage", default="Ref", choices=ALLOWED_LINKAGES, help="Issue linkage keyword"
    )
    parser.add_argument("--notes", default="", help="Additional notes")
    parser.add_argument("--title", default=None, help="PR title")
    parser.add_argument("--dry-run", action="store_true", help="Print without executing")
    parser.add_argument("--base", default=None, help="Override auto-detected target branch")
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="After creating the PR, chain straight into vrg-finalize-pr "
        "(wait for checks, merge, post-merge cleanup)",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="After creating the PR, run the full cascade: finalize (implies "
        "--finalize) and then vrg-release",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Run the full cascade through the install step: implies --release "
        "(hence --finalize) and passes --install through so vrg-release runs the "
        "consumer-refresh install commands (issue #1643)",
    )
    parser.add_argument(
        "--all",
        dest="all_worktrees",
        action="store_true",
        help="Select every ready worktree for a batch submission (issue #1673).",
    )
    parser.add_argument(
        "--select",
        default=None,
        help="Comma-separated list of ready worktrees to batch-submit, by issue "
        "number or worktree directory name (issue #1673).",
    )
    add_yes_argument(parser)
    return parser.parse_args(argv)


def _target_branch(base_override: str | None, oracle_base: str | None = None) -> str:
    """Resolve the PR's target branch.

    Precedence: an explicit ``--base`` always wins; otherwise the base the
    oracle recorded (``oracle_base``, ``origin/`` stripped) is honored; failing
    that, default to ``develop``.

    There is deliberately no branch-name inference here. The legacy
    ``release/`` → ``main`` special-case predated ``vrg-submit-pr`` becoming a
    human-only command and silently retargeted PRs (issue #1609); release→main
    PRs are created by the release tooling via ``github.create_pr``, never this
    path. A genuine manual release PR uses an explicit ``--base main``.
    """
    if base_override:
        return base_override
    if oracle_base:
        return oracle_base.removeprefix("origin/")
    return "develop"


def _push_branch(branch: str) -> None:
    """Push *branch* to origin, tolerating a rebased (diverged) remote.

    Submitting a PR routinely follows a rebase onto the current base
    branch to clear stale drift; that leaves any previously-pushed remote
    branch diverged, and a plain push is rejected non-fast-forward.
    ``--force-with-lease`` is the safe overwrite: it updates the remote
    only while it still matches our remote-tracking ref, so it refuses to
    clobber commits pushed elsewhere since our last fetch. Bare
    ``--force`` is never used. (Issue #1557.)
    """
    try:
        git.run("push", "--force-with-lease", "-u", "origin", branch)
    except subprocess.CalledProcessError as exc:
        msg = (
            f"vrg-submit-pr: pushing '{branch}' to origin failed.\n"
            "  --force-with-lease was refused, which means the remote branch "
            "moved since your last fetch.\n"
            "  Run `vrg-git fetch origin` and review the remote commits before "
            "retrying, so you don't overwrite someone else's work."
        )
        raise SystemExit(msg) from exc


def _create_pr(*, target_branch: str, title: str, pr_body: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(pr_body)
        tmp_path = f.name
    try:
        pr_url = github.create_pr(base=target_branch, title=title, body_file=tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return pr_url


def _chain_finalize(pr_url: str, *, release: bool = False, install: bool = False) -> int:
    """Hand off to ``vrg-finalize-pr`` right after PR creation (issue #1491).

    Equivalent to running ``vrg-finalize-pr <pr-url>`` by hand: same
    merge-strategy default, same post-merge cleanup. With *release*, passes
    ``--release`` through so a clean finalize cascades into ``vrg-release``
    (issue #1634); with *install*, also passes ``--install`` so the cascade
    runs vrg-release's consumer-refresh install step (issue #1643). Runs as a
    subprocess from the main worktree root because vrg-finalize-pr refuses to
    run from a secondary worktree (it removes worktrees during cleanup) and
    template mode chdir'd into one. The child inherits the TTY so its live
    progress display and prompts behave exactly as a manual run.

    A failure here never un-creates the PR — report the PR clearly so
    the human can re-run the finalize (or the cascade) alone.
    """
    main_root = git.main_worktree_root()
    cmd: tuple[str, ...] = ("vrg-finalize-pr", pr_url)
    if release:
        cmd = (*cmd, "--release")
    if install:
        cmd = (*cmd, "--install")
    stage = "install" if install else "release" if release else "finalize"
    print()
    print(f"--{stage}: handing off to {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=main_root, check=False)  # noqa: S603
    if result.returncode != 0:
        flags = f"{' --release' if release else ''}{' --install' if install else ''}"
        rerun = f"vrg-finalize-pr {pr_url}{flags}"
        print(
            f"vrg-submit-pr: cascade failed (exit {result.returncode}); "
            "the PR was created and is unaffected:\n"
            f"  {pr_url}\n"
            f"  Re-run the rest of the cascade alone with: {rerun}",
            file=sys.stderr,
        )
        return result.returncode
    return 0


def _print_cascade_summary(pr_url: str, *, released: bool, installed: bool) -> None:
    """Final summary owned by vrg-submit-pr stating how far the cascade got.

    Reached only after a successful chain, so it always reports completed
    work (issue #1634, extended for --install in issue #1643). The
    submitted-only outcome is reported separately by the pr-watch one-liner.
    """
    print()
    if installed:
        print(f"Done: PR submitted, finalized, released, and installed.\n  {pr_url}")
    elif released:
        print(f"Done: PR submitted, finalized, and released.\n  {pr_url}")
    else:
        print(f"Done: PR submitted and finalized.\n  {pr_url}")


def _dry_run_chain_note(*, release: bool, install: bool) -> str:
    """The ``[dry-run]`` line naming the chain command that would run."""
    flags = f"{' --release' if release else ''}{' --install' if install else ''}"
    cmd = f"vrg-finalize-pr{flags} <pr-url>"
    return f"\n[dry-run] would chain into: {cmd}"


def _print_pr_watch(pr_url: str) -> None:
    """Emit the paste-ready post-PR monitoring one-liner.

    Opening the PR auto-triggers the mechanized CI gates; this line starts the
    USER agent's monitoring loop. (The dual-agent framing was removed in #1872.)
    """
    print()
    print("Next — monitor the PR through CI:")
    print()
    print(f"    /vergil:pr-watch {pr_url}")


def _reject_if_epic_link(issue_ref: str) -> None:
    """Abort if the linkage points at an epic — PRs link a task, never an epic.

    This lives here, at PR construction time, because deciding epic-ness needs
    an authenticated, cross-repo ``gh`` call (e.g. an epic in ``.github``) —
    which vrg-submit-pr has via the App installation token. Self-scoping:
    legacy issues are never epics, so they pass.
    """
    if epics.is_epic_linkage(issue_ref, default_repo=github.current_repo()):
        raise SystemExit(
            "vrg-submit-pr: --issue links an epic; link a task, not an epic "
            "(epics are closed by rollup when their tasks complete)."
        )


def _reject_if_operational_task(issue_ref: str) -> None:
    """Abort if the linkage is an operational task — it is not PR-workable.

    An operational task (validation, deployment, …) is proven by *running* it and
    recording an ``Outcome:`` comment; it has no code PR. Refuse PR construction
    here so the guard matches the skill boundary (issue-implement redirects to the
    task's run skill). Deciding operational-ness needs the same authenticated
    ``gh`` call as the epic guard. Self-scoping: plain/legacy tasks are never
    operational, so they pass.
    """
    if epics.is_operational_task(issue_ref, default_repo=github.current_repo()):
        raise SystemExit(
            "vrg-submit-pr: --issue is an operational task (validation/deployment), "
            "which is not PR-workable; run it with its run skill (issue-validate / "
            "issue-deploy) and record the Outcome as a comment instead of opening a PR."
        )


def _task_linkage(issue_ref: str, requested: str) -> tuple[str, str | None]:
    """Choose the PR-body linkage keyword for *issue_ref*.

    A managed task — an issue with an ``epic``-labeled parent — is closed by its
    single PR, so it links with ``Closes`` to auto-close on merge to the default
    branch (epic vergil-project/.github#75). Legacy issues (no epic parent) keep
    *requested* (default ``Ref``) and stay open for manual close. Assumes the
    epic-target case was already rejected by :func:`_reject_if_epic_link`.

    Deciding task-ness needs the same authenticated cross-repo ``gh`` call as the
    epic rejection — a parent epic may live in ``.github``. Returns
    ``(linkage, note)`` where *note* explains the automatic upgrade to ``Closes``
    (for the caller to surface), or ``None``.
    """
    try:
        task = epics.parse_issue_ref(issue_ref, default_repo=github.current_repo())
    except ValueError:
        return requested, None
    parent = epics.parent_of(task)
    if parent is not None and epics.is_epic(parent):
        note = (
            f"{task.slug} is a task under epic {parent.slug}; "
            "linking with 'Closes' to auto-close it on merge."
        )
        return "Closes", note
    return requested, None


def _resolve_linkage(issue_ref: str, requested: str) -> str:
    """Effective PR-body linkage for *issue_ref*, announcing a ``Closes`` upgrade.

    Thin wrapper over :func:`_task_linkage` used at every PR-body build site: it
    prints the upgrade note (when a managed task is auto-linked with ``Closes``)
    and returns the linkage keyword.
    """
    linkage, note = _task_linkage(issue_ref, requested)
    if note:
        print(f"vrg-submit-pr: {note}")
    return linkage


def _run_cli_mode(args: argparse.Namespace) -> int:
    # main() only routes here when all three are present; narrow for the
    # type checker without relying on an assert (ruff S101).
    if args.issue is None or args.summary is None or args.title is None:  # pragma: no cover
        msg = "internal error: CLI mode requires --issue, --summary, and --title"
        raise SystemExit(msg)

    issue_ref = resolve_issue_ref(args.issue)
    _reject_if_epic_link(issue_ref)
    _reject_if_operational_task(issue_ref)
    linkage = _resolve_linkage(issue_ref, args.linkage)
    branch = git.current_branch()
    target = _target_branch(args.base)
    pr_body = build_pr_body(
        summary=args.summary,
        linkage=linkage,
        issue_ref=issue_ref,
        notes=args.notes,
    )

    if args.dry_run:
        print(f"=== PR Title ===\n{args.title}\n")
        print(f"=== Target Branch ===\n{target}\n")
        print(f"=== PR Body ===\n{pr_body}")
        if args.finalize:
            print(_dry_run_chain_note(release=args.release, install=args.install))
        return 0

    print(f"Pushing branch '{branch}' to origin...")
    _push_branch(branch)

    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=args.title, pr_body=pr_body)
    print(f"PR created: {pr_url}")
    if args.finalize:
        rc = _chain_finalize(pr_url, release=args.release, install=args.install)
        if rc != 0:
            return rc
        _print_cascade_summary(pr_url, released=args.release, installed=args.install)
        return 0
    print(f"Done. PR URL: {pr_url}")
    _print_pr_watch(pr_url)
    return 0


def _ready_worktrees(root: Path) -> list[tuple[worktrees.Worktree, dict[str, str]]]:
    """Return submittable ``(worktree, fields)`` pairs, or SystemExit if none.

    Same classification (ready / in-flight / not-ready) and same
    no-submittable-worktrees error as the single-select path; shared by the
    single picker and the batch selector (issue #1673).
    """
    ready: list[tuple[worktrees.Worktree, dict[str, str]]] = []
    in_flight: list[str] = []
    not_ready: list[str] = []
    for wt in worktrees.list_worktrees(root):
        try:
            fields = submission.read_pr_fields(wt.path)
        except AlreadySubmittedError as exc:
            ref = f"PR #{exc.pr_number}" if exc.pr_number is not None else "open PR"
            in_flight.append(f"{wt.path.name}: {ref} ({exc.pr_url})")
            continue
        except FileNotFoundError:
            not_ready.append(f"{wt.path.name}: no .vergil/pr-workflow.json")
            continue
        except WorkflowError as exc:
            not_ready.append(f"{wt.path.name}: {exc}")
            continue
        ready.append((wt, fields))

    if not ready:
        lines = ["vrg-submit-pr: no submittable worktrees found."]
        if in_flight:
            lines.append("")
            lines.append("  In flight (open PR — nothing to do):")
            lines.extend(f"    {entry}" for entry in in_flight)
        if not_ready:
            lines.append("")
            lines.append("  Not ready (no submission metadata yet):")
            lines.extend(f"    {entry}" for entry in not_ready)
        if not in_flight and not not_ready:
            lines.append("  (no .worktrees/ entries exist)")
        raise SystemExit("\n".join(lines))
    return ready


def _choose_submit_worktree(root: Path) -> Path:
    """At the repo root, pick the single template-ready worktree to submit from.

    One candidate is auto-picked (the existing y/N preview still confirms);
    several prompt a menu; none is an error that names each skipped worktree
    and why. Root launches are interactive by requirement, so the TTY guard
    fires up front, not per-prompt.
    """
    worktrees.require_tty("vrg-submit-pr from the repo root")
    ready = _ready_worktrees(root)

    if len(ready) == 1:
        wt, fields = ready[0]
        print(f"Using worktree {wt.path.name} (issue {fields['issue']}: {fields['title']})")
        return wt.path

    labels = [f"{wt.path.name} — issue {f['issue']}: {f['title']}" for wt, f in ready]
    chosen = worktrees.select_worktree(
        [wt for wt, _ in ready],
        purpose="Multiple submittable worktrees",
        labels=labels,
    )
    return chosen.path


def _select_batch_worktrees(root: Path, args: argparse.Namespace) -> list[worktrees.Worktree]:
    """Resolve the batch's worktrees from --all, --select, or a checkbox menu."""
    ready = _ready_worktrees(root)
    candidates = [wt for wt, _ in ready]
    fields_by_name = {wt.path.name: f for wt, f in ready}
    if args.all_worktrees:
        return candidates
    if args.select is not None:
        tokens = [t.strip() for t in args.select.split(",") if t.strip()]
        try:
            return worktrees.match_worktrees(candidates, tokens)
        except ValueError as exc:
            raise SystemExit(f"vrg-submit-pr --select: {exc}") from exc
    labels = [
        f"{wt.path.name} — issue {fields_by_name[wt.path.name]['issue']}: "
        f"{fields_by_name[wt.path.name]['title']}"
        for wt in candidates
    ]
    return worktrees.select_worktrees(
        candidates, purpose="Select worktrees to batch-submit", labels=labels
    )


def _run_submit_batch(
    selected: list[worktrees.Worktree],
    *,
    base: str,
    finalize: bool,
    release: bool,
    install: bool,
    assume_yes: bool,
) -> int:
    """Submit (and optionally finalize) *selected* worktrees as a serial batch.

    Per item: rebase the branch on the latest *base* (the zero-waste-CI step),
    chdir in, submit, chdir back, and — when *finalize* — shell out to
    ``vrg-finalize-pr <url> --skip-post-checks``. On full success, one
    end-of-batch validation and a single release run if requested (#1673).
    """
    main_root = git.main_worktree_root()

    def _process(wt: worktrees.Worktree) -> None:
        try:
            worktrees.rebase_onto(wt, base)
        except subprocess.CalledProcessError as exc:
            raise batch.BatchAbortError(f"rebase onto origin/{base} failed: {exc}") from exc
        os.chdir(wt.path)
        try:
            pr_url = _submit_one(wt.path, base_override=base, assume_yes=True)
        finally:
            os.chdir(main_root)
        if finalize:
            result = subprocess.run(  # noqa: S603
                ("vrg-finalize-pr", pr_url, "--skip-post-checks"),  # noqa: S607
                cwd=main_root,
                check=False,
            )
            if result.returncode != 0:
                raise batch.BatchAbortError(
                    f"vrg-finalize-pr {pr_url} --skip-post-checks exited {result.returncode}"
                )

    def _validate() -> None:
        result = subprocess.run(  # noqa: S603
            ("vrg-finalize-pr", "--cleanup-only"),  # noqa: S607
            cwd=main_root,
            check=False,
        )
        if result.returncode != 0:
            raise batch.BatchAbortError(f"end-of-batch validation exited {result.returncode}")

    def _release() -> None:
        cmd = ("vrg-release", "--install") if install else ("vrg-release",)
        result = subprocess.run(cmd, cwd=main_root, check=False)  # noqa: S603,S607
        if result.returncode != 0:
            raise batch.BatchAbortError(f"{' '.join(cmd)} exited {result.returncode}")

    post_steps: list[batch.PostStep] = []
    if finalize:
        post_steps.append(batch.PostStep("validation", _validate))
        if release:
            post_steps.append(batch.PostStep("release", _release))

    plan = [
        f"rebase + submit {wt.path.name}" + (" + finalize" if finalize else "") for wt in selected
    ]
    if finalize:
        plan.append("then: validate develop once" + (", then release" if release else ""))

    report = batch.run_batch(
        selected,
        _process,
        label=lambda wt: wt.path.name,
        plan=plan,
        assume_yes=assume_yes,
        post_steps=post_steps,
    )
    print(batch.format_report(report))
    return 0 if report.all_merged and report.post_failure is None else 1


def _push_create_record(
    *,
    worktree_root: Path,
    branch: str,
    target: str,
    title: str,
    pr_body: str,
) -> str:
    """Push the branch, create the PR, record submission, return the URL.

    The shared submit tail used by both single-PR template mode and the batch
    ``_submit_one``. Pushes with the human's host credentials (the superset of
    any agent's rights) so a branch touching ``.github/workflows/`` still
    pushes.
    """
    print(f"Ensuring branch '{branch}' is pushed to origin...")
    _push_branch(branch)
    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=title, pr_body=pr_body)
    submission.record_submission(worktree_root, pr_url=pr_url)
    print(f"PR created: {pr_url}")
    return pr_url


def _submit_one(worktree_root: Path, *, base_override: str | None, assume_yes: bool) -> str:
    """Read the worktree's PR fields, push, create the PR, record it, return URL.

    Self-contained submit for one worktree, used by the batch orchestrator.
    Propagates ``AlreadySubmittedError`` / ``FileNotFoundError`` /
    ``WorkflowError`` from the field reader and raises ``SystemExit`` on a
    forbidden linkage or a declined confirm. The per-PR
    confirm is pre-answered when *assume_yes* — the batch path passes True so
    the single up-front batch confirm is the only gate (issue #1673).
    """
    fields = submission.read_pr_fields(worktree_root)
    issue_ref = resolve_issue_ref(fields["issue"])
    _reject_if_epic_link(issue_ref)
    _reject_if_operational_task(issue_ref)
    branch = git.current_branch()
    target = _target_branch(base_override, fields.get("base"))
    try:
        linkage, linkage_warning = normalize_linkage(fields.get("linkage", "Ref"))
    except ValueError as exc:
        raise SystemExit(f"vrg-submit-pr: {exc}") from exc
    if linkage_warning:
        print(f"vrg-submit-pr: {linkage_warning}", file=sys.stderr)
    linkage = _resolve_linkage(issue_ref, linkage)
    pr_body = build_pr_body(
        summary=fields["summary"],
        linkage=linkage,
        issue_ref=issue_ref,
        notes=fields.get("notes", ""),
    )
    print(f"=== Submitting issue {issue_ref}: {fields['title']} ===")
    print(f"    base {target}, branch {branch}")
    if not confirm("\nSubmit this PR?", assume_yes=assume_yes):
        raise SystemExit("vrg-submit-pr: submission declined at the per-PR confirm")
    return _push_create_record(
        worktree_root=worktree_root,
        branch=branch,
        target=target,
        title=fields["title"],
        pr_body=pr_body,
    )


def _run_template_mode(args: argparse.Namespace) -> int:
    root = Path(git.repo_root())

    # Location resolution: from the main worktree (repo root), resolve
    # which `.worktrees/` worktree to submit from and move there. The
    # invoking shell is unaffected — chdir applies to this process only.
    if git.is_main_worktree():
        # Batch when --all/--select is given. A single selection falls through
        # to the unchanged single-PR path below (issue #1673). Interactive
        # no-flag multi-select is a deliberate follow-up: the no-flag path
        # stays single-select via _choose_submit_worktree.
        if args.all_worktrees or args.select is not None:
            selected = _select_batch_worktrees(root, args)
            base = _target_branch(args.base) if args.base else "develop"
            return _run_submit_batch(
                selected,
                base=base,
                finalize=args.finalize,
                release=args.release,
                install=args.install,
                assume_yes=args.yes,
            )
        wt_path = _choose_submit_worktree(root)
        os.chdir(wt_path)
        root = wt_path

    try:
        fields = submission.read_pr_fields(root)
    except AlreadySubmittedError as exc:
        print(
            f"vrg-submit-pr: this worktree's PR is already submitted — {exc}.\n"
            "  Nothing to do; the PR is in flight. Use vrg-finalize-pr to merge it.",
        )
        return 0
    except FileNotFoundError:
        print(
            "vrg-submit-pr: No .vergil/pr-workflow.json found,\n"
            "  and no CLI arguments provided. Either provide --issue, --summary, and\n"
            "  --title, or ensure the agent has run the workflow through to approval.",
            file=sys.stderr,
        )
        return 1
    except WorkflowError as exc:
        print(f"vrg-submit-pr: cannot read PR submission fields:\n  {exc}", file=sys.stderr)
        return 1

    issue_ref = resolve_issue_ref(fields["issue"])
    branch = git.current_branch()
    target = _target_branch(args.base, fields.get("base"))
    title = fields["title"]
    notes = fields.get("notes", "")

    # Belt-and-suspenders: the oracle normalizes linkage at report-ready, but
    # guard the value used to build the PR body so a forbidden keyword can never
    # reach the PR regardless of how the fields were obtained. A keyword carrying
    # a stray issue number is unambiguous, so strip it and warn rather than fail.
    try:
        linkage, linkage_warning = normalize_linkage(fields.get("linkage", "Ref"))
    except ValueError as exc:
        print(f"vrg-submit-pr: {exc}", file=sys.stderr)
        return 1
    if linkage_warning:
        print(f"vrg-submit-pr: {linkage_warning}", file=sys.stderr)
    linkage = _resolve_linkage(issue_ref, linkage)
    pr_body = build_pr_body(
        summary=fields["summary"],
        linkage=linkage,
        issue_ref=issue_ref,
        notes=notes,
    )

    print("=== PR from template ===")
    print(f"Title:  {title}")
    print(f"Base:   {target}")
    print(f"Branch: {branch}")
    print(f"Issue:  {issue_ref}")
    print()
    print(f"=== Body Preview ===\n{pr_body}")

    if args.dry_run:
        if args.finalize:
            print(_dry_run_chain_note(release=args.release, install=args.install))
        return 0

    if not confirm("\nSubmit this PR?", assume_yes=args.yes):
        print("Aborted.")
        return 1

    pr_url = _push_create_record(
        worktree_root=root,
        branch=branch,
        target=target,
        title=title,
        pr_body=pr_body,
    )
    if args.finalize:
        rc = _chain_finalize(pr_url, release=args.release, install=args.install)
        if rc != 0:
            return rc
        _print_cascade_summary(pr_url, released=args.release, installed=args.install)
        return 0
    print(f"Done. PR URL: {pr_url}")
    _print_pr_watch(pr_url)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # --install extends the cascade one link past --release, which itself
    # implies --finalize; normalize once (install ⇒ release ⇒ finalize) so
    # every downstream branch (dry-run notes, chain, summary) sees the right
    # flags (issues #1634, #1643).
    if args.install:
        args.release = True
    if args.release:
        args.finalize = True

    if identity_mode.is_agent():
        print(
            "vrg-submit-pr: PR submission requires a human maintainer. Agents cannot submit PRs.",
            file=sys.stderr,
        )
        return 1

    cli_fields = [args.issue, args.summary, args.title]
    has_any = any(f is not None for f in cli_fields)
    has_all = all(f is not None for f in cli_fields)

    if has_any and not has_all:
        missing = []
        if args.issue is None:
            missing.append("--issue")
        if args.summary is None:
            missing.append("--summary")
        if args.title is None:
            missing.append("--title")
        print(
            f"vrg-submit-pr: The following required arguments are missing: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 1

    if has_all:
        return _run_cli_mode(args)
    return _run_template_mode(args)


if __name__ == "__main__":
    sys.exit(main())
