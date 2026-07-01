"""Create an unlinked triage issue.

Triage issues are standalone (no parent epic) and labelled ``triage``; they are
routed to an epic later during triage-review. This is the sanctioned path for
creating them — ``vrg-gh`` denies raw ``gh issue create`` — used by the
``triage-capture`` skill.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import github


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-triage-create",
        description=(
            "Create an unlinked triage issue (labelled 'triage', no parent "
            "epic). Defaults to the current repo; use --repo for another repo "
            "(e.g. the org .github for a project-level seed)."
        ),
        epilog=("The issue is intentionally unlinked; triage-review routes it to an epic later."),
    )
    parser.add_argument("--title", required=True, help="Issue title")
    parser.add_argument("--body", default="", help="Issue body text")
    parser.add_argument("--body-file", help="Read the issue body from a file")
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Extra label (repeatable); 'triage' is always added",
    )
    parser.add_argument("--assignee", action="append", default=[], help="Assignee (repeatable)")
    parser.add_argument("--repo", help="Target repo owner/name (defaults to the current repo)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = args.repo or github.current_repo()
    labels = list(dict.fromkeys(["triage", *args.label]))
    url = github.create_issue(
        repo=repo,
        title=args.title,
        body=args.body,
        body_file=args.body_file,
        labels=labels,
        assignees=args.assignee,
    )
    print(f"Created {url} (triage).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
