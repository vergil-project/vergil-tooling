"""Patch mkdocs.yml nav with release version entries."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.docs import patch_nav
from vergil_tooling.lib.output import emit_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-docs-patch-nav",
        description="Patch mkdocs.yml nav with release version entries.",
    )
    parser.add_argument(
        "--mkdocs-yml",
        type=Path,
        required=True,
        help="Path to mkdocs.yml",
    )
    parser.add_argument(
        "--releases-dir",
        type=Path,
        required=True,
        help="Docs releases directory to scan",
    )
    args = parser.parse_args(argv)

    if not args.mkdocs_yml.is_file():
        emit_error(f"mkdocs.yml not found: {args.mkdocs_yml}")
        return 1

    if not args.releases_dir.is_dir():
        emit_error(f"releases directory not found: {args.releases_dir}")
        return 1

    patch_nav(args.mkdocs_yml, args.releases_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
