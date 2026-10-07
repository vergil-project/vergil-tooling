"""Tests for vergil_tooling.lib.sarif."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.lib.sarif import (
    EvaluationResult,
    SarifFinding,
    SuppressedFinding,
    evaluate_findings,
    filter_suppressed,
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


_RULE = "yaml.github-actions.security.secrets-inherit.secrets-inherit"


def _suppressed_sarif(
    suppressions: object,
    *,
    level: str = "warning",
) -> dict:
    """A one-result SARIF document whose result carries ``suppressions``.

    The shape mirrors semgrep's real SARIF for an inline
    ``# nosemgrep: <rule-id>`` comment (verified 2026-10-06), which emits the
    result with ``"suppressions": [{"kind": "inSource"}]``.
    """
    return {
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": "semgrep"}},
                "results": [
                    {
                        "ruleId": _RULE,
                        "level": level,
                        "message": {"text": "secrets: inherit passes all secrets"},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": ".github/workflows/ci.yml"},
                                    "region": {"startLine": 17},
                                }
                            }
                        ],
                        "suppressions": suppressions,
                    }
                ],
            }
        ],
    }


class TestEvaluateSuppressions:
    def test_semgrep_nosemgrep_insource_is_suppressed(self) -> None:
        result = evaluate_findings([_suppressed_sarif([{"kind": "inSource"}])])
        assert result.passed
        assert result.findings == []
        assert result.suppressed == [
            SuppressedFinding(
                rule_id=_RULE,
                message="secrets: inherit passes all secrets",
                level="warning",
                file=".github/workflows/ci.yml",
                line=17,
                kind="inSource",
                justification="",
            )
        ]

    def test_external_is_suppressed(self) -> None:
        result = evaluate_findings([_suppressed_sarif([{"kind": "external"}])])
        assert result.passed
        assert result.findings == []
        assert [s.kind for s in result.suppressed] == ["external"]

    def test_accepted_status_is_suppressed(self) -> None:
        sarif = _suppressed_sarif(
            [{"kind": "inSource", "status": "accepted", "justification": "reviewed, see #12"}]
        )
        result = evaluate_findings([sarif])
        assert result.passed
        assert result.suppressed[0].justification == "reviewed, see #12"

    def test_rejected_status_does_not_suppress(self) -> None:
        sarif = _suppressed_sarif([{"kind": "inSource", "status": "rejected"}])
        result = evaluate_findings([sarif])
        assert not result.passed
        assert [f.rule_id for f in result.findings] == [_RULE]
        assert result.suppressed == []

    def test_under_review_status_does_not_suppress(self) -> None:
        sarif = _suppressed_sarif([{"kind": "external", "status": "underReview"}])
        result = evaluate_findings([sarif])
        assert not result.passed
        assert len(result.findings) == 1
        assert result.suppressed == []

    def test_any_accepted_entry_suppresses(self) -> None:
        sarif = _suppressed_sarif(
            [
                {"kind": "external", "status": "rejected"},
                {"kind": "inSource"},
            ]
        )
        result = evaluate_findings([sarif])
        assert result.passed
        assert [s.kind for s in result.suppressed] == ["inSource"]

    def test_unknown_kind_does_not_suppress(self) -> None:
        result = evaluate_findings([_suppressed_sarif([{"kind": "somethingElse"}])])
        assert not result.passed
        assert result.suppressed == []

    def test_missing_kind_does_not_suppress(self) -> None:
        result = evaluate_findings([_suppressed_sarif([{"status": "accepted"}])])
        assert not result.passed

    def test_empty_suppressions_list_does_not_suppress(self) -> None:
        result = evaluate_findings([_suppressed_sarif([])])
        assert not result.passed
        assert result.suppressed == []

    def test_malformed_suppressions_do_not_suppress(self) -> None:
        for bad in ("inSource", {"kind": "inSource"}, ["inSource"], None):
            result = evaluate_findings([_suppressed_sarif(bad)])
            assert not result.passed, bad
            assert result.suppressed == []

    def test_suppressed_outside_severity_filter_is_not_reported(self) -> None:
        sarif = _suppressed_sarif([{"kind": "inSource"}], level="note")
        result = evaluate_findings([sarif])
        assert result.passed
        assert result.suppressed == []

    def test_suppressed_and_unsuppressed_mixed(self) -> None:
        result = evaluate_findings([_suppressed_sarif([{"kind": "inSource"}]), _FINDINGS_SARIF])
        assert not result.passed
        assert len(result.findings) == 2
        assert len(result.suppressed) == 1


class TestFormatSummarySuppressed:
    _SUPPRESSED = SuppressedFinding(
        rule_id="R-SUP",
        message="accepted risk",
        level="warning",
        file="b.yml",
        line=7,
        kind="inSource",
        justification="",
    )

    def test_only_suppressed_renders_suppressed_section(self) -> None:
        result = EvaluationResult(findings=[], suppressed=[self._SUPPRESSED], passed=True)
        summary = format_summary(result)
        assert "No findings" not in summary
        assert "**0 finding(s)**" in summary
        assert "1 suppressed finding(s)" in summary
        assert "| R-SUP | warning | b.yml | 7 | inSource |  |" in summary

    def test_findings_and_suppressed_both_rendered(self) -> None:
        failing = SarifFinding(rule_id="R1", message="bad", level="error", file="a.py", line=1)
        justified = SuppressedFinding(
            rule_id="R2",
            message="ok",
            level="error",
            file="c.py",
            line=3,
            kind="external",
            justification="false positive, see #9",
        )
        result = EvaluationResult(findings=[failing], suppressed=[justified], passed=False)
        summary = format_summary(result)
        assert "**1 finding(s)**" in summary
        assert "| R1 | error | a.py | 1 | bad |" in summary
        assert "1 suppressed finding(s)" in summary
        assert "| R2 | error | c.py | 3 | external | false positive, see #9 |" in summary

    def test_findings_without_suppressed_has_no_suppressed_section(self) -> None:
        failing = SarifFinding(rule_id="R1", message="bad", level="error", file="a.py", line=1)
        summary = format_summary(EvaluationResult(findings=[failing], passed=False))
        assert "suppressed" not in summary.lower()

    def test_pipe_in_cell_is_escaped(self) -> None:
        sup = SuppressedFinding(
            rule_id="R3",
            message="m",
            level="warning",
            file="d.py",
            line=4,
            kind="inSource",
            justification="a|b",
        )
        summary = format_summary(EvaluationResult(suppressed=[sup], passed=True))
        assert "a\\|b" in summary


def _result(rule: str, suppressions: object = None) -> dict[str, object]:
    result: dict[str, object] = {"ruleId": rule, "level": "warning", "message": {"text": rule}}
    if suppressions is not None:
        result["suppressions"] = suppressions
    return result


class TestFilterSuppressed:
    def test_removes_semgrep_insource_result(self) -> None:
        sarif = _suppressed_sarif([{"kind": "inSource"}])
        filtered, removed = filter_suppressed(sarif)
        assert removed == 1
        assert filtered["runs"][0]["results"] == []

    def test_preserves_everything_but_suppressed_results(self) -> None:
        sarif = {
            "$schema": "https://example.invalid/sarif-schema-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {"driver": {"name": "semgrep", "rules": [{"id": "A"}, {"id": "B"}]}},
                    "invocations": [{"executionSuccessful": True}],
                    "results": [
                        _result("A"),
                        _result("B", [{"kind": "inSource"}]),
                        _result("C", [{"kind": "external", "status": "accepted"}]),
                        _result("D", [{"kind": "inSource", "status": "underReview"}]),
                        _result("E", [{"kind": "inSource", "status": "rejected"}]),
                        _result("F", [{"kind": "bogus"}]),
                        _result("G", []),
                        _result("H", "not-a-list"),
                    ],
                    "properties": {"x": 1},
                }
            ],
        }
        filtered, removed = filter_suppressed(sarif)
        assert removed == 2
        run = filtered["runs"][0]
        assert [r["ruleId"] for r in run["results"]] == ["A", "D", "E", "F", "G", "H"]
        expected = json.loads(json.dumps(sarif))
        expected["runs"][0]["results"] = [
            r for r in expected["runs"][0]["results"] if r["ruleId"] not in {"B", "C"}
        ]
        assert filtered == expected

    def test_does_not_mutate_input(self) -> None:
        sarif = _suppressed_sarif([{"kind": "inSource"}])
        snapshot = json.loads(json.dumps(sarif))
        filtered, _ = filter_suppressed(sarif)
        assert sarif == snapshot
        filtered["runs"][0]["tool"]["driver"]["name"] = "changed"
        assert sarif["runs"][0]["tool"]["driver"]["name"] == "semgrep"

    def test_filters_every_run_and_ignores_severity(self) -> None:
        sarif = {
            "version": "2.1.0",
            "runs": [
                {"results": [_result("A", [{"kind": "inSource"}])]},
                {"results": [{**_result("B", [{"kind": "external"}]), "level": "note"}]},
                {"tool": {"driver": {"name": "no-results"}}},
            ],
        }
        filtered, removed = filter_suppressed(sarif)
        assert removed == 2
        assert filtered["runs"][0]["results"] == []
        assert filtered["runs"][1]["results"] == []
        assert filtered["runs"][2] == {"tool": {"driver": {"name": "no-results"}}}

    def test_non_dict_result_is_kept(self) -> None:
        sarif = {"version": "2.1.0", "runs": [{"results": ["odd", _result("A")]}]}
        filtered, removed = filter_suppressed(sarif)
        assert removed == 0
        assert filtered == sarif

    def test_non_list_results_and_non_dict_runs_left_untouched(self) -> None:
        sarif = {"version": "2.1.0", "runs": [{"results": None}, "odd-run"]}
        filtered, removed = filter_suppressed(sarif)
        assert removed == 0
        assert filtered == sarif

    @pytest.mark.parametrize("doc", [{"version": "2.1.0"}, {"runs": "nope"}, ["runs"]])
    def test_invalid_document_raises(self, doc: Any) -> None:
        with pytest.raises(ValueError, match="invalid SARIF"):
            filter_suppressed(doc)
