"""Run the repo's ``[upstream-refresh].hook`` on a fresh clone of ``develop``.

Language-neutral (epic vergil-project/.github#472, spec §5.6): the repo's hook
refreshes its upstream inputs by editing tracked files. The command is being
rebuilt on the mechanized-PR building block (epic vergil-project/.github#1168):
its prepare half is in place, and ``--dry-run`` runs it and prints the PR body
the refresh would open. The publish half, which applies the patch, checks the
path fence and opens the PR, arrives with that epic's Task 3b; until then a
run without ``--dry-run`` is refused. It never touches the caller's checkout.
Agent identity modes are refused.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib import github, identity_mode
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import ConfigError
from vergil_tooling.lib.develop_clone import DevelopCloneError
from vergil_tooling.lib.process_errors import error_text
from vergil_tooling.lib.upstream_refresh import UpstreamRefreshError, prepare, render_body

_PROG = "vrg-upstream-refresh"


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if identity_mode.is_agent():
        print(f"{_PROG} opens PRs and is not run by agents", file=sys.stderr)
        return 1
    if not args.dry_run:
        print(
            f"{_PROG}: publishing is not available in this build (it arrives with epic "
            "vergil-project/.github#1168, Task 3b); run with --dry-run",
            file=sys.stderr,
        )
        return 1
    try:
        return _dry_run(args)
    except (UpstreamRefreshError, ConfigError, DevelopCloneError) as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"{_PROG}: command failed: {error_text(exc)}", file=sys.stderr)
        return 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            "Run the repo's [upstream-refresh].hook in a fresh clone of develop and "
            "print the refresh PR it would open. Never touches the checkout."
        ),
    )
    parser.add_argument(
        "--repo",
        metavar="OWNER/NAME",
        help="Target repository (default: $GITHUB_REPOSITORY, then the origin remote).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the hook and print the PR body; push nothing and change no PR.",
    )
    return parser.parse_args(argv)


def _resolve_repo(arg: str | None) -> str:
    return arg or os.environ.get("GITHUB_REPOSITORY") or github.current_repo()


def _dry_run(args: argparse.Namespace) -> int:
    repo = _resolve_repo(args.repo)
    with tempfile.TemporaryDirectory(prefix="vrg-upstream-refresh-out-") as out:
        prepared = prepare(repo, Path(out))
    if prepared.paths:
        print(f"Dry run: would open the refresh PR on {repo} (develop):\n")
        print(render_body(prepared.notes, prepared.paths))
    else:
        print("Dry run: nothing to refresh on develop.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
