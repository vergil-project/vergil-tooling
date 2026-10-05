"""vrg-package — build, test and index binary OS packages (epic vergil-project/.github#356).

Reads the ``[package]`` section of ``vergil.toml`` in the current directory.

Subcommands:

  matrix  Resolve [package] into explicit build and test cells and print them
          as JSON (read-only). With --github-output, append ``enabled``,
          ``build`` and ``test`` to $GITHUB_OUTPUT instead of printing. With
          --manifest PATH, also write the release artifact manifest to PATH.
          A repo without [package] reports ``enabled: false`` and empty lists.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vergil_tooling.lib import config
from vergil_tooling.lib.package import PackageError, matrix


def _cmd_matrix(args: argparse.Namespace) -> int:
    cfg = config.read_config(Path.cwd())
    m = matrix.resolve(cfg.package) if cfg.package is not None else None
    payload: dict[str, object] = (
        {"enabled": True, **matrix.to_json(m)}
        if m is not None
        else {"enabled": False, "build": [], "test": []}
    )
    output_path = os.environ.get("GITHUB_OUTPUT", "")
    if args.github_output and not output_path:
        msg = "--github-output: GITHUB_OUTPUT is not set"
        raise PackageError(msg)
    if args.manifest:
        if m is None:
            print(
                f"vrg-package: no [package] section; manifest not written to {args.manifest}",
                file=sys.stderr,
            )
        else:
            Path(args.manifest).write_text(
                json.dumps(matrix.manifest(m), indent=2) + "\n", encoding="utf-8"
            )
    if args.github_output:
        with Path(output_path).open("a", encoding="utf-8") as fh:
            fh.write(f"enabled={'true' if m is not None else 'false'}\n")
            fh.write(f"build={json.dumps(payload['build'])}\n")
            fh.write(f"test={json.dumps(payload['test'])}\n")
    else:
        print(json.dumps(payload, indent=2))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-package",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser(
        "matrix",
        help="Resolve [package] into build/test cells (JSON; read-only)",
        description="Resolve [package] into explicit build and test cells (spec §5.3).",
    )
    p.add_argument(
        "--github-output",
        action="store_true",
        help="Append enabled/build/test to $GITHUB_OUTPUT instead of printing",
    )
    p.add_argument(
        "--manifest",
        default="",
        metavar="PATH",
        help="Also write the release artifact manifest to PATH",
    )
    p.set_defaults(func=_cmd_matrix)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return int(args.func(args))
    except (config.ConfigError, PackageError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
