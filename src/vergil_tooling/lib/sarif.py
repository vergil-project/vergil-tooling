"""SARIF file parsing and finding evaluation."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# SARIF 2.1.0 §3.35.3: the only defined suppression kinds.
SUPPRESSION_KINDS = frozenset({"inSource", "external"})

# SARIF 2.1.0 §3.35.4: a suppression with no ``status`` is in effect, as is one
# whose status is ``accepted``. ``underReview`` and ``rejected`` are not — the
# gate fails closed on them.
_EFFECTIVE_SUPPRESSION_STATUS = "accepted"


@dataclass(frozen=True)
class SarifFinding:
    rule_id: str
    message: str
    level: str
    file: str
    line: int


@dataclass(frozen=True)
class SuppressedFinding:
    """A finding the analyzer reported as suppressed (not a gate failure).

    ``kind`` is the SARIF suppression kind (``inSource`` for an in-code comment
    such as ``# nosemgrep: <rule-id>``, ``external`` for an out-of-band record)
    and ``justification`` is the SARIF ``justification`` text, empty when the
    analyzer did not supply one.
    """

    rule_id: str
    message: str
    level: str
    file: str
    line: int
    kind: str
    justification: str


@dataclass(frozen=True)
class EvaluationResult:
    findings: list[SarifFinding] = field(default_factory=list)
    suppressed: list[SuppressedFinding] = field(default_factory=list)
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


def effective_suppression(result: dict[str, Any]) -> dict[str, Any] | None:
    """Return the suppression that takes ``result`` out of the gate, if any.

    A result is suppressed when its ``suppressions`` array holds at least one
    object whose ``kind`` is a SARIF-defined kind (``inSource`` / ``external``)
    and whose ``status`` is absent or ``accepted``. Anything else — an empty or
    malformed array, an unknown kind, ``underReview`` or ``rejected`` — is not
    a suppression, so the finding still counts (fail closed).
    """
    suppressions = result.get("suppressions")
    if not isinstance(suppressions, list):
        return None
    for suppression in suppressions:
        if not isinstance(suppression, dict):
            continue
        if suppression.get("kind") not in SUPPRESSION_KINDS:
            continue
        status = suppression.get("status", _EFFECTIVE_SUPPRESSION_STATUS)
        if status == _EFFECTIVE_SUPPRESSION_STATUS:
            return suppression
    return None


def filter_suppressed(sarif: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Return a copy of ``sarif`` without its effectively-suppressed results.

    Every result for which ``effective_suppression`` reports an accepted
    suppression is dropped from every run, at any severity. Everything else —
    top-level fields, runs, tool, rules, invocations and unsuppressed results —
    is kept as-is. A run whose ``results`` is not a list, or a result that is
    not an object, is left untouched (it carries no effective suppression).
    The input is not mutated. Returns the copy and the number of removed
    results.

    Raises ``ValueError`` for a document that is not a SARIF object with a
    ``runs`` list.
    """
    if not isinstance(sarif, dict) or not isinstance(sarif.get("runs"), list):
        msg = "invalid SARIF document: expected an object with a 'runs' list"
        raise ValueError(msg)
    filtered = copy.deepcopy(sarif)
    removed = 0
    for run in filtered["runs"]:
        if not isinstance(run, dict) or not isinstance(run.get("results"), list):
            continue
        kept = [
            result
            for result in run["results"]
            if not isinstance(result, dict) or effective_suppression(result) is None
        ]
        removed += len(run["results"]) - len(kept)
        run["results"] = kept
    return filtered, removed


def evaluate_findings(
    sarif_data: list[dict[str, Any]],
    severity_filter: set[str] | None = None,
) -> EvaluationResult:
    """Filter findings by severity level across SARIF data.

    Results carrying an effective suppression (see ``effective_suppression``)
    are collected in ``suppressed`` rather than ``findings``: they do not fail
    the gate, but they stay visible in the result and its summary.
    """
    if severity_filter is None:
        severity_filter = {"warning", "error"}

    findings: list[SarifFinding] = []
    suppressed: list[SuppressedFinding] = []
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
                suppression = effective_suppression(result)
                if suppression is not None:
                    suppressed.append(
                        SuppressedFinding(
                            rule_id=rule_id,
                            message=message,
                            level=level,
                            file=file_path,
                            line=line_num,
                            kind=suppression["kind"],
                            justification=suppression.get("justification", ""),
                        )
                    )
                    continue
                findings.append(
                    SarifFinding(
                        rule_id=rule_id,
                        message=message,
                        level=level,
                        file=file_path,
                        line=line_num,
                    )
                )

    return EvaluationResult(findings=findings, suppressed=suppressed, passed=len(findings) == 0)


def _cell(value: object) -> str:
    """Render ``value`` as a markdown table cell (escape the column separator)."""
    return str(value).replace("|", "\\|")


def format_summary(result: EvaluationResult) -> str:
    """Generate a markdown summary of evaluation results.

    Failing findings and suppressed findings are listed in separate tables, so
    a suppression is always visible in the summary — never silently dropped.
    """
    if not result.findings and not result.suppressed:
        return "## Security Scan Results\n\nNo findings.\n"

    lines = [
        "## Security Scan Results",
        "",
        f"**{len(result.findings)} finding(s)**",
    ]
    if result.findings:
        lines += [
            "",
            "| Rule | Level | File | Line | Message |",
            "|------|-------|------|------|---------|",
        ]
        for f in result.findings:
            lines.append(
                f"| {_cell(f.rule_id)} | {_cell(f.level)} | {_cell(f.file)} "
                f"| {f.line} | {_cell(f.message)} |"
            )
    if result.suppressed:
        lines += [
            "",
            f"**{len(result.suppressed)} suppressed finding(s)** (not counted against the gate)",
            "",
            "| Rule | Level | File | Line | Suppression | Justification |",
            "|------|-------|------|------|-------------|---------------|",
        ]
        for s in result.suppressed:
            lines.append(
                f"| {_cell(s.rule_id)} | {_cell(s.level)} | {_cell(s.file)} | {s.line} "
                f"| {_cell(s.kind)} | {_cell(s.justification)} |"
            )
    return "\n".join(lines) + "\n"
