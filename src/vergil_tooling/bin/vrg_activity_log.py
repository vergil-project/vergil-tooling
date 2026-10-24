"""Print the project activity log (recently closed work across the org).

A pure read-and-render command — see :mod:`vergil_tooling.lib.activity_log`. The
nightly job that writes/publishes the rendered markdown is separate (T8c).

By default it prints the flat org ledger to stdout; ``--repo`` narrows that to
one repo. ``--out-dir`` instead writes the multi-page form for a docs site: an
``index.md`` day x repo summary table, ``days/<date>.md`` detail pages and
``repos/<name>.md`` per-repo ledgers.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from vergil_tooling.lib import activity_log, github
from vergil_tooling.lib.cli import cli_main

_WINDOW_DAYS = 30


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the project activity log.")
    parser.add_argument(
        "--org",
        help="GitHub org whose closed issues to list (default: current repo's owner).",
    )
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--repo",
        help="Only list this repo's closed issues (a name in --org, or owner/name).",
    )
    target.add_argument(
        "--out-dir",
        type=Path,
        help=(
            "Write the multi-page log here instead of printing it: index.md (day x repo "
            "summary), days/<date>.md and repos/<name>.md."
        ),
    )
    args = parser.parse_args(argv)
    org = args.org or github.current_org()
    since = (datetime.now(UTC) - timedelta(days=_WINDOW_DAYS)).date().isoformat()
    # Scope the App token to the org being listed so a cross-org --org selects
    # that org's installation, not the cwd repo's (#2070).
    try:
        with github.target_org(org):
            items = activity_log.gather(since, org=org, repo=args.repo)
    except github.NoInstallationError as exc:
        print(f"vrg-activity-log: {github.no_installation_message(exc)}", file=sys.stderr)
        return 1
    if args.out_dir is not None:
        pages = activity_log.render_pages(items)
        _write_pages(args.out_dir, pages)
        print(f"vrg-activity-log: wrote {len(pages)} page(s) to {args.out_dir}")
    elif args.repo is not None:
        name = args.repo.split("/", 1)[-1]
        print(activity_log.render(items, title=f"Activity log: {name}"))
    else:
        print(activity_log.render(items))
    return 0


def _write_pages(out_dir: Path, pages: dict[str, str]) -> None:
    """Write *pages* under *out_dir*, first clearing the pages a prior run left.

    The day and repo subdirectories belong to this command: a day that has
    rolled out of the window must not linger as a stale page.
    """
    for sub in (activity_log.DAYS_DIR, activity_log.REPOS_DIR):
        for stale in (out_dir / sub).glob("*.md"):
            stale.unlink()
    for rel, text in pages.items():
        path = out_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
