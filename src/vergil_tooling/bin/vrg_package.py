"""vrg-package — build, test and index binary OS packages (epic vergil-project/.github#356).

Reads the ``[package]`` section of ``vergil.toml`` in the current directory.

Subcommands:

  matrix  Resolve [package] into explicit build and test cells and print them
          as JSON (read-only). --tier reduced keeps every build cell but only
          one install-test cell per format (default: full). With
          --github-output, append ``enabled``, ``build``, ``test`` and
          ``tier`` to $GITHUB_OUTPUT instead of printing. With --manifest
          PATH, also write the release artifact manifest to PATH (always the
          full build output). A repo without [package] reports
          ``enabled: false`` and empty lists. A python-builder release
          resolves its vergil-python runtime here, once: every build cell
          and the manifest carry it as ``runtime`` (#3478).

  build   Build every package format of one build cell (--cell, from the
          matrix) at --version (overridden by [package].version when set).
          Stages into --staging (wiped first) and writes the .deb/.rpm
          artifacts to --out, printing each artifact path. Needs nfpm on PATH.
          --runtime takes the cell's recorded runtime; without it the build
          resolves the runtime itself and prints which it chose.

  check-runtime  Refuse (exit 1) when any .deb/.rpm in --artifacts does not
          depend on exactly the vergil-python runtime --manifest records
          (#3478). Run where every build cell's packages are together, before
          publishing. A manifest with no runtime passes.

  check-unpublished  Refuse (exit 1) when a release at --version of --repo
          (default: $GITHUB_REPOSITORY) would publish a package — same name,
          full version, arch and format — that an earlier stable release of
          the repo already published (#3309). Read-only; needs gh. vrg-release
          runs the same check in preflight; this is the CD backstop, to run
          before building. A repo without [package] passes. Any gh error
          refuses: the check never skips.

  index   Build the signed apt/dnf package-repository site (spec §7.2): collect
          and verify the stable releases of every product in --config, apply
          retention, write and sign the metadata, and size-check the result in
          --out. Needs PACKAGE_SIGNING_KEY and PACKAGE_SIGNING_PASSPHRASE.

  install-test  In a clean container for one test cell (--cell), install that
          cell's artifact from --artifacts, verify shipped systemd units, run
          the smoke command and shim checks with a sanitized environment,
          remove the package, assert no residue, and write --report (JSON).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from vergil_tooling.lib import config
from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.package import PackageError, build, matrix, python_builder
from vergil_tooling.lib.package import runtime as pkg_runtime
from vergil_tooling.lib.package import staged as _staged  # noqa: F401  (registers the builder)
from vergil_tooling.lib.package.index import collect, published, site


@cli_main
def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return int(args.func(args))
    except (config.ConfigError, PackageError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


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
        help="Append enabled/build/test/tier to $GITHUB_OUTPUT instead of printing",
    )
    p.add_argument(
        "--tier",
        choices=[t.value for t in matrix.Tier],
        default=matrix.Tier.FULL.value,
        help=(
            "full (default): every build and test cell. reduced: every build "
            "cell, one install-test cell per format"
        ),
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
    b.add_argument(
        "--runtime",
        default="",
        metavar="RUNTIME",
        help=(
            "The vergil-python runtime `vrg-package matrix` recorded for the release "
            "(the build cell's `runtime`). Empty or absent: resolve it here, as a "
            "local build does"
        ),
    )
    b.set_defaults(func=_cmd_build)
    r = sub.add_parser(
        "check-runtime",
        help="Refuse a release whose packages depend on different vergil-python runtimes",
        description=(
            "Check that every .deb/.rpm in --artifacts depends on exactly the "
            "vergil-python runtime --manifest records (#3478). Read-only."
        ),
    )
    r.add_argument("--manifest", required=True, metavar="PATH", help="packages-manifest.json")
    r.add_argument("--artifacts", required=True, metavar="DIR", help="Every built package")
    r.set_defaults(func=_cmd_check_runtime)
    c = sub.add_parser(
        "check-unpublished",
        help="Refuse a release that would re-publish an already published package version",
        description=(
            "Refuse when any package a release at --version would publish has the same "
            "(name, full version, arch, format) as one an earlier release already "
            "published (#3309). Read-only."
        ),
    )
    c.add_argument("--version", required=True, help="Repo version being released (e.g. 2.1.240)")
    c.add_argument(
        "--repo",
        default="",
        metavar="OWNER/NAME",
        help="Repository whose releases to check (default: $GITHUB_REPOSITORY)",
    )
    c.set_defaults(func=_cmd_check_unpublished)
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
    it = sub.add_parser(
        "install-test",
        help="Install-test one test cell's artifact (install, units, smoke, removal)",
        description=(
            "Install the artifact for one test cell in this (clean) container, verify "
            "shipped systemd units, run the smoke command and shim checks under a "
            "sanitized environment, remove it and check for residue (spec §8.1)."
        ),
    )
    it.add_argument("--cell", required=True, help="Test cell id from `vrg-package matrix`")
    it.add_argument("--artifacts", required=True, metavar="DIR", help="Built artifact directory")
    it.add_argument("--report", required=True, metavar="PATH", help="Write the JSON report here")
    it.set_defaults(func=_cmd_install_test)
    return parser.parse_args(argv)


def _cmd_matrix(args: argparse.Namespace) -> int:
    cwd = Path.cwd()
    cfg = config.read_config(cwd)
    tier = matrix.Tier(args.tier)
    m = matrix.resolve(cfg.package, tier=tier) if cfg.package is not None else None
    # Resolved once here, for the whole release: every build cell receives it
    # and builds against it instead of re-reading the window (#3478).
    runtime = python_builder.release_runtime(cwd, cfg.package) if cfg.package is not None else None
    if runtime is not None:
        print(f"vrg-package: release runtime vergil-python {runtime}", file=sys.stderr)
    payload: dict[str, object] = (
        {"enabled": True, "tier": tier.value, **matrix.to_json(m, runtime=runtime)}
        if m is not None
        else {"enabled": False, "tier": tier.value, "build": [], "test": []}
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
                json.dumps(matrix.manifest(m, runtime=runtime), indent=2) + "\n",
                encoding="utf-8",
            )
    if args.github_output:
        with Path(output_path).open("a", encoding="utf-8") as fh:
            fh.write(f"enabled={'true' if m is not None else 'false'}\n")
            fh.write(f"build={json.dumps(payload['build'])}\n")
            fh.write(f"test={json.dumps(payload['test'])}\n")
            fh.write(f"tier={tier.value}\n")
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
        # Empty means "none recorded", so a workflow can pass the matrix
        # cell's ``runtime`` verbatim, including for a builder without one.
        runtime=args.runtime or None,
    )
    for path in artifacts:
        print(path)
    return 0


def _cmd_check_runtime(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        msg = f"{path} is not valid JSON: {exc}"
        raise PackageError(msg) from exc
    if not isinstance(data, dict):
        msg = f"{path}: expected a JSON object"
        raise PackageError(msg)
    recorded = data.get("runtime")
    if recorded is None:
        print(f"vrg-package: {path} records no runtime (not a python-builder release); passed")
        return 0
    if not isinstance(recorded, str):
        msg = f"{path}: 'runtime' must be a string, got {recorded!r}"
        raise PackageError(msg)
    artifacts = Path(args.artifacts)
    files = sorted(p for p in artifacts.iterdir() if p.suffix in {".deb", ".rpm"})
    packages = [
        (f.name, (collect.deb_metadata if f.suffix == ".deb" else collect.rpm_metadata)(f)[4])
        for f in files
    ]
    pkg_runtime.check_consistent(recorded, packages, str(artifacts))
    print(
        f"vrg-package: all {len(packages)} package(s) in {artifacts} depend on "
        f"{pkg_runtime.runtime_package(pkg_runtime.series_of(recorded))}"
    )
    return 0


def _cmd_check_unpublished(args: argparse.Namespace) -> int:
    repo = args.repo or os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        msg = "check-unpublished: pass --repo OWNER/NAME (GITHUB_REPOSITORY is not set)"
        raise PackageError(msg)
    cwd = Path.cwd()
    cfg = config.read_config(cwd)
    identities = published.check_release(cwd, cfg.package, repo, args.version)
    if not identities:
        print(f"vrg-package: no [package] section; {repo} v{args.version} publishes no packages")
        return 0
    for ident in identities:
        print(f"vrg-package: {ident}: not yet published by {repo}")
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


def _cmd_install_test(args: argparse.Namespace) -> int:
    # Imported here, not at module top, to keep sibling subcommand changes conflict-free.
    import subprocess

    from vergil_tooling.lib.package import install_test
    from vergil_tooling.lib.process_errors import failure_message

    cwd = Path.cwd()
    try:
        install_test.run_install_test(
            repo_root=cwd,
            cell_id=args.cell,
            artifacts=cwd / args.artifacts,
            report=cwd / args.report,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        msg = f"{failure_message(exc)}\n{detail}".rstrip()
        raise PackageError(msg) from exc
    return 0


if __name__ == "__main__":
    sys.exit(main())
