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
    rc = progress.run_pipeline(
        state,
        build_stages(),
        command="vrg-release",
        label="vrg-release",
        args=args,
        repo_root=repo_root,
    )
    # The progress renderer collapses each finished stage to a one-line
    # summary, which erases the consumer-refresh commands — the one piece
    # of output the human must act on. Re-print them below the summary.
    if state.ctx is not None and state.ctx.consumer_refresh_message:
        print()
        print(state.ctx.consumer_refresh_message)
    return rc


if __name__ == "__main__":
    sys.exit(main())
