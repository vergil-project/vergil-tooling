"""View the intake, planning and deferred queues (read-only).

``vrg-queue intake`` lists the open items under the intake queue epic, oldest
first. ``vrg-queue planning`` lists the open items under the planning queue
epic with their state: waiting, in-progress, or ready-to-close.
``vrg-queue deferred`` lists the open items under the deferred queue epic,
least recently deferred first, each noted with its latest deferral (see
:mod:`vergil_tooling.lib.queue_view`). A bare ``vrg-queue`` lists all three
queues in that order; there, a queue whose epic does not exist yet is reported
as ``no queue yet`` instead of failing the run.

The default home is the current org's ``.github`` pair; ``--repo OWNER/REPO``
selects that repo's queue home, which for a self-homed private repo is its own
queue. Nothing is created or changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, github, queue_view
from vergil_tooling.lib.cli import cli_main

if TYPE_CHECKING:
    from vergil_tooling.lib.queue_view import QueueItem

# Insertion order is the order a bare ``vrg-queue`` lists the queues in.
_VIEWS = {
    "intake": queue_view.intake_items,
    "planning": queue_view.planning_items,
    "deferred": queue_view.deferred_items,
}


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.repo is not None:
        target = args.repo
        if "/" not in target:
            print(f"vrg-queue: --repo must be 'owner/repo' (got {target!r})", file=sys.stderr)
            return 1
    else:
        target = f"{github.current_repo().split('/', 1)[0]}/.github"
    owner = target.split("/", 1)[0]
    # Bare mode: None marks a queue whose epic does not exist yet.
    results: dict[str, list[QueueItem] | None] = {}
    # Scope the App token to the target's owner, not the cwd org (#3409).
    with github.target_org(owner):
        if args.repo is None:
            # The org-wide default needs <owner>/.github to exist (#3613).
            epics.require_org_home(owner)
        home, name = epics.queue_home(target)
        if args.queue is not None:
            try:
                items = _VIEWS[args.queue](home, name)
            except queue_view.QueueNotFoundError as exc:
                print(f"vrg-queue: {exc}", file=sys.stderr)
                return 1
        else:
            for queue, view in _VIEWS.items():
                try:
                    results[queue] = view(home, name)
                except queue_view.QueueNotFoundError:
                    results[queue] = None
    if args.queue is not None:
        if args.json:
            print(json.dumps(queue_view.to_json(items), indent=2))
        else:
            _print_queue(args.queue, home, name, items)
        return 0
    if args.json:
        payload = {q: None if v is None else queue_view.to_json(v) for q, v in results.items()}
        print(json.dumps(payload, indent=2))
        return 0
    for index, (queue, found) in enumerate(results.items()):
        if index:
            print()
        if found is None:
            print(f"{queue} queue for {name} in {home}: no queue yet")
        else:
            _print_queue(queue, home, name, found)
    return 0


def _print_queue(queue: str, home: str, name: str, items: list[QueueItem]) -> None:
    print(f"{queue} queue for {name} in {home}: {len(items)} open item(s)")
    if items:
        print(queue_view.render_text(items, now=datetime.now(UTC)))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-queue",
        description=(
            "Show the intake queue (oldest first), the planning queue "
            "(waiting / in-progress / ready-to-close), or the deferred queue "
            "(least recently deferred first). With no queue named, show all "
            "three. Read-only."
        ),
    )
    parser.add_argument(
        "queue",
        nargs="?",
        choices=list(_VIEWS),
        help="Which queue to show (default: all three, in order)",
    )
    parser.add_argument(
        "--repo",
        help=(
            "Show this repo's queue home (owner/name); a self-homed private repo "
            "has its own queues. Default: the current org's .github queues."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a JSON list of queue items; with no queue named, an object "
            "keyed by queue (null for a queue that does not exist yet)"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
