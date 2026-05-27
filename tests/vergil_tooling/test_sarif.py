"""Tests for vergil_tooling.lib.sarif."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.sarif import (
    EvaluationResult,
    SarifFinding,
    evaluate_findings,
    format_summary,
    parse_sarif,
    parse_sarif_directory,
)

if TYPE_CHECKING:
    from pathlib import Path

_CLEAN_SARIF = {
    "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
    "version": "2.1.0",
    "runs": [{"tool": {"driver": {"name": "test"}}, "results": []}],
}

_FINDINGS_SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "test"}},
            "results": [
                {
                    "ruleId": "RULE-001",
                    "level": "error",
                    "message": {"text": "Critical finding"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/app.py"},
                                "region": {"startLine": 42},
                            }
                        }
                    ],
                },
                {
                    "ruleId": "RULE-002",
                    "level": "warning",
                    "message": {"text": "Minor issue"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "src/utils.py"},
                                "region": {"startLine": 10},
                            }
                        }
                    ],
                },
                {
                    "ruleId": "RULE-003",
                    "level": "note",
                    "message": {"text": "Info only"},
                    "locations": [],
                },
            ],
        }
    ],
}


def _write_sarif(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestParseSarif:
    def test_valid_file(self, tmp_path: Path) -> None:
        f = _write_sarif(tmp_path / "results.sarif", _CLEAN_SARIF)
        data = parse_sarif(f)
        assert "runs" in data

    def test_invalid_json(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.sarif"
        f.write_text("not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            parse_sarif(f)

    def test_missing_runs(self, tmp_path: Path) -> None:
        f = _write_sarif(tmp_path / "no_runs.sarif", {"version": "2.1.0"})
        with pytest.raises(ValueError, match="invalid SARIF"):
            parse_sarif(f)

    def test_non_dict(self, tmp_path: Path) -> None:
        f = tmp_path / "array.sarif"
        f.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid SARIF"):
            parse_sarif(f)


class TestParseSarifDirectory:
    def test_collects_files(self, tmp_path: Path) -> None:
        _write_sarif(tmp_path / "a.sarif", _CLEAN_SARIF)
        _write_sarif(tmp_path / "b.sarif", _FINDINGS_SARIF)
        results = parse_sarif_directory(tmp_path)
        assert len(results) == 2

    def test_empty_dir(self, tmp_path: Path) -> None:
        assert parse_sarif_directory(tmp_path) == []

    def test_missing_dir(self, tmp_path: Path) -> None:
        assert parse_sarif_directory(tmp_path / "nonexistent") == []

    def test_ignores_non_sarif(self, tmp_path: Path) -> None:
        _write_sarif(tmp_path / "results.sarif", _CLEAN_SARIF)
        (tmp_path / "data.json").write_text("{}")
        results = parse_sarif_directory(tmp_path)
        assert len(results) == 1


class TestEvaluateFindings:
    def test_clean(self) -> None:
        result = evaluate_findings([_CLEAN_SARIF])
        assert result.passed
        assert result.findings == []

    def test_filters_by_severity(self) -> None:
        result = evaluate_findings([_FINDINGS_SARIF])
        assert not result.passed
        assert len(result.findings) == 2
        levels = {f.level for f in result.findings}
        assert levels == {"error", "warning"}

    def test_error_only_filter(self) -> None:
        result = evaluate_findings([_FINDINGS_SARIF], severity_filter={"error"})
        assert not result.passed
        assert len(result.findings) == 1
        assert result.findings[0].rule_id == "RULE-001"

    def test_note_filter(self) -> None:
        result = evaluate_findings([_FINDINGS_SARIF], severity_filter={"note"})
        assert not result.passed
        assert len(result.findings) == 1
        assert result.findings[0].rule_id == "RULE-003"

    def test_no_matching_severity(self) -> None:
        result = evaluate_findings([_FINDINGS_SARIF], severity_filter={"critical"})
        assert result.passed
        assert result.findings == []

    def test_multiple_sarif_data(self) -> None:
        result = evaluate_findings([_FINDINGS_SARIF, _FINDINGS_SARIF])
        assert len(result.findings) == 4

    def test_empty_data(self) -> None:
        result = evaluate_findings([])
        assert result.passed

    def test_finding_without_location(self) -> None:
        result = evaluate_findings([_FINDINGS_SARIF], severity_filter={"note"})
        finding = result.findings[0]
        assert finding.file == ""
        assert finding.line == 0

    def test_finding_extracts_location(self) -> None:
        result = evaluate_findings([_FINDINGS_SARIF], severity_filter={"error"})
        finding = result.findings[0]
        assert finding.file == "src/app.py"
        assert finding.line == 42

    def test_result_without_level_defaults_to_warning(self) -> None:
        sarif = {
            "runs": [
                {
                    "results": [
                        {
                            "ruleId": "NO-LEVEL",
                            "message": {"text": "missing level"},
                            "locations": [],
                        }
                    ]
                }
            ]
        }
        result = evaluate_findings([sarif])
        assert len(result.findings) == 1
        assert result.findings[0].level == "warning"


class TestFormatSummary:
    def test_clean(self) -> None:
        result = EvaluationResult(findings=[], passed=True)
        summary = format_summary(result)
        assert "No findings" in summary

    def test_with_findings(self) -> None:
        findings = [
            SarifFinding(
                rule_id="R1",
                message="bad",
                level="error",
                file="a.py",
                line=1,
            )
        ]
        result = EvaluationResult(findings=findings, passed=False)
        summary = format_summary(result)
        assert "1 finding(s)" in summary
        assert "R1" in summary
        assert "a.py" in summary
