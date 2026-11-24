"""View the intake, planning and deferred queues, and the ad-hoc and hoshin tasks (read-only).

``vrg-queue intake`` lists the open items under the intake queue epic, oldest
first. ``vrg-queue planning`` lists the open items under the planning queue
epic with their state: waiting, in-progress, or ready-to-close. A planning
item that would be ready-to-close is held at in-progress, with a ``held:``
note, while any advisory still tracks it or it names, with the ``security``
label, an advisory that cannot be read; reading the advisories needs
Repository security advisories access on the org's ``.github``.
``vrg-queue deferred`` lists the open items under the deferred queue epic,
least recently deferred first, each noted with its latest deferral (see
:mod:`vergil_tooling.lib.queue_view`). A bare ``vrg-queue`` lists all three
queues in that order; there, a queue whose epic does not exist yet is reported
as ``no queue yet`` instead of failing the run.

Each planning and deferred line shows the item's epic size after its age:
``M/14`` (size and estimated task count), ``?`` when unsized, ``!`` when its
sizing is in error (see :mod:`vergil_tooling.lib.epic_size`); ``--json`` adds
``epic_size``, ``estimated_tasks``, ``confidence`` and ``size_error`` to each
of their items. Intake lines and items are unchanged. For the planning view
only, ``--sort age|size|tasks`` (default ``age``, oldest first) orders the
queue and ``--size S,M`` (``unsized`` for the unsized) filters it; an item in
error is always listed, and a filtered header adds ``<M> shown``. Either flag
with any other view, or a bare run, is refused.

Exit status: **0** when every queue was read in full and no item's sizing is
in error; **2** when every queue was read in full and one or more items are in
error, each named on stderr as ``vrg-queue: <ref>: <error>`` after the full
listing (``--json`` included); **1** when a queue could not be read, an item
was stopped, or the command line is invalid (every usage error exits 1, never
2). 1 wins over 2: 2 always means a complete listing.

A queue that cannot be read (a failed GitHub call, an advisory line naming
another org, a malformed response, trusted authors not configured or invalid)
is named on stderr with the reason and the run exits 1. A bare text run still
lists the queues it could read; a bare ``--json`` run prints nothing on
stdout, so a partial object is never taken for the whole.

An item whose comments the trust layer cannot read in full is **stopped**,
not the queue (spec §5.3 of epic vergil-project/.github#1467: batch runs stop
the item, not the run). A text run lists the rest of the queue; its header
counts the stopped items (``<N> open item(s), <K> stopped``, ``<N>`` counting
them), and each is shown after the listed items as
``<ref>  stopped: its comments could not be read (see stderr)``. A planning
item is also stopped when the trust layer does not trust its body, whose
``Security advisory:`` lines the advisory hold reads: it is shown as
``<ref>  stopped: its body is untrusted (see stderr)``, and under GitHub
Actions it is counted, never named. After every
listing, each stop is named on stderr by the trust layer's
:class:`~vergil_tooling.lib.trust.BatchStops` (``stopped on a read failure:
<ref> on comments: <reason>``; under GitHub Actions, a count and the refs),
and the run exits 1. A ``--json`` run, named queue or bare, with any stopped
item prints nothing on stdout, as for a queue that cannot be read: a list
without its stopped items must not be taken for the whole queue. It still
reads every queue and names every stop on stderr, then exits 1.

The home is always an org's ``.github``, which must exist: by default the
current org's, and with ``--repo OWNER/REPO`` that repo's org's. Nothing is
created or changed.

``vrg-queue adhoc`` lists the open tasks under every live ad-hoc epic in the
home, grouped by repo and oldest first, each with its kind, its open blockers
(its body ``Blocked-by:`` refs and its native blocked-by relationships, in any
org; a list it cannot read in full is an error) and its state (ready, blocked
or in-flight). An in-flight
task also shows its marks and its disposition, handed-off or resume (see
:mod:`vergil_tooling.lib.in_flight`); the local marks are read from each repo's
main clone under the repo roots (``--root DIR``, repeatable, or the platform
default), and a repo with open ad-hoc tasks but no clone there is an error. It
is not a review queue, so a bare ``vrg-queue`` never shows it. With
``--repo``, the list narrows to that repo's own ad-hoc epic in the org's
``.github`` (none gives an empty list).

``vrg-queue hoshin`` lists the open tasks under the org's ``Epic (hoshin):
<org>`` epic, the tasks ``/vergil:hoshin`` files, in the ad-hoc form, with the
same flags (``--json``, ``--repo``, ``--root``). Its home is the org's
``.github``; with ``--repo``, only the epic's tasks in that repo are listed.
An org with no hoshin epic yet gives an empty
list. A bare ``vrg-queue`` never shows it.

In both task views, a task whose body the trust layer does not trust, or
cannot read in full, is **stopped**, not the view (#4971): its
``Blocked-by:`` lines count only from a trusted body. A text run lists the
rest; the header reads ``<N> open task(s), <K> stopped`` (``<N>`` counting
them), and each stopped task follows as ``<ref>  stopped: its body is
untrusted (see stderr)`` or ``... could not be read (see stderr)``. Under
GitHub Actions a task stopped on untrusted content is counted, never named,
on stdout and stderr alike. Every stop is then named on stderr by
:class:`~vergil_tooling.lib.trust.BatchStops` and the run exits 1. A
``--json`` run with a stopped task prints nothing on stdout, names every stop
and says why no JSON was printed on stderr, and exits 1.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from vergil_tooling.lib import (
    epics,
    github,
    in_flight,
    live_branches,
    queue_view,
    trust,
    trusted_authors,
)
from vergil_tooling.lib.cli import cli_main

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vergil_tooling.lib.queue_view import QueueItem

# The one view ``--sort`` and ``--size`` apply to.
_PLANNING = "planning"

# The task views: choices of their own, never in _VIEWS, so a bare run never shows them.
_ADHOC = "adhoc"
_HOSHIN = "hoshin"
_TASK_VIEWS = (_ADHOC, _HOSHIN)

# Insertion order is the order a bare ``vrg-queue`` lists the queues in.
_VIEWS = {
    "intake": queue_view.intake_items,
    "planning": queue_view.planning_items,
    "deferred": queue_view.deferred_items,
}

# What a view can raise reading GitHub, besides a missing queue epic: a failed
# ``gh`` call, or a malformed response or an advisory line naming another org
# (both ``ValueError``s; #3826). A missing token is not among them: it reaches
# ``cli_main``'s guided message. Each is reported, never shown as an empty queue.
# Trusted authors that are not configured fail the intake, planning and deferred views
# (invalid ones are a ``ValueError``), never counting every comment instead.
# An item's comment read the trust layer cannot complete stops that item only
# (the view's ``stops``), as does a task view's body read the layer does not
# trust or cannot complete; any other ``TrustReadError`` still fails the view.
_VIEW_ERRORS = (
    github.GitHubAPIError,
    trust.TrustReadError,
    trusted_authors.TrustedAuthorsNotConfiguredError,
    ValueError,
)

# What the task views can raise besides: a native blocked-by list they cannot read
# in full, an in-flight source they cannot read or a repo with no main clone, and
# repo roots they cannot determine.
_TASK_ERRORS = (
    *_VIEW_ERRORS,
    epics.NativeBlockersReadError,
    in_flight.InFlightError,
    live_branches.RepoSetError,
)


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    for flag, value in (("--sort", args.sort), ("--size", args.size)):
        if value is not None and args.queue != _PLANNING:
            print(f"vrg-queue: {flag} applies only to {_PLANNING}", file=sys.stderr)
            return 1
    sizes: frozenset[str] | None = None
    if args.size is not None:
        try:
            sizes = queue_view.parse_size_filter(args.size)
        except ValueError as exc:
            print(f"vrg-queue: --size: {exc}", file=sys.stderr)
            return 1
    if args.repo is not None:
        target = args.repo
        if "/" not in target:
            print(f"vrg-queue: --repo must be 'owner/repo' (got {target!r})", file=sys.stderr)
            return 1
    else:
        target = f"{github.current_repo().split('/', 1)[0]}/.github"
    owner = target.split("/", 1)[0]
    if args.queue in _TASK_VIEWS:
        return _tasks(args.queue, owner, repo=args.repo, roots=args.root, as_json=args.json)
    if args.root is not None:
        print(f"vrg-queue: --root applies only to {' and '.join(_TASK_VIEWS)}", file=sys.stderr)
        return 1
    # Bare mode: None marks a queue whose epic does not exist yet.
    results: dict[str, list[QueueItem] | None] = {}
    # Bare mode: the queues that could not be read, and why.
    failures: dict[str, Exception] = {}
    # Each queue's stopped items (spec §5.3: a batch run stops the item, not the run).
    stops = {queue: trust.BatchStops() for queue in _VIEWS}
    # Scope the App token to the target's owner, not the cwd org (#3409).
    with github.target_org(owner):
        # The queues live in <owner>/.github, which must exist (#3613).
        home, name = epics.require_org_home(owner), owner
        if args.queue is not None:
            try:
                items = _VIEWS[args.queue](home, name, stops=stops[args.queue])
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
                    results[queue] = view(home, name, stops=stops[queue])
                except queue_view.QueueNotFoundError:
                    results[queue] = None
                except github.MissingGitHubTokenError:
                    raise
                except _VIEW_ERRORS as exc:
                    failures[queue] = exc
    if args.queue is not None:
        queue_stops = stops[args.queue]
        total = len(items) + len(queue_stops.stopped)
        if args.queue == _PLANNING:
            items = queue_view.sort_planning(items, args.sort or "age")
            if sizes is not None:
                items = queue_view.filter_planning(items, sizes)
        if args.json:
            if queue_stops.stopped:
                _report_stops(args.queue, home, name, queue_stops, as_json=True)
                return 1
            print(json.dumps(queue_view.to_json(items), indent=2))
        else:
            shown = len(items) if sizes is not None else None
            _print_queue(args.queue, home, name, items, total=total, shown=shown, stops=queue_stops)
            _report_stops(args.queue, home, name, queue_stops, as_json=False)
        status = _report_size_errors(items)
        # A stopped item (exit 1) wins over items in error (exit 2).
        return queue_stops.exit_code() or status
    stopped = [queue for queue, found in stops.items() if found.stopped]
    if args.json:
        if failures or stopped:
            # Never a partial object: a failed queue must not read as present or as
            # null, and a queue without its stopped items must not read as whole.
            for queue, failure in failures.items():
                _report_failure(queue, home, name, failure)
            for queue in stopped:
                _report_stops(queue, home, name, stops[queue], as_json=True)
            return 1
        payload = {q: None if v is None else queue_view.to_json(v) for q, v in results.items()}
        print(json.dumps(payload, indent=2))
        return _report_size_errors(v for found in results.values() if found for v in found)
    for index, queue in enumerate(_VIEWS):
        if index:
            print()
        if queue in failures:
            print(f"{queue} queue for {name} in {home}: not shown (failed; see stderr)")
            _report_failure(queue, home, name, failures[queue])
        elif (found := results[queue]) is None:
            print(f"{queue} queue for {name} in {home}: no queue yet")
        else:
            total = len(found) + len(stops[queue].stopped)
            _print_queue(queue, home, name, found, total=total, shown=None, stops=stops[queue])
    for queue in stopped:
        _report_stops(queue, home, name, stops[queue], as_json=False)
    status = _report_size_errors(v for found in results.values() if found for v in found)
    # A queue that could not be read or a stopped item (exit 1) wins over items in
    # error (exit 2).
    return 1 if failures or stopped else status


def _report_stops(
    queue: str, home: str, name: str, stops: trust.BatchStops, *, as_json: bool
) -> None:
    """Name *queue*'s stopped items on stderr; with *as_json*, say why no JSON was printed."""
    if not stops.stopped:
        return
    stops.print_summary(sys.stderr)
    if as_json:
        print(
            f"vrg-queue: {queue} queue for {name} in {home}: {len(stops.stopped)} item(s) "
            "stopped, so no JSON is printed (a list without them is not the whole queue)",
            file=sys.stderr,
        )


