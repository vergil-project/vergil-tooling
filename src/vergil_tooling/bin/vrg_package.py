"""vrg-package — build, test and index binary OS packages (epic vergil-project/.github#356).

Reads the ``[package]`` section of ``vergil.toml`` in the current directory.

Subcommands:

  matrix  Resolve [package] into explicit build and test cells and print them
          as JSON (read-only). With --github-output, append ``enabled``,
          ``build`` and ``test`` to $GITHUB_OUTPUT instead of printing. With
          --manifest PATH, also write the release artifact manifest to PATH.
          A repo without [package] reports ``enabled: false`` and empty lists.

  build   Build every package format of one build cell (--cell, from the
          matrix) at --version (overridden by [package].version when set).
          Stages into --staging (wiped first) and writes the .deb/.rpm
          artifacts to --out, printing each artifact path. Needs nfpm on PATH.

  indexBuild the signed apt/dnf package-repository site (spec §7.2): collect
          and verify the stable releases of every product in --config, apply
          retention, write and sign the metadata, and size-check the result in
          --out. Needs PACKAGE_SIGNING_KEY and PACKAGE_SIGNING_PASSPHRASE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vergil_tooling.lib import config
from vergil_tooling.lib.package import PackageError, build, matrix
from vergil_tooling.lib.package import staged as _staged  # noqa: F401  (registers the builder)
from vergil_tooling.lib.package.index import site


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


def _cmd_build(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    artifacts = build.run_build(
        repo_root=cwd,
        cell_id=args.cell,
        version=args.version,
        out_dir=cwd / args.out,
        staging_root=cwd / args.staging,
    )
    for path in artifacts:
        print(path)
    return 0


def _cmd_index(args: argparse.Namespace) -> int:
    site.build_site(
        Path(args.config),
        Path(args.keys),
        Path(args.out),
        Path(args.work),
        base_url=args.base_url or site.default_base_url(),
    )
    print(f"vrg-package: package site written to {args.out}")
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
    b = sub.add_parser(
        "build",
        help="Build one build cell's .deb/.rpm packages (needs nfpm)",
        description="Build every package format of one build cell (spec §6).",
    )
    b.add_argument("--cell", required=True, help="Build cell id from `vrg-package matrix`")
    b.add_argument("--version", required=True, help="Repo version (e.g. 2.1.240)")
    b.add_argument(
        "--out",
        default="dist/packages",
        metavar="DIR",
        help="Artifact directory (default: dist/packages)",
    )
    b.add_argument(
        "--staging",
        default=".vergil/package-staging",
        metavar="DIR",
        help="Staging root, wiped before the build (default: .vergil/package-staging)",
    )
    b.set_defaults(func=_cmd_build)
    p = sub.add_parser(
        "index",
        help="Build the signed apt/dnf package-repository site",
        description="Collect, verify, retain, index, sign and size-check (spec §7.2).",
    )
    p.add_argument("--config", required=True, metavar="PATH", help="packages.toml")
    p.add_argument("--keys", required=True, metavar="DIR", help="Directory holding <vendor>.asc")
    p.add_argument("--out", required=True, metavar="DIR", help="Site output (wiped first)")
    p.add_argument(
        "--work",
        default=".vergil/index-work",
        metavar="DIR",
        help="Download and scratch directory (default: .vergil/index-work)",
    )
    p.add_argument(
        "--base-url",
        default="",
        metavar="URL",
        help="Public URL of the site (default: https://<owner>.github.io/<repo> "
        "from GITHUB_REPOSITORY)",
    )
    p.set_defaults(func=_cmd_index)
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
