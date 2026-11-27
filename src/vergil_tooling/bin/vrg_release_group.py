"""Read an epic's release groups: ``vrg-release-group list``.

A release group is a batch of tasks of one development epic (or a proxy of one) that ship
together (epic vergil-project/.github#1672). Its membership is a record in the epic's
comments, read by :mod:`~vergil_tooling.lib.release_groups.record`. This command is the
only read path outside that package; it writes nothing.

``list --epic <owner>/<repo>#<N> [--group <id>] [--json]`` prints one line per group,
``G1 rev 3: <ref> <ref>`` or ``G2 rev 2: dissolved``, or with ``--json`` one object,
``{"epic", "groups": [{"group", "rev", "members", "dissolved"}]}``, groups in ID order.

Exits 0 on a read, 1 on a refusal or a failed read (stderr names it, nothing is printed on
stdout), 2 on a usage error. A refusal of one group refuses only a read of that group, so
``--group`` reads past a broken group that is not the one asked for; a listing of every
group refuses when any group is broken.
"""

from __future__ import annotations

import argparse
import json
import sys

from vergil_tooling.lib import epics, github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.release_groups import record

_PROG = "vrg-release-group"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        log = record.read_log(args.epic)
    except (github.GitHubAPIError, RuntimeError, ValueError) as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    refusals = _refusals(log, args.group)
    if refusals:
        for message in refusals:
            print(f"{_PROG}: {message}", file=sys.stderr)
        return 1
    groups = [g for g in record.in_id_order(log.groups.values()) if _selected(g, args.group)]
    if args.json:
        print(json.dumps(_as_json(log.epic, groups)))
    else:
        for group in groups:
            print(_as_line(group))
    return 0


def _refusals(log: record.GroupLog, group: str | None) -> list[str]:
    """The messages that refuse this read: the epic's, and each broken group's it covers."""
    if log.epic_error is not None:
        return [str(log.epic_error)]
    return [str(error) for gid, error in sorted(log.broken.items()) if group in (None, gid)]


def _selected(group: record.Group, wanted: str | None) -> bool:
    return wanted is None or group.group == wanted


def _as_json(epic: epics.IssueRef, groups: list[record.Group]) -> dict[str, object]:
    return {
        "epic": epic.slug,
        "groups": [
            {
                "group": g.group,
                "rev": g.rev,
                "members": [m.slug for m in g.members],
                "dissolved": g.dissolved,
            }
            for g in groups
        ],
    }


def _as_line(group: record.Group) -> str:
    if group.dissolved:
        return f"{group.group} rev {group.rev}: dissolved"
    return f"{group.group} rev {group.rev}: " + " ".join(m.slug for m in group.members)


def _epic_ref(text: str) -> epics.IssueRef:
    try:
        return epics.parse_issue_ref(text, default_repo="")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _group_id(text: str) -> str:
    if record.GROUP_ID_RE.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(f"not a group ID (G1, G2, ...): {text!r}")
    return text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line; a usage error exits 2 through argparse."""
    parser = argparse.ArgumentParser(
        prog=_PROG, description="Read an epic's release groups (read-only)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List an epic's release groups")
    listing.add_argument(
        "--epic", required=True, type=_epic_ref, help="The epic: owner/repo#N"
    )
    listing.add_argument("--group", type=_group_id, help="Only this group, e.g. G1")
    listing.add_argument(
        "--json",
        action="store_true",
        help='Print {"epic", "groups"} instead of one line per group',
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main())
