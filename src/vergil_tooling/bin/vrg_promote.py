"""CLI entry point for vrg-promote."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.promote import promote
from vergil_tooling.lib.version import show


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-promote",
        description="Force-update the vX.Y rolling tag to track vX.Y.Z",
    )
    parser.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Version to promote (e.g., v2.0.34). Default: current version from VERSION file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without executing.",
    )
    args = parser.parse_args()

    version = args.version
    if version is None:
        try:
            version = show(Path.cwd())
        except FileNotFoundError:
            print(
                "No version specified and no VERSION file found.",
                file=sys.stderr,
            )
            return 1

    try:
        promote(version, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
