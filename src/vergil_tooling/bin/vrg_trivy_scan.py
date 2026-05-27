"""Run Trivy vulnerability scan with dual-output format."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.output import emit_error, write_output, write_summary
from vergil_tooling.lib.sarif import evaluate_findings, format_summary, parse_sarif
from vergil_tooling.lib.trivy import generate_sbom, run_scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-trivy-scan",
        description="Run Trivy vulnerability scan with SARIF and table output.",
    )
    parser.add_argument(
        "--type",
        required=True,
        choices=["filesystem", "image"],
        dest="scan_type",
        help="Scan type: filesystem or image",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Target path (filesystem) or image name (image)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for SARIF and table output",
    )
    parser.add_argument(
        "--trivyignore",
        type=Path,
        default=None,
        help="Path to .trivyignore file",
    )
    parser.add_argument(
        "--sbom",
        type=Path,
        default=None,
        help="Generate CycloneDX SBOM at this path",
    )
    parser.add_argument(
        "--severity",
        default="MEDIUM,HIGH,CRITICAL",
        help="Severity filter (default: MEDIUM,HIGH,CRITICAL)",
    )
    args = parser.parse_args(argv)

    scan_result = run_scan(args.scan_type, args.target, args.output_dir)

    if scan_result.returncode > 1:
        emit_error(f"trivy scan failed with exit code {scan_result.returncode}")
        return 2

    sarif_file = Path(scan_result.sarif_path)
    if sarif_file.is_file():
        sarif_data = [parse_sarif(sarif_file)]
        result = evaluate_findings(sarif_data)

        for finding in result.findings:
            emit_error(
                f"[{finding.rule_id}] {finding.message}",
                file=finding.file,
                line=finding.line,
            )

        write_output("finding_count", str(len(result.findings)))

        if not result.passed:
            write_summary(format_summary(result))
            if args.sbom:
                generate_sbom(args.target, args.sbom)
                write_output("sbom_path", str(args.sbom))
            return 1
    else:
        write_output("finding_count", "0")

    if args.sbom:
        generate_sbom(args.target, args.sbom)
        write_output("sbom_path", str(args.sbom))

    return 0


if __name__ == "__main__":
    sys.exit(main())
