"""PR submission wrapper that constructs standards-compliant PR bodies.

Supports two modes:
- **Template mode** (no CLI args): reads the PR workflow state file
  (``.vergil/pr-workflow.json``), falling back to the legacy
  ``.vergil/pr-template.yml``; shows a summary, prompts for
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
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib import git, github, identity_mode, pr_template, worktrees
from vergil_tooling.lib.linkage import ALLOWED_LINKAGES
from vergil_tooling.lib.pr_body import build_pr_body, resolve_issue_ref
from vergil_tooling.lib.pr_workflow import submission
from vergil_tooling.lib.pr_workflow.errors import WorkflowError


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


def _chain_finalize(pr_url: str) -> int:
    """Hand off to ``vrg-finalize-pr`` right after PR creation (issue #1491).

    Equivalent to running ``vrg-finalize-pr <pr-url>`` by hand: same
    merge-strategy default, same post-merge cleanup. Runs as a subprocess
    from the main worktree root because vrg-finalize-pr refuses to run
    from a secondary worktree (it removes worktrees during cleanup) and
    template mode chdir'd into one. The child inherits the TTY so its
    live progress display and prompts behave exactly as a manual run.

    A failure here never un-creates the PR — report the PR clearly so
    the human can re-run vrg-finalize-pr alone.
    """
    main_root = git.main_worktree_root()
    print()
    print(f"--finalize: handing off to vrg-finalize-pr {pr_url}")
    result = subprocess.run(  # noqa: S603
        ("vrg-finalize-pr", pr_url),  # noqa: S607
        cwd=main_root,
        check=False,
    )
    if result.returncode != 0:
        print(
            f"vrg-submit-pr: finalize failed (exit {result.returncode}); "
            "the PR was created and is unaffected:\n"
            f"  {pr_url}\n"
            f"  Re-run finalization alone with: vrg-finalize-pr {pr_url}",
            file=sys.stderr,
        )
        return result.returncode
    return 0


def _print_pr_watch(pr_url: str) -> None:
    """Emit the paste-ready post-PR one-liner (§9 of the 2.1 workflow design).

    Opening the PR auto-triggers the mechanized CI gates; the post-PR loop is
    started by pasting this same line into *both* agent sessions. The skill
    reads its own identity and runs the matching half of the loop.
    """
    print()
    print("Next — paste this into BOTH agent sessions (USER and AUDIT):")
    print()
    print(f"    /vergil:pr-watch {pr_url}")


def _run_cli_mode(args: argparse.Namespace) -> int:
    # main() only routes here when all three are present; narrow for the
    # type checker without relying on an assert (ruff S101).
    if args.issue is None or args.summary is None or args.title is None:  # pragma: no cover
        msg = "internal error: CLI mode requires --issue, --summary, and --title"
        raise SystemExit(msg)

    issue_ref = resolve_issue_ref(args.issue)
    branch = git.current_branch()
    target = _target_branch(args.base)
    pr_body = build_pr_body(
        summary=args.summary,
        linkage=args.linkage,
        issue_ref=issue_ref,
        notes=args.notes,
    )

    if args.dry_run:
        print(f"=== PR Title ===\n{args.title}\n")
        print(f"=== Target Branch ===\n{target}\n")
        print(f"=== PR Body ===\n{pr_body}")
        if args.finalize:
            print("\n[dry-run] would chain into: vrg-finalize-pr <pr-url>")
        return 0

    print(f"Pushing branch '{branch}' to origin...")
    _push_branch(branch)

    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=args.title, pr_body=pr_body)
    print(f"PR created: {pr_url}")
    print(f"Done. PR URL: {pr_url}")
    if args.finalize:
        return _chain_finalize(pr_url)
    _print_pr_watch(pr_url)
    return 0


def _choose_submit_worktree(root: Path) -> Path:
    """At the repo root, pick the template-ready worktree to submit from.

    Candidates are worktrees with submittable PR fields — a valid
    ``.vergil/pr-workflow.json`` (with PR metadata) or the legacy
    ``.vergil/pr-template.yml`` — the agent-written signal that the issue
    is ready for submission.
    One candidate is auto-picked (the existing y/N preview still
    confirms); several prompt a menu; none is an error that names each
    skipped worktree and why.

    Root launches are interactive by requirement regardless of how many
    candidates exist — even the auto-picked path ends in a y/N confirm —
    so the TTY guard fires up front, not per-prompt.
    """
    worktrees.require_tty("vrg-submit-pr from the repo root")

    ready: list[tuple[worktrees.Worktree, dict[str, str]]] = []
    skipped: list[str] = []
    for wt in worktrees.list_worktrees(root):
        try:
            fields = submission.read_pr_fields(wt.path)
        except FileNotFoundError:
            skipped.append(
                f"{wt.path.name}: no .vergil/pr-workflow.json or pr-template.yml — not ready"
            )
            continue
        except (pr_template.TemplateError, WorkflowError) as exc:
            skipped.append(f"{wt.path.name}: {exc}")
            continue
        ready.append((wt, fields))

    if not ready:
        lines = ["vrg-submit-pr: no submittable worktrees found."]
        if skipped:
            lines.extend(f"  {reason}" for reason in skipped)
        else:
            lines.append("  (no .worktrees/ entries exist)")
        raise SystemExit("\n".join(lines))

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


def _run_template_mode(args: argparse.Namespace) -> int:
    root = Path(git.repo_root())

    # Location resolution: from the main worktree (repo root), resolve
    # which `.worktrees/` worktree to submit from and move there. The
    # invoking shell is unaffected — chdir applies to this process only.
    if git.is_main_worktree():
        wt_path = _choose_submit_worktree(root)
        os.chdir(wt_path)
        root = wt_path

    try:
        fields = submission.read_pr_fields(root)
    except FileNotFoundError:
        print(
            "vrg-submit-pr: No .vergil/pr-workflow.json or .vergil/pr-template.yml found,\n"
            "  and no CLI arguments provided. Either provide --issue, --summary, and\n"
            "  --title, or ensure the agent has run the workflow through to approval.",
            file=sys.stderr,
        )
        return 1
    except (pr_template.TemplateError, WorkflowError) as exc:
        print(f"vrg-submit-pr: cannot read PR submission fields:\n  {exc}", file=sys.stderr)
        return 1

    issue_ref = resolve_issue_ref(fields["issue"])
    branch = git.current_branch()
    target = _target_branch(args.base, fields.get("base"))
    title = fields["title"]
    linkage = fields.get("linkage", "Ref")
    notes = fields.get("notes", "")

    # Belt-and-suspenders: read_template validates linkage, but guard the
    # value used to build the PR body so a forbidden auto-close keyword can
    # never reach the PR regardless of how the fields were obtained.
    if linkage not in ALLOWED_LINKAGES:
        print(
            f"vrg-submit-pr: linkage '{linkage}' in the PR submission fields is not "
            f"allowed; use: {', '.join(ALLOWED_LINKAGES)}.",
            file=sys.stderr,
        )
        return 1
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
            print("\n[dry-run] would chain into: vrg-finalize-pr <pr-url>")
        return 0

    try:
        answer = input("\nSubmit this PR? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        return 1

    if answer != "y":
        print("Aborted.")
        return 1

    # Ensure-pushed: push with the human's host credentials before creating
    # the PR. The human is the superset of any agent's rights, so this push
    # succeeds even for branches that touch .github/workflows/ — which the
    # agent's own push would have been rejected for.
    print(f"Ensuring branch '{branch}' is pushed to origin...")
    _push_branch(branch)

    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=title, pr_body=pr_body)
    submission.delete_submission(root)
    print(f"PR created: {pr_url}")
    print(f"Done. PR URL: {pr_url}")
    if args.finalize:
        return _chain_finalize(pr_url)
    _print_pr_watch(pr_url)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

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