def _report_size_errors(items: Iterable[QueueItem]) -> int:
    """Name each listed item whose sizing is in error on stderr; 2 if any, else 0."""
    errors = queue_view.size_errors(list(items))
    for item, error in errors:
        print(f"vrg-queue: {item.ref.slug}: {error}", file=sys.stderr)
    return 2 if errors else 0


def _tasks(
    view: str, owner: str, *, repo: str | None, roots: Sequence[Path] | None, as_json: bool
) -> int:
    """Show the ad-hoc or hoshin tasks (*view*): the org's, or one ``owner/name`` *repo*'s.

    The home is the org's ``.github``, which must exist. With *repo*, the
    ad-hoc view narrows to that repo's own ad-hoc epic, and the hoshin view
    to the hoshin epic's children in *repo*. *roots* (``--root``) replace the
    platform's repo roots for the clones the in-flight marks are read from.
    """
    stops = trust.BatchStops()
    with github.target_org(owner):
        home = epics.require_org_home(owner)
        name = owner if repo is None else repo.split("/", 1)[1]
        try:
            read = queue_view.hoshin_items if view == _HOSHIN else queue_view.adhoc_items
            items = read(home, repo=repo, roots=roots, stops=stops)
        except github.MissingGitHubTokenError:
            raise
        except _TASK_ERRORS as exc:
            _report_failure(view, home, name, exc)
            return 1
    if as_json:
        if stops.stopped:
            # Never a partial list: one without its stopped tasks is not the whole view.
            _report_stops(view, home, name, stops, as_json=True)
            return 1
        print(json.dumps(queue_view.adhoc_to_json(items), indent=2))
        return 0
    header = f"{view} queue for {name} in {home}: {len(items) + len(stops.stopped)} open task(s)"
    if stops.stopped:
        header += f", {len(stops.stopped)} stopped"
    print(header)
    if items:
        print(queue_view.render_adhoc_text(items, now=datetime.now(UTC)))
    for item in stops.named:
        print(_stopped_line(item))
    _report_stops(view, home, name, stops, as_json=False)
    return stops.exit_code()


