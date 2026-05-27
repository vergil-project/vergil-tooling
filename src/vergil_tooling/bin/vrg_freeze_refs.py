"""Freeze and validate internal action references in workflow YAML files."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.freeze_refs import (
    collect_yaml_files,
    freeze_references,
    validate_no_unfrozen,
)
from vergil_tooling.lib.output import emit_error, emit_warning, write_summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-freeze-refs",
        description="Freeze internal action references in workflow YAML files.",
    )
    parser.add_argument(
        "--owner-repo",
        required=True,
        help="Owner/repo (e.g. vergil-project/vergil-actions)",
    )
    parser.add_argument("--tag", required=True, help="Release tag to freeze to (e.g. v2.0.50)")
    parser.add_argument(
        "--scan-dirs",
        nargs="+",
        type=Path,
        default=[Path(".github/workflows"), Path("actions")],
        help="Directories to scan (default: .github/workflows actions)",
    )
    args = parser.parse_args(argv)

    files = collect_yaml_files(args.scan_dirs)
    if not files:
        emit_warning("no YAML files found in scan directories")
        return 0

    all_findings: list[str] = []
    frozen_count = 0

    for filepath in files:
        original = filepath.read_text(encoding="utf-8")
        frozen = freeze_references(original, args.owner_repo, args.tag)

        if frozen != original:
            filepath.write_text(frozen, encoding="utf-8")
            frozen_count += 1

        findings = validate_no_unfrozen(frozen, str(filepath), args.owner_repo)
        for f in findings:
            emit_error(f"unfrozen reference: {f.text}", file=f.file, line=f.line)
            all_findings.append(f"{f.file}:{f.line}: {f.text}")

    if all_findings:
        summary_lines = "\n".join(f"- `{f}`" for f in all_findings)
        write_summary(f"## Unfrozen References Found\n\n{summary_lines}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
