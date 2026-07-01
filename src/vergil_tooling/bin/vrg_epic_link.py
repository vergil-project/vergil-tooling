"""Link a task issue under an epic as a native GitHub sub-issue.

The rollup (``vrg-finalize-pr``) decides whether to close an epic by querying its
native sub-issues; a ``Parent:`` reflink alone is only a flaky ``gh search``
fallback. This CLI creates the reliable native link via ``epics.add_child``
(which reopens a closed epic first, per the reopen-on-late-child rule). Use it
when creating a task under an epic, and to backfill reflink-only children.

Refs are ``owner/repo#N`` or bare ``#N`` (bare resolves to the current repo).
The epic typically lives in the org ``.github`` repo, so pass it fully-qualified.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Link a task under an epic as a native GitHub sub-issue."
    )
    parser.add_argument("--epic", required=True, help="Epic ref: owner/repo#N or #N")
    parser.add_argument("--task", required=True, help="Task ref: owner/repo#N or #N")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    default_repo = github.current_repo()
    try:
        epic = epics.parse_issue_ref(args.epic, default_repo=default_repo)
        task = epics.parse_issue_ref(args.task, default_repo=default_repo)
        owner = epics.single_target_org(epic, task)
    except ValueError as exc:
        print(f"vrg-epic-link: {exc}", file=sys.stderr)
        return 1
    try:
        with github.target_org(owner):
            epics.add_child(epic, task)
    except github.NoInstallationError as exc:
        print(f"vrg-epic-link: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    print(f"Linked {task.slug} under epic {epic.slug}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