def _report_failure(queue: str, home: str, name: str, exc: Exception) -> None:
    """Name the queue that could not be read, and why, on stderr."""
    print(
        f"vrg-queue: {queue} queue for {name} in {home} could not be read: {exc}",
        file=sys.stderr,
    )


def _print_queue(
    queue: str,
    home: str,
    name: str,
    items: list[QueueItem],
    *,
    total: int,
    shown: int | None,
    stops: trust.BatchStops,
) -> None:
    """The queue's header and lines, then a line per stopped item.

    *total* counts the stopped items too; *stops* adds ``, <K> stopped`` and
    *shown* (a filtered count) adds ``, <M> shown``.
    """
    header = f"{queue} queue for {name} in {home}: {total} open item(s)"
    if stops.stopped:
        header += f", {len(stops.stopped)} stopped"
    if shown is not None:
        header += f", {shown} shown"
    print(header)
    if items:
        print(queue_view.render_text(items, now=datetime.now(UTC)))
    for item in stops.named:
        print(_stopped_line(item))


def _stopped_line(item: trust.Skipped) -> str:
    """The listing's line for a stopped item: its ref, the field and why, metadata only."""
    why = "could not be read" if item.cause == trust.READ_FAILURE else "is untrusted"
    return f"{item.repo}#{item.number}  stopped: its {item.field} {why} (see stderr)"


