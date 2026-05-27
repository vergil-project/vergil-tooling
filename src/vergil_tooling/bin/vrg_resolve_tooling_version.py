"""Resolve the vergil-tooling install tag from vergil.toml."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.config import ConfigError, vrg_install_tag
from vergil_tooling.lib.output import emit_error, write_output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-resolve-tooling-version",
        description="Resolve vergil-tooling install tag from vergil.toml.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root containing vergil.toml (default: cwd)",
    )
    args = parser.parse_args(argv)

    try:
        tag = vrg_install_tag(args.repo_root)
    except (FileNotFoundError, ConfigError) as exc:
        emit_error(str(exc))
        return 1

    write_output("vergil_version", tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
