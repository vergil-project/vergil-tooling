"""SARIF file parsing and finding evaluation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class SarifFinding:
    rule_id: str
    message: str
    level: str
    file: str
    line: int


@dataclass(frozen=True)
class EvaluationResult:
    findings: list[SarifFinding] = field(default_factory=list)
    passed: bool = True


def parse_sarif(path: Path) -> dict[str, Any]:
    """Load and validate a SARIF JSON file."""
    content = path.read_text(encoding="utf-8")
    data = json.loads(content)
    if not isinstance(data, dict) or "runs" not in data:
        msg = f"invalid SARIF file: {path}"
        raise ValueError(msg)
    return data


def parse_sarif_directory(directory: Path) -> list[dict[str, Any]]:
    """Glob for *.sarif files in a directory and load all."""
    results: list[dict[str, Any]] = []
    if not directory.is_dir():
        return results
    for path in sorted(directory.glob("*.sarif")):
        results.append(parse_sarif(path))
    return results


def evaluate_findings(
    sarif_data: list[dict[str, Any]],
    severity_filter: set[str] | None = None,
) -> EvaluationResult:
    """Filter findings by severity level across SARIF data.

    Returns an EvaluationResult with filtered findings and pass/fail status.
    """
    if severity_filter is None:
        severity_filter = {"warning", "error"}

    findings: list[SarifFinding] = []
    for data in sarif_data:
        for run in data.get("runs", []):
            for result in run.get("results", []):
                level = result.get("level", "warning")
                if level not in severity_filter:
                    continue
                rule_id = result.get("ruleId", "unknown")
                message = result.get("message", {}).get("text", "")
                file_path = ""
                line_num = 0
                locations = result.get("locations", [])
                if locations:
                    phys = locations[0].get("physicalLocation", {})
                    artifact = phys.get("artifactLocation", {})
                    file_path = artifact.get("uri", "")
                    region = phys.get("region", {})
                    line_num = region.get("startLine", 0)
                findings.append(
                    SarifFinding(
                        rule_id=rule_id,
                        message=message,
                        level=level,
                        file=file_path,
                        line=line_num,
                    )
                )

    return EvaluationResult(findings=findings, passed=len(findings) == 0)


def format_summary(result: EvaluationResult) -> str:
    """Generate a markdown summary of evaluation results."""
    if result.passed:
        return "## Security Scan Results\n\nNo findings.\n"

    lines = [
        "## Security Scan Results",
        "",
        f"**{len(result.findings)} finding(s)**",
        "",
        "| Rule | Level | File | Line | Message |",
        "|------|-------|------|------|---------|",
    ]
    for f in result.findings:
        lines.append(f"| {f.rule_id} | {f.level} | {f.file} | {f.line} | {f.message} |")
    return "\n".join(lines) + "\n"
