"""Route one container-epic item: drop, defer, intake, one-off, planning, or split.

A thin argparse layer over :func:`vergil_tooling.lib.intake_route.route`,
which owns the mechanics (transfer-or-recreate, reparent, re-link, close with
a pointer, record a deferral) so no queue review hand-rolls them::

    vrg-intake-route <issue-ref> drop --reason TEXT
    vrg-intake-route <issue-ref> defer --reason TEXT --revisit-when TEXT [--date YYYY-MM-DD]
    vrg-intake-route <issue-ref> intake
    vrg-intake-route <issue-ref> one-off --repo OWNER/REPO [--epic REF]
    vrg-intake-route <issue-ref> planning
    vrg-intake-route <issue-ref> split --into REF [--into REF ...] --reason TEXT

A bare ``#N`` item ref reads against the current repo; a bare ``--into`` ref
reads against the item's repo. Which routes an item allows depends on the
container epic it is in (:data:`intake_route.ROUTES`). A refused route (a
route its container does not allow, a development-epic task, cross-org, or a
private item headed somewhere public) exits 1 with the reason on stderr.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from vergil_tooling.lib import epics, github, intake_route
from vergil_tooling.lib.cli import cli_main

_PROG = "vrg-intake-route"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        default_repo = github.current_repo() if args.issue.strip().startswith("#") else ""
        ref = epics.parse_issue_ref(args.issue, default_repo=default_repo)
        action = _action(args, item_repo=f"{ref.owner}/{ref.repo}")
        result = intake_route.route(ref, action)
    except ValueError as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    except github.NoInstallationError as exc:
        print(f"{_PROG}: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    if result.final == ref:
        print(f"Routed {ref.slug}: {args.route}.")
    else:
        print(f"Routed {ref.slug}: {args.route}, now {result.final.slug} (by {result.moved_by}).")
    return 0


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
        return intake_route.ToPlanning()
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

    routes.add_parser(
        "planning", help="Reparent the item onto the planning epic (intake or deferred item)"
    )

    split = routes.add_parser("split", help="Close the item, linking the tasks it was split into")
    split.add_argument(
        "--into",
        action="append",
        required=True,
        help="A task the item was split into (repeatable; #N reads against the item's repo)",
    )
    split.add_argument("--reason", required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
