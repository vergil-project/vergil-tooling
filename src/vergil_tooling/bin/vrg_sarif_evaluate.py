"""Evaluate SARIF files for security findings."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.output import emit_error, emit_warning, write_summary
from vergil_tooling.lib.sarif import (
    evaluate_findings,
    format_summary,
    parse_sarif,
    parse_sarif_directory,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-sarif-evaluate",
        description="Evaluate SARIF files for security findings.",
    )
    parser.add_argument(
        "path",
        type=Path,
        help="SARIF file or directory containing .sarif files",
    )
    parser.add_argument(
        "--severity",
        nargs="+",
        default=["warning", "error"],
        help="Severity levels to include (default: warning error)",
    )
    args = parser.parse_args(argv)

    path: Path = args.path
    severity_filter = set(args.severity)

    if path.is_dir():
        sarif_data = parse_sarif_directory(path)
        if not sarif_data:
            emit_warning(f"no SARIF files found in {path}")
            return 0
    elif path.is_file():
        sarif_data = [parse_sarif(path)]
    else:
        emit_error(f"path not found: {path}")
        return 2

    result = evaluate_findings(sarif_data, severity_filter)

    for finding in result.findings:
        emit_error(
            f"[{finding.rule_id}] {finding.message}",
            file=finding.file,
            line=finding.line,
        )

    if not result.passed:
        write_summary(format_summary(result))

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
