"""Compare head and main branch versions for release divergence."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.output import emit_error, write_output, write_summary
from vergil_tooling.lib.version_divergence import (
    DivergenceStatus,
    compare_versions,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-version-divergence",
        description="Compare head and main branch versions.",
    )
    parser.add_argument("head_version", help="Version string from head branch")
    parser.add_argument(
        "main_version",
        nargs="?",
        default=None,
        help="Version string from main branch (omit for first release)",
    )
    args = parser.parse_args(argv)

    if not args.head_version:
        emit_error("head version must not be empty")
        return 2

    result = compare_versions(args.head_version, args.main_version)

    write_output("status", result.status.value)
    write_output("head_version", result.head_version)
    write_output("main_version", result.main_version)

    if result.status == DivergenceStatus.EQUAL:
        msg = f"version not bumped: head ({result.head_version}) == main ({result.main_version})"
        emit_error(msg)
        write_summary(f"## Version Divergence Failed\n\n{msg}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
