"""Read and change an epic's release groups: ``vrg-release-group list``, ``set`` and ``dissolve``.

A release group is a batch of tasks of one development epic (or a proxy of one) that ship
together (epic vergil-project/.github#1672). Its membership is a record in the epic's comments,
read by :mod:`~vergil_tooling.lib.release_groups.record`. ``list`` reads it and writes nothing.
``set`` and ``dissolve`` post the next revision of a group's record, through
:mod:`~vergil_tooling.lib.release_groups.write`.

``list --epic <owner>/<repo>#<N> [--group <id>] [--json]`` prints one line per group,
``G1 rev 3: <ref> <ref>`` or ``G2 rev 2: dissolved``, or with ``--json`` one object,
``{"epic", "groups": [{"group", "rev", "members", "dissolved"}]}``, groups in ID order.

``set --epic <ref> --group <id> --member <ref> [--member <ref> ...] --reason <text>
[--at-batch-stop]`` posts the group's membership, a new group at rev 1 and an existing one at its
latest revision plus one. ``dissolve --epic <ref> --group <id> --reason <text>
[--at-batch-stop]`` posts the group dissolved. Each prints ``G1 rev 3: posted``, or
``G1 rev 3: already recorded`` when the latest record already holds the request, and posts
nothing then (a retry is safe, and needs no reason). ``--at-batch-stop`` lets a group whose batch
has started change for the human, and lets a task that is only in that batch join it. A change
runs only from the main clone of the members' repo, since batch markers are local.

Exits 0 on a read, a post or an already-recorded request. Exits 1 on a refusal or a failed read
or write (stderr names each problem, and nothing is printed on stdout); a refusal of ``list``
prints nothing on stdout either. Exits 2 on a usage error. A refusal of one group refuses only a
read of that group, so ``list --group`` reads past a broken group that is not the one asked for;
a listing of every group refuses when any group is broken, and a change refuses while any group
of its epic is broken.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import TYPE_CHECKING

from vergil_tooling.lib import epics, git, github
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.release_groups import record, write

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_PROG = "vrg-release-group"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "list":
        return _list(args)
    if args.command == "set":
        return _change(
            args.group,
            lambda root: write.set_group(
                args.epic,
                args.group,
                args.member,
                reason=args.reason,
                at_batch_stop=args.at_batch_stop,
                main_root=root,
            ),
        )
    return _change(
        args.group,
        lambda root: write.dissolve_group(
            args.epic,
            args.group,
            reason=args.reason,
            at_batch_stop=args.at_batch_stop,
            main_root=root,
        ),
    )


def _list(args: argparse.Namespace) -> int:
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


def _change(group: str, run: Callable[[Path], write.WriteResult]) -> int:
    """Run one change from the main clone, print its outcome; exit 1 on any refusal."""
    try:
        result = run(_main_clone())
    except write.WriteRefusedError as exc:
        for message in exc.messages:
            print(f"{_PROG}: {message}", file=sys.stderr)
        return 1
    outcome = "posted" if result.posted else "already recorded"
    print(f"{group} rev {result.rev}: {outcome}")
    return 0


def _main_clone() -> Path:
    """The main clone this command runs from; a worktree or an unknown place is refused."""
    try:
        if not git.is_main_worktree():
            raise write.WriteRefusedError(
                [
                    "run this from the main clone of the members' repo, not a worktree:"
                    " batch markers are read from the main clone"
                ]
            )
        return git.main_worktree_root()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise write.WriteRefusedError(
            [f"cannot tell whether this is the main clone ({error_text(exc)})"]
        ) from exc


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


def _member_ref(text: str) -> epics.IssueRef:
    """A member, qualified as ``owner/repo#N``: a bare ``#N`` names no repo here."""
    try:
        return epics.parse_issue_ref(text, default_repo="")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"a member is owner/repo#N: {exc}") from exc


def _group_id(text: str) -> str:
    if record.GROUP_ID_RE.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(f"not a group ID (G1, G2, ...): {text!r}")
    return text


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line; a usage error exits 2 through argparse."""
    parser = argparse.ArgumentParser(
        prog=_PROG, description="Read and change an epic's release groups."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="List an epic's release groups (read-only)")
    listing.add_argument("--epic", required=True, type=_epic_ref, help="The epic: owner/repo#N")
    listing.add_argument("--group", type=_group_id, help="Only this group, e.g. G1")
    listing.add_argument(
        "--json",
        action="store_true",
        help='Print {"epic", "groups"} instead of one line per group',
    )
    setter = commands.add_parser("set", help="Post a group's membership as its next revision")
    setter.add_argument("--epic", required=True, type=_epic_ref, help="The epic: owner/repo#N")
    setter.add_argument("--group", required=True, type=_group_id, help="The group, e.g. G1")
    setter.add_argument(
        "--member",
        required=True,
        action="append",
        type=_member_ref,
        help="A member, owner/repo#N; repeat for each member",
    )
    _change_options(setter)
    dissolver = commands.add_parser("dissolve", help="Post a group as dissolved")
    dissolver.add_argument("--epic", required=True, type=_epic_ref, help="The epic: owner/repo#N")
    dissolver.add_argument("--group", required=True, type=_group_id, help="The group, e.g. G1")
    _change_options(dissolver)
    return parser.parse_args(argv)


def _change_options(parser: argparse.ArgumentParser) -> None:
    """The options a change shares: its reason, and ``--at-batch-stop``."""
    parser.add_argument(
        "--reason",
        help="Why the group changes, one line (needed to post a change, not a retry)",
    )
    parser.add_argument(
        "--at-batch-stop",
        action="store_true",
        help="Change a started group, or join a task of its batch, at the batch stop",
    )


if __name__ == "__main__":
    sys.exit(main())
