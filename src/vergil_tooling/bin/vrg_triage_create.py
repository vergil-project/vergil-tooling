"""Create an unlinked intake issue (triage / idea / research).

Intake issues are standalone (no parent epic) and carry one of three ``--kind``
labels — ``triage`` (a problem not yet understood), ``idea`` (a spark), or
``research`` (a reproducible investigation). They are folded into the epic/task
model later during triage-review. This is the sanctioned path for creating them
— ``vrg-gh`` denies raw ``gh issue create`` — used by the ``triage-capture``
skill.

Intake is routed to the org's ``.github`` by default so the whole org-wide
intake queue lives in one place (epic vergil-project/.github#85); this
supersedes the earlier current-repo default (#2075).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import github

_KINDS = ("triage", "idea", "research")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-triage-create",
        description=(
            "Create an unlinked intake issue (triage/idea/research, no parent "
            "epic). Routed to the org's .github by default; use --repo to "
            "override (e.g. a task whose PR lands in .github itself)."
        ),
        epilog=("The issue is intentionally unlinked; triage-review folds it into an epic later."),
    )
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument(
        "--kind",
        choices=_KINDS,
        default="triage",
        help="Intake kind; sets the primary label (default: triage)",
    )
    parser.add_argument("--body", default="", help="Issue body text")
    parser.add_argument("--body-file", help="Read the issue body from a file")
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Extra label (repeatable); the --kind label is always added",
    )
    parser.add_argument("--assignee", action="append", default=[], help="Assignee (repeatable)")
    parser.add_argument("--repo", help="Target repo owner/name (defaults to the org's .github)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.repo:
        repo = args.repo
    else:
        org = github.detect_org()
        if org is None:
            print(
                "vrg-triage-create: could not determine the GitHub org from this "
                "repo's 'origin' remote; run it from inside a repo in the target "
                "org, or pass --repo.",
                file=sys.stderr,
            )
            return 1
        repo = f"{org}/.github"
    labels = list(dict.fromkeys([args.kind, *args.label]))
    url = github.create_issue(
        repo=repo,
        title=args.title,
        body=args.body,
        body_file=args.body_file,
        labels=labels,
        assignees=args.assignee,
    )
    print(f"Created {url} ({args.kind}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
