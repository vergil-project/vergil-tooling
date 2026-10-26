"""Move a task issue to a different epic (idempotent re-parent).

GitHub sub-issues allow only one parent, so re-parenting is unlink-then-link.
The target epic is resolved via :func:`epics.resolve_epic_ref` (accepting the
``adhoc``, ``intake`` and ``planning`` sentinels and validating epic-ness);
moving a task already under the target epic is a no-op.

The sentinels and a bare ``#N`` epic ref are read against the **task's** repo,
never the cwd repo: ``adhoc`` is the task repo's ad-hoc epic, ``intake`` and
``planning`` its intake and planning queue epics, and ``#N``
is issue N in the task repo's epic home (``<org>/.github`` for a public repo,
the repo itself for a self-homed private one). A fully qualified
``owner/repo#N`` is taken as written (#3409, #3411).

The deferred queue is entered and left only through ``vrg-intake-route``, which
records the deferral: this refuses the ``deferred`` sentinel, a target epic that
is a deferred queue epic, and a task whose current parent is one (#3673). Every
refusal runs before any write.
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
    if args.epic == epics.DEFERRED.sentinel:
        # Refused before resolving: resolving the sentinel would create the epic.
        print(
            f"vrg-epic-move: refusing --epic {args.epic}: {epics.DEFERRED_ROUTE_ONLY}",
            file=sys.stderr,
        )
        return 1
    try:
        task = epics.parse_issue_ref(args.task, default_repo=default_repo)
        task_repo = f"{task.owner}/{task.repo}"
        # Scope the App token to the task's owner (#2070). The epic must live in
        # the same org — the standing sentinels (adhoc/intake/planning) and a
        # bare '#N' all resolve within the task repo's epic or queue home (in
        # the task's org), so only a qualified ref can diverge; guard that
        # before any network call so a cross-org mistake is a clear message,
        # not a cwd-scoped 403 (#3514).
        if args.epic not in epics.EPIC_SENTINELS:
            epic_owner = epics.parse_issue_ref(args.epic, default_repo=task_repo).owner
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
            # 'adhoc' and a bare '#N' resolve against the *task's* repo (its
            # ad-hoc epic / its epic home), never the cwd repo (#3409, #3411).
            # The epic-home visibility probes run under the task owner's token.
            try:
                epic = epics.resolve_epic_ref(args.epic, repo=task_repo)
            except ValueError as exc:
                print(f"vrg-epic-move: {exc}", file=sys.stderr)
                return 1
            if epics.queue_kind(epic) == epics.DEFERRED.key:
                print(
                    f"vrg-epic-move: refusing to move {task.slug} into deferred epic "
                    f"{epic.slug}: {epics.DEFERRED_ROUTE_ONLY}",
                    file=sys.stderr,
                )
                return 1
            current = epics.parent_of(task)
            if current is not None and epics.queue_kind(current) == epics.DEFERRED.key:
                print(
                    f"vrg-epic-move: refusing to move {task.slug} out of deferred epic "
                    f"{current.slug}: {epics.DEFERRED_ROUTE_ONLY}",
                    file=sys.stderr,
                )
                return 1
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
    parser.add_argument(
        "--task", required=True, help="Task ref: owner/repo#N, or #N in the current repo"
    )
    parser.add_argument(
        "--epic",
        required=True,
        help="Target epic: 'adhoc' (the task repo's ad-hoc epic), 'intake' or "
        "'planning' (the task repo's intake or planning queue epic), #N (issue N in the "
        "task repo's epic home: <org>/.github for a public repo, the repo itself for "
        "a self-homed private one), or owner/repo#N (taken as written)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
