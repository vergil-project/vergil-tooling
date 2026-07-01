"""Create a top-level epic issue in the org's ``.github`` repo.

Epics are top-level (no parent) and live in ``<org>/.github`` with the ``epic``
label. This is the sanctioned path for creating them — ``vrg-gh`` denies raw
``gh issue create`` — used by the ``epic-create`` and ``migrate-repo`` skills.
The org is auto-detected from the current repo's remote.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import github


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-epic-create",
        description=(
            "Create a top-level epic in the current repo's GitHub org: an issue "
            "in <org>/.github labelled 'epic', with no parent. The org is "
            "auto-detected from this repo's origin remote."
        ),
        epilog=(
            "Run from inside a repo in the target org. Extra --label values are "
            "added alongside 'epic' (e.g. --label standing for a standing epic)."
        ),
    )
    parser.add_argument("--title", required=True, help="Epic title")
    parser.add_argument("--body", default="", help="Epic body text")
    parser.add_argument("--body-file", help="Read the epic body from a file")
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Extra label (repeatable); 'epic' is always added",
    )
    parser.add_argument("--assignee", action="append", default=[], help="Assignee (repeatable)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    org = github.detect_org()
    if org is None:
        print(
            "vrg-epic-create: could not determine the GitHub org from this "
            "repo's 'origin' remote; run it from inside a repo in the org whose "
            ".github should hold the epic.",
            file=sys.stderr,
        )
        return 1
    repo = f"{org}/.github"
    labels = list(dict.fromkeys(["epic", *args.label]))
    url = github.create_issue(
        repo=repo,
        title=args.title,
        body=args.body,
        body_file=args.body_file,
        labels=labels,
        assignees=args.assignee,
    )
    print(f"Created {url} (epic).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
