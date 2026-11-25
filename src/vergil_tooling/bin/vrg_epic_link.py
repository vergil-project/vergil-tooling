"""Link a task issue under an epic as a native GitHub sub-issue.

The rollup (``vrg-finalize-pr``) decides whether to close an epic by querying its
native sub-issues; a ``Parent:`` reflink alone is only a flaky ``gh search``
fallback. This CLI creates the reliable native link through the chain layer
(:func:`epic_chain.link_into`, epic vergil-project/.github#1609): the task goes
into the first container of the epic's chain with a free slot, rolling over
into a continuation when every one is full. A closed epic is refused by name
and never reopened (closed is closed), as is one at GitHub's nesting limit.
Use it when creating a task under an epic, and to backfill reflink-only
children.

Refs are ``owner/repo#N`` or bare ``#N``. A bare task ref resolves to the
current repo. A bare epic ref resolves to the **task's** epic home, never the
cwd repo: ``<org>/.github`` (#3411). A fully qualified epic ref is taken as written.
The epic must carry the ``epic`` label, and a continuation's ref resolves to its
chain's head, with a line on stderr saying so.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epic_chain, epics, github
from vergil_tooling.lib.cli import cli_main


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    default_repo = github.current_repo()
    try:
        task = epics.parse_issue_ref(args.task, default_repo=default_repo)
        task_repo = f"{task.owner}/{task.repo}"
        # Pre-network cross-org guard: a bare epic ref homes in the task's org,
        # so only a qualified epic ref can diverge (#2070, #3411).
        owner = epics.single_target_org(
            epics.parse_issue_ref(args.epic, default_repo=task_repo), task
        )
    except ValueError as exc:
        print(f"vrg-epic-link: {exc}", file=sys.stderr)
        return 1
    try:
        with github.target_org(owner):
            return _link(args.epic, task, task_repo)
    except github.NoInstallationError as exc:
        print(f"vrg-epic-link: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1


def _link(epic_ref: str, task: epics.IssueRef, task_repo: str) -> int:
    """Resolve *epic_ref* against the task's epic home, guard the task, and link.

    Runs under the task owner's App token, so the epic-home probe never uses a
    cwd-scoped token.
    """
    # The shared ref resolution: it refuses an issue that is not an epic, and a
    # continuation's ref (copied from GitHub's UI) means its chain's head. No
    # sentinel: this command links only into an epic it is given.
    try:
        epic = epics.resolve_epic_ref(epic_ref, repo=task_repo, sentinels=False)
    except ValueError as exc:
        print(f"vrg-epic-link: {exc}", file=sys.stderr)
        return 1
    # Task guard (#2842): --task must be a task, never an epic or a standing
    # epic's archive — linking one would nest an umbrella under an epic as
    # though it were work. A failed label read propagates (fail closed).
    kind = epics.non_task_kind(task)
    if kind is not None:
        message = epics.non_task_refusal_message("--task", task.slug, kind)
        print(f"vrg-epic-link: {message}", file=sys.stderr)
        return 1
    container = epic_chain.link_into(epic, task)
    print(f"Linked {task.slug} under epic {epic.slug}{epic_chain.container_note(epic, container)}.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Link a task under an epic as a native GitHub sub-issue."
    )
    parser.add_argument(
        "--epic",
        required=True,
        help="Epic ref: owner/repo#N (taken as written), or #N (issue N in the task "
        "repo's epic home, <org>/.github)",
    )
    parser.add_argument(
        "--task", required=True, help="Task ref: owner/repo#N, or #N in the current repo"
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
