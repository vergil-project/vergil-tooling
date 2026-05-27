"""Stage changelog and release notes into the docs build directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.docs import stage_docs
from vergil_tooling.lib.output import emit_error, write_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-docs-stage",
        description="Stage changelog and release notes into the docs build directory.",
    )
    parser.add_argument(
        "--docs-dir",
        type=Path,
        required=True,
        help="Target docs source directory",
    )
    parser.add_argument(
        "--releases-dir",
        type=Path,
        default=Path("releases"),
        help="Source releases directory",
    )
    parser.add_argument(
        "--changelog",
        type=Path,
        default=Path("CHANGELOG.md"),
        help="Source changelog file",
    )
    args = parser.parse_args(argv)

    if not args.docs_dir.is_dir():
        emit_error(f"docs directory not found: {args.docs_dir}")
        return 1

    changelog = args.changelog if args.changelog.is_file() else None
    count = stage_docs(
        docs_dir=args.docs_dir,
        releases_dir=args.releases_dir,
        changelog=changelog,
    )
    write_output("releases_staged", str(count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
