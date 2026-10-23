"""Deprecated alias for ``vrg-intake-create`` (kept for one release).

Warns on stderr, strips ``--kind <v>`` / ``--kind=<v>`` (intake captures carry
no kind label any more), and forwards every other argument — ``--help``
included — to :func:`vrg_intake_create.main` (epic vergil-project/.github#176).
"""

from __future__ import annotations

import sys

from vergil_tooling.bin import vrg_intake_create
from vergil_tooling.lib.cli import cli_main


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    print("vrg-triage-create is deprecated; use vrg-intake-create.", file=sys.stderr)
    rest = _strip_kind(args)
    if len(rest) != len(args):
        print("vrg-triage-create: --kind is ignored (intake has no kind label).", file=sys.stderr)
    return vrg_intake_create.main(rest)


def _strip_kind(args: list[str]) -> list[str]:
    rest: list[str] = []
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg == "--kind":
            skip = True
        elif not arg.startswith("--kind="):
            rest.append(arg)
    return rest


if __name__ == "__main__":
    sys.exit(main())
