"""CLI entry point for vrg-changelog."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.changelog import generate_changelog, generate_release_notes
from vergil_tooling.lib.version import show


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-changelog",
        description="Generate changelog and release notes via git-cliff",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--changelog-only",
        action="store_true",
        help="Generate only CHANGELOG.md",
    )
    group.add_argument(
        "--notes-only",
        action="store_true",
        help="Generate only releases/vX.Y.Z.md",
    )
    args = parser.parse_args()
    repo_root = Path.cwd()

    try:
        version = show(repo_root)
    except FileNotFoundError:
        print("Could not detect version — is there a VERSION file?", file=sys.stderr)
        return 1

    if args.notes_only:
        output = generate_release_notes(repo_root, version)
        print(f"Generated: {output}")
    elif args.changelog_only:
        generate_changelog(repo_root, version)
        print("Generated: CHANGELOG.md")
    else:
        generate_changelog(repo_root, version)
        output = generate_release_notes(repo_root, version)
        print(f"Generated: CHANGELOG.md, {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
