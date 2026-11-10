"""Run semgrep scan with language-aware ruleset resolution."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib.cli import cli_main
from vergil_tooling.lib.output import emit_error, emit_warning, write_output, write_summary
from vergil_tooling.lib.sarif import evaluate_findings, format_summary, parse_sarif
from vergil_tooling.lib.semgrep import resolve_rulesets, run_scan

if TYPE_CHECKING:
    from vergil_tooling.lib.semgrep import ScanResult


@cli_main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-semgrep-scan",
        description="Run semgrep scan with language-aware ruleset resolution.",
    )
    parser.add_argument(
        "--language",
        required=True,
        help="Programming language (python, go, rust)",
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
        "--exclude-rule",
        action="append",
        default=[],
        metavar="RULE_ID",
        help=(
            "Full semgrep rule id to exclude (repeatable). Added on top of the "
            "fleet defaults in DEFAULT_EXCLUDED_RULES, which are always excluded."
        ),
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

    scan_result = run_scan(
        rulesets,
        args.target_dir,
        args.output,
        exclude_rules=args.exclude_rule or None,
    )
    write_output("scan_exit_code", str(scan_result.returncode))

    if scan_result.returncode > 1:
        _report_scan_failure(scan_result)
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
    write_output("suppressed_count", str(len(result.suppressed)))

    if not result.passed or result.suppressed:
        write_summary(format_summary(result))

    return 0 if result.passed else 1


def _last_line(text: str) -> str:
    """Return the last non-empty line of *text*, stripped, or ``""``."""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def _report_scan_failure(scan_result: ScanResult) -> None:
    """Print semgrep's captured output, then an error carrying its key line."""
    for name, text in (("stdout", scan_result.stdout), ("stderr", scan_result.stderr)):
        if text.strip():
            print(f"--- semgrep {name} ---", file=sys.stderr)
            print(text.rstrip("\n"), file=sys.stderr)
    msg = f"semgrep scan failed with exit code {scan_result.returncode}"
    detail = _last_line(scan_result.stderr) or _last_line(scan_result.stdout)
    if detail:
        msg = f"{msg}: {detail}"
    emit_error(msg)


if __name__ == "__main__":
    sys.exit(main())
