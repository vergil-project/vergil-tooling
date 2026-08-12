"""Print a repo's declared [container].build-command, for CI consumption.

Emits the build-command verbatim so CI can run it. Prints nothing when the
repo declares no build-command.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.config import container_build_command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print declared [container].build-command.")
    parser.add_argument(
        "--script",
        action="store_true",
        help="print the command verbatim for CI (identical output; explicit CI intent)",
    )
    parser.add_argument("--repo-root", default=".", help="repo root (default: CWD)")
    args = parser.parse_args(argv)

    command = container_build_command(Path(args.repo_root))
    if command:
        print(command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
