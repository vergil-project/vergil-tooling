"""``vrg-ci-evidence`` — the CI-evidence harvester CLI.

A thin orchestrator over :mod:`vergil_tooling.lib.ci_evidence`: every subcommand
maps to one library orchestrator, and the CLI only wires argparse to it and maps
the two substantive failures
(:class:`~vergil_tooling.lib.ci_evidence.IncompleteEvidenceError`,
:class:`~vergil_tooling.lib.ci_evidence.NoQualifyingRunError`) to a non-zero exit
via :func:`~vergil_tooling.lib.output.emit_error`.

The atomic flow is split into two subcommands so a release harvests its evidence
exactly once (issue #2330):

- ``harvest`` — resolve → select run → download → validate; persist the
  harvested tree + a JSON state file into a staging dir (the pre-publish gate).
- ``assemble`` — consume the persisted harvest → build the manifest, tar the
  tree, drop a standalone manifest (the post-release attach; no network).
- ``bundle`` — ``harvest`` then ``assemble`` composed through a temp staging dir
  (back-compat + local dogfooding).

``--generated-at`` is caller-injected (CD supplies the release timestamp), so the
assemble stays a pure function of its inputs and the bundle is byte-reproducible.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib import ci_evidence
from vergil_tooling.lib.ci_evidence import (
    IncompleteEvidenceError,
    NoQualifyingRunError,
)
from vergil_tooling.lib.output import emit_error


def cmd_harvest(args: argparse.Namespace) -> int:
    """Harvest the release's CI evidence into a staging dir (pre-publish gate).

    Delegates to :func:`~vergil_tooling.lib.ci_evidence.run_harvest`, which
    resolves the release PR, selects the qualifying CI run, downloads the
    evidence artifacts, enforces completeness, and persists the harvested tree
    plus a state file into ``--out-dir`` for a later network-free assemble.
    """
    ci_evidence.run_harvest(args.repo, args.merge_sha, Path(args.out_dir))
    return 0


def cmd_assemble(args: argparse.Namespace) -> int:
    """Assemble the bundle from a persisted harvest (post-release attach).

    Delegates to :func:`~vergil_tooling.lib.ci_evidence.run_assemble`, which
    reads the persisted state + tree from ``--staging`` and writes the tarball +
    standalone manifest into ``--out-dir``. No network.
    """
    ci_evidence.run_assemble(
        Path(args.staging),
        version=args.version,
        generated_at=args.generated_at,
        out_dir=Path(args.out_dir),
        sbom_file=Path(args.sbom_file) if args.sbom_file else None,
    )
    return 0


def cmd_bundle(args: argparse.Namespace) -> int:
    """Harvest then assemble in one shot (back-compat + local dogfooding).

    Delegates to :func:`~vergil_tooling.lib.ci_evidence.run_bundle`, which
    composes ``harvest`` and ``assemble`` over a throwaway staging directory —
    the original atomic behaviour.
    """
    ci_evidence.run_bundle(
        args.repo,
        version=args.version,
        merge_sha=args.merge_sha,
        generated_at=args.generated_at,
        out_dir=Path(args.out_dir),
        sbom_file=Path(args.sbom_file) if args.sbom_file else None,
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-ci-evidence",
        description="Harvest and bundle a release's CI evidence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_harvest = sub.add_parser(
        "harvest", help="Harvest a release's CI evidence into a staging dir (gate)."
    )
    p_harvest.add_argument("--repo", required=True, help="owner/name of the release repo")
    p_harvest.add_argument("--merge-sha", required=True, help="release merge commit SHA")
    p_harvest.add_argument(
        "--out-dir", required=True, help="staging directory for the harvested tree + state"
    )
    p_harvest.set_defaults(func=cmd_harvest)

    p_assemble = sub.add_parser(
        "assemble", help="Assemble the bundle from a persisted harvest (no network)."
    )
    p_assemble.add_argument(
        "--staging", required=True, help="staging directory a prior harvest wrote"
    )
    p_assemble.add_argument("--version", required=True, help="release version, e.g. 2.1.129")
    p_assemble.add_argument(
        "--generated-at", required=True, help="ISO-8601 timestamp (caller-injected)"
    )
    p_assemble.add_argument("--out-dir", required=True, help="directory for the bundle + manifest")
    p_assemble.add_argument("--sbom-file", default=None, help="optional pre-built SBOM to include")
    p_assemble.set_defaults(func=cmd_assemble)

    p_bundle = sub.add_parser("bundle", help="Harvest + assemble in one shot (back-compat).")
    p_bundle.add_argument("--repo", required=True, help="owner/name of the release repo")
    p_bundle.add_argument("--version", required=True, help="release version, e.g. 2.1.129")
    p_bundle.add_argument("--merge-sha", required=True, help="release merge commit SHA")
    p_bundle.add_argument(
        "--generated-at", required=True, help="ISO-8601 timestamp (caller-injected)"
    )
    p_bundle.add_argument("--out-dir", required=True, help="directory for the bundle + manifest")
    p_bundle.add_argument("--sbom-file", default=None, help="optional pre-built SBOM to include")
    p_bundle.set_defaults(func=cmd_bundle)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return int(args.func(args))
    except (IncompleteEvidenceError, NoQualifyingRunError) as exc:
        emit_error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
