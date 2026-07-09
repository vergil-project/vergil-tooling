"""Link a task issue under an epic as a native GitHub sub-issue.

The rollup (``vrg-finalize-pr``) decides whether to close an epic by querying its
native sub-issues; a ``Parent:`` reflink alone is only a flaky ``gh search``
fallback. This CLI creates the reliable native link via ``epics.add_child``
(which reopens a closed epic first, per the reopen-on-late-child rule). Use it
when creating a task under an epic, and to backfill reflink-only children.

Refs are ``owner/repo#N`` or bare ``#N`` (bare resolves to the current repo).
The epic lives in its resolved home (``.github`` for a public repo, the repo
itself when private), so pass it fully-qualified. A public task may not
hard-link under a private epic — that would leak the private repo's name and
break cross-boundary roll-up; such dependencies use a soft ``Blocked-by:``
reference from the private side instead (epic #130).
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
    # Visibility-boundary guard (epic #130): a task may hard-link to an epic only
    # if it is no more publicly visible than the epic's home. A public task under
    # a private epic would leak the private repo's name into a public issue and
    # break cross-boundary roll-up. Binary is_public suffices; fail-loud on a
    # probe error (never silently allow a leaking link).
    task_repo = f"{task.owner}/{task.repo}"
    epic_home = f"{epic.owner}/{epic.repo}"
    if github.is_public(task_repo) and not github.is_public(epic_home):
        print(
            f"vrg-epic-link: refusing to link public task {task.slug} under "
            f"less-visible epic {epic.slug} — a public issue must not name a "
            f"private epic (leak) and cross-boundary roll-up cannot fire. File "
            f"the public work as its own task and reference it from the private "
            f"epic's body with 'Blocked-by: {task_repo}#{task.number}'.",
            file=sys.stderr,
        )
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
