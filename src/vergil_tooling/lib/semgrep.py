"""Semgrep scan orchestration: ruleset resolution and scan execution."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_LANGUAGE_RULESETS: dict[str, str] = {
    "python": "p/python",
    "go": "p/golang",
    "java": "p/java",
    "ruby": "p/ruby",
    "rust": "p/rust",
}


@dataclass(frozen=True)
class ScanResult:
    returncode: int
    sarif_produced: bool


def resolve_rulesets(
    language: str,
    *,
    has_dockerfiles: bool = False,
    has_workflows: bool = False,
    extra_config: list[str] | None = None,
) -> list[str]:
    """Resolve semgrep rulesets for the given language and repo context."""
    rulesets: list[str] = []

    base = _LANGUAGE_RULESETS.get(language)
    if base:
        rulesets.append(base)

    if has_dockerfiles:
        rulesets.append("p/dockerfile")

    if has_workflows:
        rulesets.append("p/github-actions")

    if extra_config:
        rulesets.extend(extra_config)

    return rulesets


def run_scan(
    rulesets: list[str],
    target_dir: Path,
    output_path: Path,
) -> ScanResult:
    """Execute semgrep scan with the given rulesets.

    Semgrep exit codes: 0 = clean, 1 = findings, >1 = error.
    All three can produce valid SARIF output.
    """
    cmd: list[str] = ["semgrep", "scan", "--sarif", "--output", str(output_path)]
    for ruleset in rulesets:
        cmd.extend(["--config", ruleset])
    cmd.append(str(target_dir))

    result = subprocess.run(cmd, capture_output=True)  # noqa: S603
    sarif_produced = output_path.is_file() and output_path.stat().st_size > 0

    return ScanResult(returncode=result.returncode, sarif_produced=sarif_produced)
