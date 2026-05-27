"""Run semgrep scan with language-aware ruleset resolution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.output import emit_error, emit_warning, write_output, write_summary
from vergil_tooling.lib.sarif import evaluate_findings, format_summary, parse_sarif
from vergil_tooling.lib.semgrep import resolve_rulesets, run_scan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-semgrep-scan",
        description="Run semgrep scan with language-aware ruleset resolution.",
    )
    parser.add_argument(
        "--language",
        required=True,
        help="Programming language (python, go, java, ruby, rust)",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=Path(),
        help="Directory to scan (default: .)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("semgrep-results.sarif"),
        help="SARIF output path (default: semgrep-results.sarif)",
    )
    parser.add_argument(
        "--extra-config",
        nargs="*",
        default=[],
        help="Additional semgrep config values",
    )
    parser.add_argument(
        "--has-dockerfiles",
        action="store_true",
        help="Add Dockerfile-specific rulesets",
    )
    parser.add_argument(
        "--has-workflows",
        action="store_true",
        help="Add GitHub Actions workflow rulesets",
    )
    args = parser.parse_args(argv)

    rulesets = resolve_rulesets(
        args.language,
        has_dockerfiles=args.has_dockerfiles,
        has_workflows=args.has_workflows,
        extra_config=args.extra_config or None,
    )

    if not rulesets:
        emit_warning(f"no rulesets resolved for language: {args.language}")
        return 0

    scan_result = run_scan(rulesets, args.target_dir, args.output)
    write_output("scan_exit_code", str(scan_result.returncode))

    if scan_result.returncode > 1:
        emit_error(f"semgrep scan failed with exit code {scan_result.returncode}")
        return 2

    if not scan_result.sarif_produced:
        emit_warning("no SARIF output produced")
        return 0

    sarif_data = [parse_sarif(args.output)]
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

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
