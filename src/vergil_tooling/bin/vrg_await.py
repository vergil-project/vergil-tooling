"""Block until a ``.vergil/`` channel file appears or changes.

``vrg-await <path> [--since <sha256>]`` is the wait primitive behind the
implement↔audit handshake (§6 of the Vergil 2.1 workflow design). Without
``--since`` it blocks until ``<path>`` exists; with ``--since`` it blocks until
the file's SHA-256 differs from the given digest. On return it prints the
current SHA-256 so the caller can thread it back as ``--since`` on the next
round.

The tool blocks patiently and indefinitely — that is the feature. A wait that
never returns means the counterpart agent has not (yet) produced its artifact.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib import await_file


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Block until a file appears or its content changes.",
    )
    parser.add_argument("path", help="File to wait on")
    parser.add_argument(
        "--since",
        help="Previously seen SHA-256; wait until the file's digest differs from it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    digest = await_file.wait_for_file(Path(args.path), since=args.since)
    print(digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
