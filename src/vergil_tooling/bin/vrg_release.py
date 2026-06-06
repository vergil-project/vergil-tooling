"""Mechanized release workflow — human-invoked, fully automated."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, progress
from vergil_tooling.lib.release.orchestrator import ReleaseState, build_stages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the full release workflow from develop to main.",
    )
    parser.add_argument(
        "version_override",
        nargs="?",
        choices=("minor", "major"),
        default=None,
        help="Bump to next minor or major before releasing (default: release current version).",
    )
    parser.add_argument(
        "--no-promote",
        action="store_true",
        default=False,
        help="Skip rolling-tag promotion after release.",
    )
    progress.add_progress_args(parser, build_stages())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = git.repo_root()
    state = ReleaseState(
        version_override=args.version_override,
        repo_root=repo_root,
        promote=not args.no_promote,
    )
    return progress.run_pipeline(
        state,
        build_stages(),
        command="vrg-release",
        label="vrg-release",
        args=args,
        repo_root=repo_root,
    )


if __name__ == "__main__":
    sys.exit(main())
