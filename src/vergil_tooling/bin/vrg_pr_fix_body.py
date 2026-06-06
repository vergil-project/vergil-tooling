"""Repair a PR body through the validated body builder (#1459).

During the pr-watch reconcile loop a standards failure on the PR
*body* (e.g. a forbidden auto-close linkage) is a failing CI check
that no code patch can fix, and ``vrg-gh pr edit`` is denied for
agent identities. This tool is the narrow, validated repair path:
the body is regenerated from corrected fields through the same
builder and linkage validation as ``vrg-submit-pr``, so the body can
only be replaced by a compliant body — free-form PR edits stay
closed to agents.

Identity rules:

- **audit** — denied. The audit identity never mutates PRs.
- **user** — allowed only for the agent's own PR: the PR's head
  branch must match the session's current branch.
- **human** — allowed, no scope check (the human is the superset).

A body edit alone does not re-run the standards gate —
``pull_request`` workflows trigger on ``opened/synchronize/reopened``,
not ``edited``, and a manual re-run replays the stale event payload.
The tool therefore pushes an empty commit after the edit to
re-trigger CI (``--no-retrigger`` skips this).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, github, identity_mode, pr_body
from vergil_tooling.lib.linkage import ALLOWED_LINKAGES

_RETRIGGER_MESSAGE = (
    "chore(ci): empty commit to re-run checks after PR body fix\n\n"
    "PR body edits do not re-trigger pull_request workflows; this\n"
    "commit re-runs the gates against the corrected body."
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Regenerate a PR body from corrected fields via the validated builder."
    )
    parser.add_argument("pr", help="PR number or URL")
    parser.add_argument(
        "--issue", required=True, help="Issue reference: number or owner/repo#number"
    )
    parser.add_argument("--summary", required=True, help="One-line PR summary")
    parser.add_argument(
        "--linkage", default="Ref", choices=ALLOWED_LINKAGES, help="Issue linkage keyword"
    )
    parser.add_argument("--notes", default="", help="Additional notes")
    parser.add_argument("--dry-run", action="store_true", help="Print the body without editing")
    parser.add_argument(
        "--no-retrigger",
        action="store_true",
        help="Skip the empty-commit push that re-triggers CI after the edit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    mode = identity_mode.current_mode()
    if mode == identity_mode.IdentityMode.AUDIT:
        print(
            "vrg-pr-fix-body: denied for the audit identity. "
            "The audit never mutates PRs — escalate findings via review comments.",
            file=sys.stderr,
        )
        return 1

    issue_ref = pr_body.resolve_issue_ref(args.issue)
    body = pr_body.build_pr_body(
        summary=args.summary,
        linkage=args.linkage,
        issue_ref=issue_ref,
        notes=args.notes,
    )

    if args.dry_run:
        print(f"=== PR Body ===\n{body}")
        return 0

    if identity_mode.is_agent():
        head = github.head_ref(args.pr)
        branch = git.current_branch()
        if head != branch:
            print(
                f"vrg-pr-fix-body: agents may only fix their own PR's body: "
                f"PR head branch '{head}' does not match the current branch '{branch}'.",
                file=sys.stderr,
            )
            return 1

    state = github.pr_state(args.pr)
    if state != "OPEN":
        print(
            f"vrg-pr-fix-body: PR {args.pr} is {state}; only open PRs can be fixed.",
            file=sys.stderr,
        )
        return 1

    print(f"Replacing PR body for {args.pr}...")
    github.edit_pr_body(args.pr, body=body)
    print("PR body updated.")

    if args.no_retrigger:
        print(
            "NOTE: --no-retrigger set. A body edit alone does not re-run the\n"
            "standards gate — push a commit to re-trigger CI."
        )
        return 0

    print("Pushing an empty commit to re-trigger CI...")
    git.run("commit", "--allow-empty", "-m", _RETRIGGER_MESSAGE)
    git.run("push")
    print("Done. CI will re-run against the corrected body.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
