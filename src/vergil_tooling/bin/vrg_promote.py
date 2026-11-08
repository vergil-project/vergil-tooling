"""CLI entry point for vrg-promote.

Moves the rolling ``vX.Y`` tag to an existing release ``vX.Y.Z``. It is a
human maintainer action: an agent identity is refused before any output (a
guard against an agent running it by mistake; the GitHub rolling-tag ruleset
is what enforces who can move the tag). The
command prints the origin repo and the tag move, and asks for confirmation on
the terminal before it pushes; with no terminal to ask on, it refuses.
``--yes`` moves the tag with no prompt, so it needs no terminal.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import github, identity_mode
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.promote import describe, plan_promotion, promote_confirmed
from vergil_tooling.lib.version import show

if TYPE_CHECKING:
    from vergil_tooling.lib.promote import Promotion


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
            "asks for confirmation before it pushes, unless --yes is given."
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
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Move the tag without asking for confirmation, so no terminal is needed.",
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
        except Exception as exc:
            print(f"Error: cannot read the VERSION file: {error_text(exc)}", file=sys.stderr)
            return 1

    try:
        p = plan_promotion(version)
        if p.old_commit == p.new_commit:
            print(f"{p.rolling_tag} already points at {p.release_tag} — already promoted.")
            return 0
        if args.dry_run or args.yes:
            print(describe(p))
            if args.dry_run:
                return 0
        elif not _can_prompt():
            print(describe(p))
            print(
                f"vrg-promote: refusing to move {p.rolling_tag} without a terminal to "
                "confirm on; pass --yes to move it without a prompt.",
                file=sys.stderr,
            )
            return 1
        elif not _confirmed(p):
            print(f"vrg-promote: {p.rolling_tag} not moved.", file=sys.stderr)
            return 1
        promote_confirmed(p)
    except (github.AppAuthError, github.NoInstallationError, github.MissingGitHubTokenError):
        raise  # cli_main prints the guided remediation message
    except Exception as exc:
        print(f"Error: {error_text(exc)}", file=sys.stderr)
        return 1
    return 0


def _can_prompt() -> bool:
    """Whether there is a terminal to confirm on.

    The answer is read from stdin and the move and the prompt are written to
    stderr, so both must be terminals.
    """
    return sys.stdin.isatty() and sys.stderr.isatty()


def _confirmed(p: Promotion) -> bool:
    """Show the move and ask for confirmation on the terminal.

    The move and the prompt go to stderr, not stdout, so they stay visible when
    stdout is redirected.
    """
    print(describe(p), file=sys.stderr)
    print(f"Move {p.rolling_tag} on {p.origin_repo}? [y/N] ", end="", file=sys.stderr, flush=True)
    try:
        answer = input()
    except (EOFError, KeyboardInterrupt):
        print(file=sys.stderr)
        answer = ""
    return answer.strip().lower() in ("y", "yes")


if __name__ == "__main__":
    sys.exit(main())
