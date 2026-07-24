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

# Rules excluded from every fleet scan, by full semgrep rule id.
#
# github-actions-mutable-action-tag flags every `uses: …@vN` action ref. It is
# exempted fleet-wide pending vergil-project/.github#194 (pin third-party action
# SHAs once pin-advancement tooling exists); our own vergil-actions@v2.1 refs are
# a permanent exception (our release line). All other rules stay enforced.
DEFAULT_EXCLUDED_RULES: tuple[str, ...] = (
    "yaml.github-actions.security.github-actions-mutable-action-tag"
    ".github-actions-mutable-action-tag",
)


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
    *,
    exclude_rules: list[str] | None = None,
) -> ScanResult:
    """Execute semgrep scan with the given rulesets.

    Semgrep exit codes: 0 = clean, 1 = findings, >1 = error.
    All three can produce valid SARIF output.

    The fleet defaults in ``DEFAULT_EXCLUDED_RULES`` are always excluded via
    ``--exclude-rule``; any ``exclude_rules`` the caller supplies are added on
    top of (never in place of) the defaults.
    """
    excluded: list[str] = list(DEFAULT_EXCLUDED_RULES)
    for rule in exclude_rules or []:
        if rule not in excluded:
            excluded.append(rule)

    cmd: list[str] = ["semgrep", "scan", "--sarif", "--output", str(output_path)]
    for ruleset in rulesets:
        cmd.extend(["--config", ruleset])
    for rule in excluded:
        cmd.extend(["--exclude-rule", rule])
    cmd.append(str(target_dir))

    result = subprocess.run(cmd, capture_output=True)  # noqa: S603
    sarif_produced = output_path.is_file() and output_path.stat().st_size > 0

    return ScanResult(returncode=result.returncode, sarif_produced=sarif_produced)
