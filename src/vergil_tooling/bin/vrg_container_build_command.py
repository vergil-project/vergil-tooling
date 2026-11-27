"""Print a repo's declared [container].build-command, for CI consumption.

Default: the build-command verbatim. --script: the shell step CI runs, the
command after the noninteractive apt export (the single speller shared with the
local cache build, #5225). Prints nothing when the repo declares no
build-command.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.config import container_build_command
from vergil_tooling.lib.container_cache import build_command_script


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print declared [container].build-command.")
    parser.add_argument(
        "--script",
        action="store_true",
        help="print the shell step CI runs: the command after the noninteractive apt export",
    )
    parser.add_argument("--repo-root", default=".", help="repo root (default: CWD)")
    args = parser.parse_args(argv)

    command = container_build_command(Path(args.repo_root))
    if command:
        print(build_command_script(command) if args.script else command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
