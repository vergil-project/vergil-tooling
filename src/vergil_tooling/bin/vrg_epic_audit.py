"""Print the epic/task drift audit (read-only safety net).

See :mod:`vergil_tooling.lib.epic_audit`. Surfaces work that slipped through
auto-close so a human can close it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from vergil_tooling.lib import epic_audit, github

_DEFAULT_WINDOW_DAYS = 30


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        msg = "must be a positive integer"
        raise argparse.ArgumentTypeError(msg)
    return value


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-epic-audit",
        description=(
            "Report epic/task drift for the current repo's GitHub org: merged "
            "PRs whose Ref'd task issue is still open, and open non-standing "
            "epics whose children are all closed. Read-only — it changes "
            "nothing; a human acts on whatever it lists."
        ),
        epilog=(
            "Scope: the org is auto-detected from this repo's 'origin' remote, "
            "so run it from inside a repo in the org you want to audit (there is "
            "no --org flag). Output is Markdown on stdout."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=_positive_int,
        default=_DEFAULT_WINDOW_DAYS,
        metavar="N",
        help=(f"How many days back to scan for merged PRs (default: {_DEFAULT_WINDOW_DAYS})."),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    org = github.detect_org()
    if org is None:
        print(
            "vrg-epic-audit: could not determine the GitHub org from this "
            "repo's 'origin' remote; run it from inside a repo in the org you "
            "want to audit.",
            file=sys.stderr,
        )
        return 1
    since = (datetime.now(UTC) - timedelta(days=args.window_days)).date().isoformat()
    print(
        epic_audit.render(
            epic_audit.task_drift(since, org=org),
            epic_audit.epic_drift(),
            org=org,
            window_days=args.window_days,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
