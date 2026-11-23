"""CLI entry point for vrg-version."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.version import (
    CargoVersionError,
    VersionSyncError,
    bump,
    show,
    show_major_minor,
)


@cli_main
def main() -> None:
    parser = argparse.ArgumentParser(
        prog="vrg-version",
        description="Version management for vergil-tooling repos",
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

    bump_parser = sub.add_parser("bump", help="Increment version")
    bump_parser.add_argument(
        "part",
        nargs="?",
        choices=("patch", "minor", "major"),
        default="patch",
        help="Version component to bump (default: patch)",
    )

    args = parser.parse_args()
    repo_root = Path.cwd()

    # The expected failures (#4484): a missing VERSION file, a missing or
    # invalid vergil.toml, a version file that disagrees with VERSION, a
    # Cargo.toml or package.json problem, and a git or lock command that
    # fails, whose own output is already on the terminal. Each is one line
    # and exit 1; anything else is a bug and keeps its traceback.
    try:
        _run(args, repo_root)
    except (CargoVersionError, ConfigError, VersionSyncError, FileNotFoundError) as exc:
        _fail(str(exc))
    except subprocess.CalledProcessError as exc:
        command = exc.cmd if isinstance(exc.cmd, str) else " ".join(map(str, exc.cmd))
        _fail(f"{command} exited {exc.returncode}")


def _run(args: argparse.Namespace, repo_root: Path) -> None:
    if args.command == "show":
        if args.major_minor:
            print(show_major_minor(repo_root, ref=args.ref))  # noqa: T201
        else:
            print(show(repo_root, ref=args.ref))  # noqa: T201
    else:
        new_version = bump(repo_root, args.part)
        print(new_version)  # noqa: T201


def _fail(message: str) -> None:
    """Print ``vrg-version: <message>`` on stderr and exit 1, with no traceback."""
    print(f"vrg-version: {message}", file=sys.stderr)  # noqa: T201
    raise SystemExit(1)


if __name__ == "__main__":
    main()
