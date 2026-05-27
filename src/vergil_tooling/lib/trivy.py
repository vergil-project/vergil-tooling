"""Trivy scan orchestration: direct subprocess invocation."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ScanResult:
    returncode: int
    sarif_path: str
    table_path: str


def _run_trivy_command(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True)  # noqa: S603


def run_scan(
    scan_type: str,
    target: str,
    output_dir: Path,
    *,
    severity: str = "MEDIUM,HIGH,CRITICAL",
    trivyignore: str | None = None,
) -> ScanResult:
    """Execute the scan-once-convert-twice workflow.

    1. Trivy scan to JSON
    2. Convert to table (file)
    3. Convert to SARIF (file)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = str(output_dir / "trivy-results.json")
    table_path = str(output_dir / "trivy-results.table")
    sarif_path = str(output_dir / "trivy-results.sarif")

    scan_cmd = [
        "trivy",
        scan_type,
        "--severity",
        severity,
        "--format",
        "json",
        "--output",
        json_path,
    ]
    if trivyignore:
        scan_cmd.extend(["--ignorefile", trivyignore])
    scan_cmd.append(target)

    scan_result = _run_trivy_command(scan_cmd)

    if scan_result.returncode > 1:
        return ScanResult(
            returncode=scan_result.returncode,
            sarif_path=sarif_path,
            table_path=table_path,
        )

    table_cmd = [
        "trivy",
        "convert",
        "--format",
        "table",
        "--output",
        table_path,
        json_path,
    ]
    _run_trivy_command(table_cmd)

    sarif_cmd = [
        "trivy",
        "convert",
        "--format",
        "sarif",
        "--output",
        sarif_path,
        json_path,
    ]
    _run_trivy_command(sarif_cmd)

    return ScanResult(
        returncode=scan_result.returncode,
        sarif_path=sarif_path,
        table_path=table_path,
    )


def generate_sbom(target: str, output_path: Path) -> int:
    """Generate a CycloneDX SBOM."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "trivy",
        "filesystem",
        "--format",
        "cyclonedx",
        "--output",
        str(output_path),
        target,
    ]
    result = _run_trivy_command(args)
    return result.returncode
