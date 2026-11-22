"""Route one container-epic item: drop, defer, intake, one-off, planning, split, or size.

A thin argparse layer over :func:`vergil_tooling.lib.intake_route.route`,
which owns the mechanics (transfer-or-recreate, reparent, re-link, close with
a pointer, record a deferral, record a size) so no queue review hand-rolls
them::

    vrg-intake-route <issue-ref> drop --reason TEXT
    vrg-intake-route <issue-ref> defer --reason TEXT --revisit-when TEXT [--date YYYY-MM-DD]
    vrg-intake-route <issue-ref> intake
    vrg-intake-route <issue-ref> one-off --repo OWNER/REPO [--epic REF]
    vrg-intake-route <issue-ref> planning [--tasks N [--confidence high|med|low] --reason TEXT]
    vrg-intake-route <issue-ref> split --into REF [--into REF ...] --reason TEXT
    vrg-intake-route <issue-ref> size --tasks N [--confidence high|med|low] --reason TEXT

``size`` records a planning or deferred item's estimated epic size: it posts
the dated sizing comment, then sets the ``epic-size:*`` label derived from
``--tasks`` (S 1-9, M 10-19, L 20-39, XL 40+) and removes any other; it
neither moves nor closes the item. ``--confidence`` defaults to ``med``, and
``--reason`` is one line. It prints ``Sized <ref>: epic-size X, about N
tasks.``. If the label cannot be set after the comment posted, it exits 1
naming the command that finishes it (re-running the same ``size`` is safe).
``planning`` takes the same sizing, ``--tasks`` and ``--reason`` together or
not at all (``--confidence`` needs ``--tasks``), and applies it once the item
is in planning, printing the ``Sized`` line after ``Routed``; a bad sizing is
refused before anything changes. Without it the item keeps any size it has.

A bare ``#N`` item ref reads against the current repo; a bare ``--into`` ref
reads against the item's repo. Which routes an item allows depends on the
container epic it is in (:data:`intake_route.ROUTES`); ``planning`` also
takes a follow-on brainstorm out of a development epic. A refused route (a
route its container does not allow, any other development-epic task,
or cross-org) exits 1 with the reason on stderr. So
does a one-off whose item does not end up under its target epic: the message
names where the item is and how to recover (the ``vrg-epic-move`` fix for a
wrong parent, the GitHub UI when GitHub keeps refusing the link as a duplicate
sub-issue, a re-run of the one-off when the target epic does not list the moved
item or a queue epic still does), and ``Routed`` is never printed for it. That
re-run, on the moved item with its own repo as ``--repo``, links it under the
target epic if that epic lacks it, then removes the stale queue listing. It
searches only the queue epics of the moved item's queue home and of its org's
``.github``; a stale listing elsewhere is named for removal in the GitHub UI.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from vergil_tooling.lib import epics, github, intake_route
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.epic_size import CONFIDENCES, Sizing

_PROG = "vrg-intake-route"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        default_repo = github.current_repo() if args.issue.strip().startswith("#") else ""
        ref = epics.parse_issue_ref(args.issue, default_repo=default_repo)
        action = _action(args, item_repo=f"{ref.owner}/{ref.repo}")
        result = intake_route.route(ref, action)
    except (
        ValueError,
        intake_route.RouteHalfAppliedError,
        epics.StandingEpicNotVisibleError,
    ) as exc:
        # A half-applied route already names the item's state and the fix; a
        # queue epic created but not confirmed names the new epic to check.
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    except github.NoInstallationError as exc:
        print(f"{_PROG}: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    sizing = None
    if isinstance(action, intake_route.SizeItem | intake_route.ToPlanning):
        sizing = action.sizing
    # The size route moves nothing, so it prints only its Sized line.
    if not isinstance(action, intake_route.SizeItem):
        print(_routed_line(ref, args.route, result))
    if sizing is not None:
        print(f"Sized {ref.slug}: epic-size {sizing.size}, about {sizing.tasks} tasks.")
    return 0


def _routed_line(ref: epics.IssueRef, route: str, result: intake_route.RouteResult) -> str:
    if result.final == ref:
        return f"Routed {ref.slug}: {route}."
    return f"Routed {ref.slug}: {route}, now {result.final.slug} (by {result.moved_by})."


def _sizing(args: argparse.Namespace) -> Sizing:
    """The sizing *args* give; :class:`Sizing` refuses a bad value with a ValueError."""
    if args.tasks is None or args.reason is None:
        raise ValueError("--tasks and --reason are given together or not at all")
    return Sizing(tasks=args.tasks, confidence=args.confidence or "med", reason=args.reason)


def _planning_sizing(args: argparse.Namespace) -> Sizing | None:
    """The optional sizing of the planning route, refused before anything is routed.

    ``--tasks`` and ``--reason`` come together or not at all, and
    ``--confidence`` needs ``--tasks``.
    """
    if args.tasks is None and args.reason is None:
        if args.confidence is not None:
            raise ValueError("--confidence needs --tasks and --reason")
        return None
    return _sizing(args)


def _action(args: argparse.Namespace, *, item_repo: str) -> intake_route.RouteAction:
    if args.route == "drop":
        return intake_route.Drop(reason=args.reason)
    if args.route == "defer":
        return intake_route.Defer(reason=args.reason, revisit_when=args.revisit_when, day=args.date)
    if args.route == "intake":
        return intake_route.ToIntake()
    if args.route == "one-off":
        return intake_route.OneOff(repo=args.repo, epic=args.epic)
    if args.route == "planning":
        return intake_route.ToPlanning(sizing=_planning_sizing(args))
    if args.route == "size":
        return intake_route.SizeItem(sizing=_sizing(args))
    into = tuple(epics.parse_issue_ref(r, default_repo=item_repo) for r in args.into)
    return intake_route.Split(into=into, reason=args.reason)


def _iso_date(value: str) -> date:
    """An argparse type: a strict ``YYYY-MM-DD`` date."""
    try:
        if len(value) != len("YYYY-MM-DD"):
            raise ValueError(value)
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value!r}") from None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description="Route one item between the container epics (intake, planning, ad hoc, "
        "deferred).",
    )
    parser.add_argument(
        "issue", help="Container-epic item: owner/repo#N, or #N in the current repo"
    )
    routes = parser.add_subparsers(dest="route", required=True, metavar="ROUTE")

    drop = routes.add_parser("drop", help="Close the item with the reason")
    drop.add_argument("--reason", required=True)

    defer = routes.add_parser(
        "defer",
        help="Move the item into the deferred queue with a dated record (on a deferred "
        "item: refresh the record)",
    )
    defer.add_argument("--reason", required=True, help="Why the item is waiting")
    defer.add_argument(
        "--revisit-when",
        required=True,
        help="What to check to know when it can come back (a date, events, or both)",
    )
    defer.add_argument(
        "--date",
        type=_iso_date,
        default=None,
        help="Stamp this YYYY-MM-DD date instead of today (keeps an original deferral date)",
    )

    routes.add_parser("intake", help="Reparent a deferred item back onto the intake epic")

    one_off = routes.add_parser(
        "one-off", help="Move the item into its repo and link it under an epic"
    )
    one_off.add_argument("--repo", required=True, help="Target repo (owner/repo)")
    one_off.add_argument(
        "--epic",
        default="adhoc",
        help="'adhoc' (default: the target repo's ad-hoc epic), #N (issue N in the target "
        "repo's epic home), or owner/repo#N",
    )

    planning = routes.add_parser(
        "planning",
        help="Reparent the item onto the planning epic (intake, deferred, ad-hoc or unparented "
        "item, "
        "or a follow-on brainstorm under a development epic), optionally sizing it",
    )
    _add_sizing_args(planning, required=False)

    split = routes.add_parser("split", help="Close the item, linking the tasks it was split into")
    split.add_argument(
        "--into",
        action="append",
        required=True,
        help="A task the item was split into (repeatable; #N reads against the item's repo)",
    )
    split.add_argument("--reason", required=True)

    size = routes.add_parser(
        "size",
        help="Record a planning or deferred item's estimated epic size (comment and label)",
    )
    _add_sizing_args(size, required=True)
    return parser.parse_args(argv)


def _add_sizing_args(route: argparse.ArgumentParser, *, required: bool) -> None:
    """Add ``--tasks``, ``--confidence`` and ``--reason`` to *route*."""
    route.add_argument(
        "--tasks",
        type=int,
        required=required,
        default=None,
        help="Estimated task count (a positive integer); the size is derived from it",
    )
    route.add_argument(
        "--confidence",
        choices=CONFIDENCES,
        default=None,
        help="Confidence in the estimate (default: med)",
    )
    route.add_argument(
        "--reason",
        required=required,
        default=None,
        help="One line on why the estimate is what it is",
    )


if __name__ == "__main__":
    sys.exit(main())
