"""Move a task issue to a different epic (idempotent re-parent).

A task with a parent moves in one atomic re-parent (``addSubIssue`` with
``replaceParent: true``, :func:`epics.reparent_child`), never unlink-then-link,
so a failed move leaves the task under its old epic; a task with no parent is
linked with :func:`epics.add_child`. A closed target epic is refused by name
before any write: closed epics are not reopened by the tooling (epic
vergil-project/.github#1609). The target epic is resolved via
:func:`epics.resolve_epic_ref` (accepting the ``adhoc``, ``intake``,
``planning``, ``hoshin`` and ``operations`` sentinels and validating
epic-ness); moving a task already under an open target epic is a no-op.

The sentinels and a bare ``#N`` epic ref are read against the **task's** repo,
never the cwd repo: ``adhoc`` is the task repo's ad-hoc epic, ``intake`` and
``planning`` its intake and planning queue epics, ``hoshin`` and
``operations`` its org's hoshin and operations epics (always in
``<org>/.github``), and ``#N``
is issue N in the task repo's epic home, ``<org>/.github``. A fully qualified
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
        # the same org — the standing sentinels (adhoc/intake/planning/hoshin/operations) and a
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
            # The epic-home existence check runs under the task owner's token.
            try:
                epic = epics.resolve_epic_ref(args.epic, repo=task_repo)
            except (ValueError, epics.StandingEpicNotVisibleError) as exc:
                # A standing epic created but not confirmed names the new epic.
                print(f"vrg-epic-move: {exc}", file=sys.stderr)
                return 1
            if epics.queue_kind(epic) == epics.DEFERRED.key:
                print(
                    f"vrg-epic-move: refusing to move {task.slug} into deferred epic "
                    f"{epic.slug}: {epics.DEFERRED_ROUTE_ONLY}",
                    file=sys.stderr,
                )
                return 1
            # Closed is closed: refuse a closed target before reading the
            # task's parent or writing anything. ClosedEpicError is an
            # EpicRefusalError, which cli_main reports in one line, exit 1.
            epics.require_open_epic(epic)
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
            try:
                if current is None:
                    epics.add_child(epic, task)
                else:
                    # One call, so a failed move never leaves the task unparented.
                    epics.reparent_child(epic, task)
            except (RuntimeError, github.GitHubAPIError) as exc:
                # A GraphQL error (RuntimeError) or a failed gh call: report it
                # in one line. The write is a single call, so no earlier
                # unlink has to be undone. A missing token keeps cli_main's
                # own message.
                if isinstance(exc, github.MissingGitHubTokenError):
                    raise
                print(
                    f"vrg-epic-move: moving {task.slug} under epic {epic.slug} failed: {exc}",
                    file=sys.stderr,
                )
                return 1
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
        "'planning' (the task repo's intake or planning queue epic), 'hoshin' or "
        "'operations' (the task org's hoshin or operations epic, in <org>/.github), "
        "#N (issue N in the "
        "task repo's epic home, <org>/.github), or owner/repo#N (taken as written)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
