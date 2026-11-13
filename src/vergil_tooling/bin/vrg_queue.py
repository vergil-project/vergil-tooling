"""View the intake, planning and deferred queues, and the ad-hoc tasks (read-only).

``vrg-queue intake`` lists the open items under the intake queue epic, oldest
first. ``vrg-queue planning`` lists the open items under the planning queue
epic with their state: waiting, in-progress, or ready-to-close. A planning
item that would be ready-to-close is held at in-progress, with a ``held:``
note, while any advisory still tracks it; reading the advisories needs
Repository security advisories access on the org's ``.github``.
``vrg-queue deferred`` lists the open items under the deferred queue epic,
least recently deferred first, each noted with its latest deferral (see
:mod:`vergil_tooling.lib.queue_view`). A bare ``vrg-queue`` lists all three
queues in that order; there, a queue whose epic does not exist yet is reported
as ``no queue yet`` instead of failing the run.

A queue that cannot be read (a failed GitHub call, an advisory home that is
not public, an advisory line naming another org, a malformed response) is
named on stderr with the reason and the run exits 1. A bare text run still
lists the queues it could read; a bare ``--json`` run prints nothing on
stdout, so a partial object is never taken for the whole.

The default home is the current org's ``.github`` pair; ``--repo OWNER/REPO``
selects that repo's queue home, which for a self-homed private repo is its own
queue. Nothing is created or changed.

``vrg-queue adhoc`` lists the open tasks under every live ad-hoc epic in the
home, grouped by repo and oldest first, each with its kind, its open
``Blocked-by:`` refs and its state (ready, blocked or in-flight). An in-flight
task also shows its marks and its disposition, handed-off or resume (see
:mod:`vergil_tooling.lib.in_flight`); the local marks are read from each repo's
main clone under the repo roots (``--root DIR``, repeatable, or the platform
default), and a repo with open ad-hoc tasks but no clone there is an error. It
is not a review queue, so a bare ``vrg-queue`` never shows it. With
``--repo``, a public repo narrows the list to its own ad-hoc epic in the org's
``.github`` (none gives an empty list), and a self-homed private repo selects
its own home.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, github, in_flight, live_branches, queue_view, security_records
from vergil_tooling.lib.cli import cli_main

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vergil_tooling.lib.queue_view import QueueItem

# The ad-hoc view: a choice of its own, never in _VIEWS, so a bare run never shows it.
_ADHOC = "adhoc"

# Insertion order is the order a bare ``vrg-queue`` lists the queues in.
_VIEWS = {
    "intake": queue_view.intake_items,
    "planning": queue_view.planning_items,
    "deferred": queue_view.deferred_items,
}

# What a view can raise reading GitHub, besides a missing queue epic: a failed
# ``gh`` call, an advisory home that is not public, or a malformed response or
# an advisory line naming another org (both ``ValueError``s; #3826). A missing
# token is not among them: it reaches ``cli_main``'s guided message. Each is
# reported, never shown as an empty queue.
_VIEW_ERRORS = (github.GitHubAPIError, security_records.PrivateHomeError, ValueError)

# What the ad-hoc view can raise besides: an in-flight source it cannot read or
# a repo with no main clone, and repo roots it cannot determine.
_ADHOC_ERRORS = (*_VIEW_ERRORS, in_flight.InFlightError, live_branches.RepoSetError)


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
    if args.queue == _ADHOC:
        return _adhoc(owner, repo=args.repo, roots=args.root, as_json=args.json)
    if args.root is not None:
        print("vrg-queue: --root applies only to adhoc", file=sys.stderr)
        return 1
    # Bare mode: None marks a queue whose epic does not exist yet.
    results: dict[str, list[QueueItem] | None] = {}
    # Bare mode: the queues that could not be read, and why.
    failures: dict[str, Exception] = {}
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
            except github.MissingGitHubTokenError:
                raise
            except _VIEW_ERRORS as exc:
                _report_failure(args.queue, home, name, exc)
                return 1
        else:
            for queue, view in _VIEWS.items():
                try:
                    results[queue] = view(home, name)
                except queue_view.QueueNotFoundError:
                    results[queue] = None
                except github.MissingGitHubTokenError:
                    raise
                except _VIEW_ERRORS as exc:
                    failures[queue] = exc
    if args.queue is not None:
        if args.json:
            print(json.dumps(queue_view.to_json(items), indent=2))
        else:
            _print_queue(args.queue, home, name, items)
        return 0
    if args.json:
        if failures:
            # Never a partial object: a failed queue must not read as present or as null.
            for queue, failure in failures.items():
                _report_failure(queue, home, name, failure)
            return 1
        payload = {q: None if v is None else queue_view.to_json(v) for q, v in results.items()}
        print(json.dumps(payload, indent=2))
        return 0
    for index, queue in enumerate(_VIEWS):
        if index:
            print()
        if queue in failures:
            print(f"{queue} queue for {name} in {home}: not shown (failed; see stderr)")
            _report_failure(queue, home, name, failures[queue])
        elif (found := results[queue]) is None:
            print(f"{queue} queue for {name} in {home}: no queue yet")
        else:
            _print_queue(queue, home, name, found)
    return 1 if failures else 0


def _adhoc(owner: str, *, repo: str | None, roots: Sequence[Path] | None, as_json: bool) -> int:
    """Show the ad-hoc tasks: the org's, or one ``owner/name`` *repo*'s.

    Without *repo* the home is the org's ``.github``, which must exist. With
    it, the home is the repo's epic home: the org's ``.github`` for a public
    repo, narrowed to its own ad-hoc epic, or a self-homed private repo itself.
    *roots* (``--root``) replace the platform's repo roots for the clones the
    in-flight marks are read from.
    """
    with github.target_org(owner):
        if repo is None:
            home = epics.require_org_home(owner)
            name = owner
        else:
            name = repo.split("/", 1)[1]
            home = epics.resolve_epic_home(owner, name)
        try:
            items = queue_view.adhoc_items(home, repo=repo, roots=roots)
        except github.MissingGitHubTokenError:
            raise
        except _ADHOC_ERRORS as exc:
            _report_failure(_ADHOC, home, name, exc)
            return 1
    if as_json:
        print(json.dumps(queue_view.adhoc_to_json(items), indent=2))
        return 0
    print(f"{_ADHOC} queue for {name} in {home}: {len(items)} open task(s)")
    if items:
        print(queue_view.render_adhoc_text(items, now=datetime.now(UTC)))
    return 0


def _report_failure(queue: str, home: str, name: str, exc: Exception) -> None:
    """Name the queue that could not be read, and why, on stderr."""
    print(
        f"vrg-queue: {queue} queue for {name} in {home} could not be read: {exc}",
        file=sys.stderr,
    )


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
            "three. 'adhoc' shows the open ad-hoc tasks (ready / blocked / in-flight), "
            "grouped by repo; it is shown only when named. Read-only."
        ),
    )
    parser.add_argument(
        "queue",
        nargs="?",
        choices=[*_VIEWS, _ADHOC],
        help="Which queue to show (default: intake, planning and deferred, in order)",
    )
    parser.add_argument(
        "--repo",
        help=(
            "Show this repo's queue home (owner/name); a self-homed private repo "
            "has its own queues. With adhoc, a public repo narrows the list to "
            "its own ad-hoc epic. Default: the current org's .github queues."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        metavar="DIR",
        help=(
            "adhoc only: a projects directory (or repo) to find each repo's main "
            "clone under, for the in-flight marks; repeatable. Replaces the "
            "platform default (/vergil/projects on a cloud VM, the projects mount "
            "on a Lima VM; none on a physical host)."
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
