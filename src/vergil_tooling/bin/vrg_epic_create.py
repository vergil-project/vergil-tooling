"""Create a top-level epic issue in its resolved home repo.

Epics are top-level (no parent), labelled ``epic``. The *home* — the repo where
the epic issue physically lives — is derived from the target repo's visibility
by :func:`vergil_tooling.lib.epics.resolve_epic_home`: a public target homes
centrally in ``<org>/.github``; a private target (with a public ``.github``)
homes its epics in itself. The target defaults to the current repo but is named
explicitly with ``--repo``. This is the sanctioned path for creating epics —
``vrg-gh`` denies raw ``gh issue create`` — used by the ``epic-create`` and
``migrate-repo`` skills.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-epic-create",
        description=(
            "Create a top-level epic labelled 'epic' with no parent. The epic "
            "home is derived from the target repo's visibility: a public target "
            "homes in <org>/.github, a private target homes in itself."
        ),
        epilog=(
            "Extra --label values are added alongside 'epic' (e.g. --label "
            "ad-hoc for an ad-hoc epic)."
        ),
    )
    parser.add_argument("--title", required=True, help="Epic title")
    parser.add_argument("--body", default="", help="Epic body text")
    parser.add_argument("--body-file", help="Read the epic body from a file")
    parser.add_argument(
        "--repo",
        help=(
            "Target repo 'owner/repo' the epic is for (default: current repo). "
            "The epic home is derived from the target's visibility."
        ),
    )
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
    target = args.repo or github.current_repo()  # "owner/repo"; raises if undeterminable
    if "/" not in target:
        print(
            f"vrg-epic-create: --repo must be 'owner/repo' (got {target!r})",
            file=sys.stderr,
        )
        return 1
    owner, bare = target.split("/", 1)
    home = epics.resolve_epic_home(owner, bare)
    print(f"-> epic home: {home} [{github.repo_visibility(home)}]")
    labels = list(dict.fromkeys(["epic", *args.label]))
    url = github.create_issue(
        repo=home,
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
