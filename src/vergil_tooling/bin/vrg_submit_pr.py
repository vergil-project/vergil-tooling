"""PR submission wrapper that constructs standards-compliant PR bodies.

Supports two modes:
- **Template mode** (no CLI args): reads ``.vergil/pr-template.yml``,
  shows a summary, prompts for confirmation, pushes the branch, and
  creates the PR.
- **CLI argument mode** (args provided): existing direct invocation
  for human emergency use.

Both modes ensure the branch is pushed using the human's host
credentials before creating the PR. Because the human is the superset
of any agent's rights, this carries workflow-touching pushes that the
agent's own credentials would be rejected for.

Agent identities are blocked — PR submission is a Chief Steward
(human) operation.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib import git, github, identity_mode, pr_template, worktrees

ALLOWED_LINKAGES = ("Ref",)
_ISSUE_PLAIN_RE = re.compile(r"^[1-9]\d*$")
_ISSUE_CROSS_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+#[1-9]\d*$")


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
    return parser.parse_args(argv)


def _resolve_issue_ref(issue: str) -> str:
    """Validate and normalize the issue reference."""
    if _ISSUE_PLAIN_RE.match(issue):
        return f"#{issue}"
    if _ISSUE_CROSS_RE.match(issue):
        return issue
    msg = f"--issue must be a number (42) or cross-repo ref (owner/repo#42), got '{issue}'."
    raise SystemExit(msg)


def _build_pr_body(*, summary: str, linkage: str, issue_ref: str, notes: str) -> str:
    notes_section = notes or "-"
    return (
        f"# Pull Request\n\n"
        f"## Summary\n\n- {summary}\n\n"
        f"## Issue Linkage\n\n- {linkage} {issue_ref}\n\n"
        f"## Notes\n\n- {notes_section}"
    )


def _target_branch(branch: str, base_override: str | None) -> str:
    if base_override:
        return base_override
    return "main" if branch.startswith("release/") else "develop"


def _create_pr(*, target_branch: str, title: str, pr_body: str) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(pr_body)
        tmp_path = f.name
    try:
        pr_url = github.create_pr(base=target_branch, title=title, body_file=tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return pr_url


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

    issue_ref = _resolve_issue_ref(args.issue)
    branch = git.current_branch()
    target = _target_branch(branch, args.base)
    pr_body = _build_pr_body(
        summary=args.summary,
        linkage=args.linkage,
        issue_ref=issue_ref,
        notes=args.notes,
    )

    if args.dry_run:
        print(f"=== PR Title ===\n{args.title}\n")
        print(f"=== Target Branch ===\n{target}\n")
        print(f"=== PR Body ===\n{pr_body}")
        return 0

    print(f"Pushing branch '{branch}' to origin...")
    git.run("push", "-u", "origin", branch)

    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=args.title, pr_body=pr_body)
    print(f"PR created: {pr_url}")
    print(f"Done. PR URL: {pr_url}")
    _print_pr_watch(pr_url)
    return 0


def _choose_submit_worktree(root: Path) -> Path:
    """At the repo root, pick the template-ready worktree to submit from.

    Candidates are worktrees containing a valid ``.vergil/pr-template.yml``
    — the agent-written signal that the issue is ready for submission.
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
            fields = pr_template.read_template(wt.path)
        except FileNotFoundError:
            skipped.append(f"{wt.path.name}: no .vergil/pr-template.yml — not ready")
            continue
        except pr_template.TemplateError as exc:
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
        fields = pr_template.read_template(root)
    except FileNotFoundError:
        print(
            "vrg-submit-pr: No .vergil/pr-template.yml found and no CLI arguments provided.\n"
            "  Either provide --issue, --summary, and --title, or ensure the agent\n"
            "  has written a PR template file.",
            file=sys.stderr,
        )
        return 1

    issue_ref = _resolve_issue_ref(fields["issue"])
    branch = git.current_branch()
    target = _target_branch(branch, args.base)
    title = fields["title"]
    linkage = fields.get("linkage", "Ref")
    notes = fields.get("notes", "")
    pr_body = _build_pr_body(
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
    git.run("push", "-u", "origin", branch)

    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=title, pr_body=pr_body)
    pr_template.delete_template(root)
    print(f"PR created: {pr_url}")
    print(f"Done. PR URL: {pr_url}")
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