class _Parser(argparse.ArgumentParser):
    """An argument parser whose usage errors exit 1, never argparse's 2.

    Exit 2 means a complete listing with items in error, so a bad flag or
    choice must not read as one.
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = _Parser(
        prog="vrg-queue",
        description=(
            "Show the intake queue (oldest first), the planning queue "
            "(waiting / in-progress / ready-to-close), or the deferred queue "
            "(least recently deferred first). With no queue named, show all "
            "three. 'adhoc' shows the open ad-hoc tasks (ready / blocked / in-flight), "
            "grouped by repo, and 'hoshin' the open tasks under the hoshin epic "
            "in the same form; each is shown only when named. Read-only."
        ),
    )
    parser.add_argument(
        "queue",
        nargs="?",
        choices=[*_VIEWS, *_TASK_VIEWS],
        help="Which queue to show (default: intake, planning and deferred, in order)",
    )
    parser.add_argument(
        "--repo",
        help=(
            "Show the queues of this repo's org (owner/name), in its .github. "
            "With adhoc, the list narrows to the repo's own ad-hoc epic; with "
            "hoshin, the org's hoshin epic is narrowed to the repo's tasks. "
            "Default: the current org's .github queues."
        ),
    )
    parser.add_argument(
        "--root",
        action="append",
        type=Path,
        metavar="DIR",
        help=(
            "adhoc and hoshin only: a projects directory (or repo) to find each repo's main "
            "clone under, for the in-flight marks; repeatable. Replaces the "
            "platform default (/vergil/projects on a cloud VM, the projects mount "
            "on a Lima VM; none on a physical host)."
        ),
    )
    parser.add_argument(
        "--sort",
        choices=queue_view.PLANNING_SORTS,
        help=(
            "planning only: order by age (oldest first, the default), size "
            "(S, M, L, XL, then unsized, then in error) or tasks (estimated "
            "task count, then unsized, then in error)"
        ),
    )
    parser.add_argument(
        "--size",
        metavar="LIST",
        help=(
            "planning only: show only these sizes, a comma list of S, M, L, XL "
            "and unsized (e.g. S,unsized); items in error are always shown"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit a JSON list of queue items; with no queue named, an object "
            "keyed by queue (null for a queue that does not exist yet). Nothing is "
            "printed when a queue cannot be read or an item is stopped"
        ),
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
