"""CLI entry point for vrg-promote.

Moves the rolling ``vX.Y`` tag to an existing release ``vX.Y.Z``. It is a
human maintainer action: an agent identity is refused before any output (a
guard against an agent running it by mistake; the GitHub rolling-tag ruleset
is what enforces who can move the tag). The
command prints the origin repo and the tag move, and asks for confirmation on
the terminal before it pushes; with no terminal to ask on, it refuses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib import github, identity_mode
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.promote import describe, plan_promotion, promote_confirmed
from vergil_tooling.lib.version import show


@cli_main
def main() -> int:
    if identity_mode.is_agent() or identity_mode.any_agent_signal():
        print(
            "vrg-promote: moving a rolling tag is a human maintainer action. "
            "Agents cannot run vrg-promote.",
            file=sys.stderr,
        )
        return 1

    parser = argparse.ArgumentParser(
        prog="vrg-promote",
        description=(
            "Move the vX.Y rolling tag to an existing release vX.Y.Z whose commit is "
            "on origin/main and that has a published GitHub Release. Human-only; "
            "asks for confirmation before it pushes."
        ),
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
        help="Check the target and print the tag move without executing it.",
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
        p = plan_promotion(version)
        if p.old_commit == p.new_commit:
            print(f"{p.rolling_tag} already points at {p.release_tag} — already promoted.")
            return 0
        print(describe(p))
        if args.dry_run:
            return 0
        if not sys.stdin.isatty():
            print(
                f"vrg-promote: refusing to move {p.rolling_tag} without a terminal to confirm on.",
                file=sys.stderr,
            )
            return 1
        try:
            answer = input(f"Move {p.rolling_tag} on {p.origin_repo}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print(f"vrg-promote: {p.rolling_tag} not moved.", file=sys.stderr)
            return 1
        promote_confirmed(p)
    except (github.AppAuthError, github.NoInstallationError, github.MissingGitHubTokenError):
        raise  # cli_main prints the guided remediation message
    except Exception as exc:
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
