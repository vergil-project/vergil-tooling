"""Print a task's epic and its kind, read through the epic's chain.

A task in an epic past GitHub's 100-sub-issue limit is linked in one of the
epic's continuations, so its native parent names the continuation, not the
epic (epic vergil-project/.github#1609). This command gives the chain-aware
answer, for the plugin skills to use instead of reading a task's native
parent and that parent's labels.

Plain output is one line, ``<epic ref> <kind>``: the chain's head and its
kind (``development``, ``proxy``, or a standing kind's key such as
``ad-hoc``, ``intake``, ``planning`` or ``deferred``). A parent that is not
an open-work epic prints ``<archive ref> archive`` or ``<parent ref>
not-epic``, and a task with no native parent prints ``none``. ``--json``
prints ``{"task", "epic", "kind", "container"}``, ``container`` being the
native parent (the head, or the continuation that holds the task).

The task ref is ``owner/repo#N``, or a bare ``#N`` in the current repo.
Exits 0 with an answer, 1 on a bad ref, a failed read or chain drift.
"""

from __future__ import annotations

import argparse
import json
import sys

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.cli import cli_main


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        default_repo = github.current_repo() if args.task.strip().startswith("#") else ""
        task = epics.parse_issue_ref(args.task, default_repo=default_repo)
    except ValueError as exc:
        print(f"vrg-epic-parent: {exc}", file=sys.stderr)
        return 1
    # A failed read, chain drift (a RuntimeError) and a head with no single kind
    # (a ValueError) each end the run with their error; an auth or trust-layer
    # failure reaches cli_main, which does the same.
    try:
        with github.target_org(task.owner):
            found = epics.task_epic(task)
    except (github.GitHubAPIError, RuntimeError, ValueError) as exc:
        print(f"vrg-epic-parent: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(_as_json(found)))
    else:
        print(_as_line(found))
    return 0


def _slug(ref: epics.IssueRef | None) -> str | None:
    return ref.slug if ref is not None else None


def _as_json(found: epics.TaskEpic) -> dict[str, str | None]:
    return {
        "task": found.task.slug,
        "epic": _slug(found.epic),
        "kind": found.kind,
        "container": _slug(found.container),
    }


def _as_line(found: epics.TaskEpic) -> str:
    if found.kind == epics.NO_PARENT_KIND:
        return epics.NO_PARENT_KIND
    # not-epic names the plain parent; every other kind names its epic (or archive).
    named = found.container if found.kind == epics.NOT_EPIC_KIND else found.epic
    if named is None:
        raise RuntimeError(f"no ref to print for {found.task.slug}'s {found.kind} parent")
    return f"{named.slug} {found.kind}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Print a task's epic (its chain's head) and the epic's kind."
    )
    parser.add_argument("task", help="Task ref: owner/repo#N, or #N in the current repo")
    parser.add_argument(
        "--json",
        action="store_true",
        help='Print {"task", "epic", "kind", "container"}, container being the native parent',
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
