"""CLI entry point for st-version."""

from __future__ import annotations

import argparse
from pathlib import Path

from standard_tooling.lib.version import bump, show, show_major_minor


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="st-version",
        description="Version management for standard-tooling repos",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    show_parser = sub.add_parser("show", help="Print current version")
    show_parser.add_argument(
        "--major-minor",
        action="store_true",
        help="Print major.minor only",
    )
    show_parser.add_argument(
        "--ref",
        default=None,
        help="Read version from a git ref (e.g., origin/main) via git show",
    )

    sub.add_parser("bump", help="Increment patch version")

    args = parser.parse_args()
    repo_root = Path.cwd()

    if args.command == "show":
        if args.major_minor:
            print(show_major_minor(repo_root, ref=args.ref))  # noqa: T201
        else:
            print(show(repo_root, ref=args.ref))  # noqa: T201
    else:
        new_version = bump(repo_root)
        print(new_version)  # noqa: T201


if __name__ == "__main__":
    main()
