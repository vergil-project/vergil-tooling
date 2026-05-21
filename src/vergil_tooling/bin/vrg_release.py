"""Mechanized release workflow — human-invoked, fully automated."""

from __future__ import annotations

import argparse
import sys
import time

from vergil_tooling.lib import git
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.orchestrator import _format_elapsed, run_release
from vergil_tooling.lib.release.preflight import preflight


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
        "-v",
        "--verbose",
        action="store_true",
        default=False,
        help="Show full subprocess output (default: summarized).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = git.repo_root()

    try:
        print("\n=== Phase: preflight ===")
        start = time.monotonic()
        ctx = preflight(
            version_override=args.version_override,
            repo_root=repo_root,
            verbose=args.verbose,
        )
        elapsed = time.monotonic() - start
        print(f"=== preflight: done ({_format_elapsed(elapsed)}) ===")
        run_release(ctx)
    except ReleaseError as exc:
        print(f"\nRelease failed in phase '{exc.phase}'.", file=sys.stderr)
        print(f"Command: {exc.command}", file=sys.stderr)
        print(f"Error: {exc}", file=sys.stderr)
        if exc.detail:
            print(f"Detail: {exc.detail}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nUnexpected error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
