"""``vrg-ci-evidence`` — the CI-evidence harvester CLI.

A thin orchestrator over :mod:`vergil_tooling.lib.ci_evidence`: every stage is a
call into the library, the CLI only wires them together and maps the two
substantive failures (:class:`~vergil_tooling.lib.ci_evidence.IncompleteEvidenceError`,
:class:`~vergil_tooling.lib.ci_evidence.NoQualifyingRunError`) to a non-zero exit
via :func:`~vergil_tooling.lib.output.emit_error`.

``--generated-at`` is caller-injected (CD supplies the release timestamp), so the
harvest stays a pure function of its inputs and the bundle is byte-reproducible.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from vergil_tooling.lib import ci_evidence, git, github
from vergil_tooling.lib.ci_evidence import (
    HarvestContext,
    IncompleteEvidenceError,
    NoQualifyingRunError,
)
from vergil_tooling.lib.output import emit_error


def cmd_bundle(args: argparse.Namespace) -> int:
    """Harvest the release's CI evidence and assemble the bundle + manifest.

    Each line is one library stage: resolve the release PR, its validated head
    SHA and the qualifying CI run; derive the required gates and the check-run
    conclusions; download and parse the evidence artifacts; enforce completeness
    (raises before anything is written on a missing gate); stage the metadata,
    build and write the manifest, tar the tree, and drop a standalone manifest
    next to the tarball.
    """
    repo: str = args.repo
    version: str = args.version
    tag = f"v{version}"
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    release_pr = ci_evidence.resolve_release_pr(repo, args.merge_sha)
    head_sha = github.head_sha(str(release_pr))
    run = ci_evidence.select_ci_run(repo, head_sha)

    required = ci_evidence.resolve_required_gates(repo, git.repo_root())
    conclusions = ci_evidence.read_gate_conclusions(repo, head_sha)

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        gates_dest = staging / "evidence" / "gates"
        gates_dest.mkdir(parents=True, exist_ok=True)

        gate_dirs = ci_evidence.download_evidence_artifacts(repo, int(run["id"]), gates_dest)
        harvested = ci_evidence.load_harvested_gates(gate_dirs, required, conclusions)
        ci_evidence.validate_completeness(required, harvested)

        ci_evidence.write_checks_json(conclusions, staging)
        ci_evidence.write_readme(staging)
        if args.sbom_file:
            ci_evidence.copy_sbom(Path(args.sbom_file), staging)

        ctx = HarvestContext(
            repo=repo,
            version=version,
            tag=tag,
            released_commit=args.merge_sha,
            release_pr=release_pr,
            validated_head_sha=head_sha,
            ci_run_urls=(str(run["html_url"]),),
        )
        manifest = ci_evidence.build_manifest(
            ctx,
            list(harvested.values()),
            generated_at=args.generated_at,
            missing_gates=[],
            staging_dir=staging,
        )
        manifest_path = ci_evidence.write_manifest(manifest, staging)

        tarball = out_dir / f"{tag}-ci-evidence.tar.gz"
        ci_evidence.assemble_bundle(staging, tarball)
        shutil.copyfile(manifest_path, out_dir / f"{tag}-ci-evidence-manifest.json")

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="vrg-ci-evidence",
        description="Harvest and bundle a release's CI evidence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_bundle = sub.add_parser("bundle", help="Assemble the CI-evidence bundle for a release.")
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
