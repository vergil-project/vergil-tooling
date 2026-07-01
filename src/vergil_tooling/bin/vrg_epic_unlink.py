"""Unlink a task issue from its epic (remove the native sub-issue link).

The counterpart to ``vrg-epic-link``. A task with no current parent is a
friendly no-op, not an error.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Unlink a task from its epic.")
    parser.add_argument("--task", required=True, help="Task ref: owner/repo#N or #N")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    default_repo = github.current_repo()
    try:
        task = epics.parse_issue_ref(args.task, default_repo=default_repo)
    except ValueError as exc:
        print(f"vrg-epic-unlink: {exc}", file=sys.stderr)
        return 1

    current = epics.parent_of(task)
    if current is None:
        print(f"{task.slug} is not linked to any epic; nothing to do.")
        return 0
    epics.remove_child(current, task)
    print(f"Unlinked {task.slug} from epic {current.slug}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
