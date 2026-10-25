"""View the intake and planning queues (read-only).

``vrg-queue intake`` lists the open items under the intake queue epic, oldest
first. ``vrg-queue planning`` lists the open items under the planning queue
epic with their state: waiting, in-progress, or ready-to-close (see
:mod:`vergil_tooling.lib.queue_view`). The default home is the current org's
``.github`` pair; ``--repo OWNER/REPO`` selects that repo's queue home, which
for a self-homed private repo is its own queue. Nothing is created or changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from vergil_tooling.lib import epics, github, queue_view
from vergil_tooling.lib.cli import cli_main

_VIEWS = {"intake": queue_view.intake_items, "planning": queue_view.planning_items}


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
    # Scope the App token to the target's owner, not the cwd org (#3409).
    with github.target_org(owner):
        if args.repo is None:
            # The org-wide default needs <owner>/.github to exist (#3613).
            epics.require_org_home(owner)
        home, name = epics.queue_home(target)
        try:
            items = _VIEWS[args.queue](home, name)
        except queue_view.QueueNotFoundError as exc:
            print(f"vrg-queue: {exc}", file=sys.stderr)
            return 1
    if args.json:
        print(json.dumps(queue_view.to_json(items), indent=2))
        return 0
    print(f"{args.queue} queue for {name} in {home}: {len(items)} open item(s)")
    if items:
        print(queue_view.render_text(items, now=datetime.now(UTC)))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-queue",
        description=(
            "Show the intake queue (oldest first) or the planning queue "
            "(waiting / in-progress / ready-to-close). Read-only."
        ),
    )
    parser.add_argument("queue", choices=sorted(_VIEWS), help="Which queue to show")
    parser.add_argument(
        "--repo",
        help=(
            "Show this repo's queue home (owner/name); a self-homed private repo "
            "has its own queues. Default: the current org's .github queues."
        ),
    )
    parser.add_argument("--json", action="store_true", help="Emit a JSON list of queue items")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
