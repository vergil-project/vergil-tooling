"""Trivy scan orchestration: Docker argument construction and scan execution."""

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


def build_docker_args(
    scan_type: str,
    target: str,
    output_dir: str,
    *,
    trivyignore: str | None = None,
    severity: str = "MEDIUM,HIGH,CRITICAL",
) -> list[str]:
    """Construct the docker run argument list for a Trivy scan."""
    args = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{output_dir}:/output",
    ]

    if scan_type == "filesystem":
        args.extend(["-v", f"{target}:/scan:ro"])

    if scan_type == "image":
        args.extend(["-v", "/var/run/docker.sock:/var/run/docker.sock"])

    if trivyignore:
        args.extend(["-v", f"{trivyignore}:/trivyignore:ro"])
        args.extend(["-e", "TRIVY_IGNOREFILE=/trivyignore"])

    args.extend(
        [
            "aquasec/trivy:latest",
            "--severity",
            severity,
        ]
    )

    return args


def _run_trivy_command(args: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(args, capture_output=True)  # noqa: S603


def run_scan(
    scan_type: str,
    target: str,
    output_dir: Path,
) -> ScanResult:
    """Execute the scan-once-convert-twice workflow.

    1. Trivy scan to JSON
    2. Convert to table (stdout)
    3. Convert to SARIF (file)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = str(output_dir / "trivy-results.json")
    table_path = str(output_dir / "trivy-results.table")
    sarif_path = str(output_dir / "trivy-results.sarif")

    base_args = build_docker_args(scan_type, target, str(output_dir))

    scan_target = "/scan" if scan_type == "filesystem" else target
    json_output = "/output/trivy-results.json"
    scan_cmd = [
        *base_args,
        scan_type,
        "--format",
        "json",
        "--output",
        json_output,
        scan_target,
    ]
    scan_result = _run_trivy_command(scan_cmd)

    if scan_result.returncode > 1:
        return ScanResult(
            returncode=scan_result.returncode,
            sarif_path=sarif_path,
            table_path=table_path,
        )

    table_cmd = [
        *base_args,
        "convert",
        "--format",
        "table",
        "--output",
        "/output/trivy-results.table",
        json_path,
    ]
    _run_trivy_command(table_cmd)

    sarif_cmd = [
        *base_args,
        "convert",
        "--format",
        "sarif",
        "--output",
        "/output/trivy-results.sarif",
        json_path,
    ]
    _run_trivy_command(sarif_cmd)

    return ScanResult(
        returncode=scan_result.returncode,
        sarif_path=sarif_path,
        table_path=table_path,
    )


def build_sbom_args(target: str, output_path: str) -> list[str]:
    """Construct docker run arguments for CycloneDX SBOM generation."""
    return [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{target}:/scan:ro",
        "aquasec/trivy:latest",
        "filesystem",
        "--format",
        "cyclonedx",
        "--output",
        f"/output/{output_path}",
        "/scan",
    ]


def generate_sbom(target: str, output_path: Path) -> int:
    """Generate a CycloneDX SBOM via Docker."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = build_sbom_args(target, output_path.name)
    args[5] = f"{output_path.parent}:/output"
    result = _run_trivy_command(args)
    return result.returncode
