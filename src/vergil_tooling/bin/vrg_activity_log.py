"""Print the project activity log (recently closed work across the org).

A pure read-and-render command — see :mod:`vergil_tooling.lib.activity_log`. The
nightly job that writes/publishes the rendered markdown is separate (T8c).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from vergil_tooling.lib import activity_log, github

_WINDOW_DAYS = 30


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the project activity log.")
    parser.add_argument(
        "--org",
        help="GitHub org whose closed issues to list (default: current repo's owner).",
    )
    args = parser.parse_args(argv)
    org = args.org or github.current_org()
    since = (datetime.now(UTC) - timedelta(days=_WINDOW_DAYS)).date().isoformat()
    print(activity_log.render(activity_log.gather(since, org=org)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
