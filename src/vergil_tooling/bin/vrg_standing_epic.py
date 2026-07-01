"""Ensure a repo's standing epic exists (create-if-missing), idempotently.

Standing epics are per-repo: the ``Epic (standing): Ad-hoc maintenance`` umbrella
(labelled ``epic`` + ``standing``) in each member repo and in ``.github``. This
provisions it before linking pre-existing issues — e.g. ``migrate-repo`` step 1,
which must ensure the epic exists before it can link standing tasks to it.
Routing work to ``standing`` (``vrg-issue-create``/``vrg-epic-move --epic
standing``) also ensures it via the same path.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import epics, github


def cmd_ensure(args: argparse.Namespace) -> int:
    repo = args.repo or github.current_repo()
    epic = epics.ensure_standing_epic(repo)
    print(f"Standing epic: {epic.slug}")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="vrg-standing-epic",
        description=(
            "Manage a repo's standing epic (Epic (standing): Ad-hoc "
            "maintenance, labelled epic + standing)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_ensure = sub.add_parser(
        "ensure",
        help="Ensure the repo's standing epic exists (create-if-missing, idempotent).",
    )
    p_ensure.add_argument("--repo", help="Target repo owner/name (defaults to the current repo)")
    p_ensure.set_defaults(func=cmd_ensure)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
