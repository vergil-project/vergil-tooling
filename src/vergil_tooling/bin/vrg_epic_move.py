"""Move a task issue to a different epic (idempotent re-parent).

GitHub sub-issues allow only one parent, so re-parenting is unlink-then-link.
The target epic is resolved via :func:`epics.resolve_epic_ref` (accepting the
``adhoc`` sentinel and validating epic-ness); moving a task already under the
target epic is a no-op.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.cli import cli_main


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    default_repo = github.current_repo()
    try:
        task = epics.parse_issue_ref(args.task, default_repo=default_repo)
        # Scope the App token to the task's owner (#2070). The epic must live in
        # the same org — the 'adhoc' sentinel resolves to the task repo's own
        # ad-hoc epic (homed in the task's org), so only an explicit ref can
        # diverge; guard that before any network call so a cross-org mistake
        # is a clear message, not a cwd-scoped 403.
        if args.epic != "adhoc":
            epic_owner = epics.parse_issue_ref(args.epic, default_repo=default_repo).owner
            if epic_owner != task.owner:
                raise ValueError(
                    "cross-org operation is out of scope: task owner "
                    f"{task.owner!r} != epic owner {epic_owner!r}"
                )
    except ValueError as exc:
        print(f"vrg-epic-move: {exc}", file=sys.stderr)
        return 1

    try:
        with github.target_org(task.owner):
            # The 'adhoc' sentinel names the *task's* repo's ad-hoc epic, never
            # the cwd repo's (#3409); an explicit '#N' stays cwd-relative.
            epic_repo = f"{task.owner}/{task.repo}" if args.epic == "adhoc" else default_repo
            try:
                epic = epics.resolve_epic_ref(args.epic, repo=epic_repo)
            except ValueError as exc:
                print(f"vrg-epic-move: {exc}", file=sys.stderr)
                return 1
            current = epics.parent_of(task)
            if current == epic:
                print(f"{task.slug} is already under epic {epic.slug}; nothing to do.")
                return 0
            if current is not None:
                epics.remove_child(current, task)
            epics.add_child(epic, task)
    except github.NoInstallationError as exc:
        print(f"vrg-epic-move: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    print(f"Moved {task.slug} under epic {epic.slug}.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Move a task under a different epic.")
    parser.add_argument("--task", required=True, help="Task ref: owner/repo#N or #N")
    parser.add_argument("--epic", required=True, help="Target epic: 'adhoc', #N, or owner/repo#N")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
