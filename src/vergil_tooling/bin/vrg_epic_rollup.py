"""Roll up a task's parent epic when the task closes.

Thin CLI over ``epics.rollup`` for the ``on: issues.closed`` Action (issue
#2042, epic vergil-project/.github#75): given the just-closed issue as
``--task``, close its parent epic when the epic is finite and all its child
tasks are now closed.

``epics.rollup`` is a no-op unless the closed issue is a managed task with an
``epic``-labeled, non-``standing`` parent, so this is safe to fire on *every*
issue close. Moving rollup here makes it event-driven — it no longer depends on
``vrg-finalize-pr`` running.

Refs are ``owner/repo#N`` or bare ``#N`` (bare resolves to the current repo).
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Roll up a task's parent epic when the task closes."
    )
    parser.add_argument("--task", required=True, help="Closed task ref: owner/repo#N or #N")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    default_repo = github.current_repo()
    try:
        task = epics.parse_issue_ref(args.task, default_repo=default_repo)
    except ValueError as exc:
        print(f"vrg-epic-rollup: {exc}", file=sys.stderr)
        return 1
    epics.rollup(task)
    print(f"Epic rollup check complete for {task.slug}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
