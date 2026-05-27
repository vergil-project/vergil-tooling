"""Tests for vergil_tooling.bin.vrg_sarif_evaluate CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_sarif_evaluate import main

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_sarif_evaluate"

_CLEAN_SARIF = {
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
                    "ruleId": "VULN-1",
                    "level": "error",
                    "message": {"text": "issue found"},
                    "locations": [
                        {
                            "physicalLocation": {
                                "artifactLocation": {"uri": "app.py"},
                                "region": {"startLine": 5},
                            }
                        }
                    ],
                }
            ],
        }
    ],
}


def _write_sarif(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_clean_file(tmp_path: Path) -> None:
    f = tmp_path / "clean.sarif"
    _write_sarif(f, _CLEAN_SARIF)
    rc = main([str(f)])
    assert rc == 0


def test_findings_file(tmp_path: Path) -> None:
    f = tmp_path / "findings.sarif"
    _write_sarif(f, _FINDINGS_SARIF)
    with patch(f"{_MOD}.emit_error"), patch(f"{_MOD}.write_summary"):
        rc = main([str(f)])
    assert rc == 1


def test_directory_mode(tmp_path: Path) -> None:
    _write_sarif(tmp_path / "a.sarif", _CLEAN_SARIF)
    _write_sarif(tmp_path / "b.sarif", _FINDINGS_SARIF)
    with patch(f"{_MOD}.emit_error"), patch(f"{_MOD}.write_summary"):
        rc = main([str(tmp_path)])
    assert rc == 1


def test_empty_directory(tmp_path: Path) -> None:
    with patch(f"{_MOD}.emit_warning") as mock_warn:
        rc = main([str(tmp_path)])
    assert rc == 0
    mock_warn.assert_called_once()


def test_missing_path(tmp_path: Path) -> None:
    with patch(f"{_MOD}.emit_error") as mock_err:
        rc = main([str(tmp_path / "missing.sarif")])
    assert rc == 2
    mock_err.assert_called_once()


def test_severity_filter(tmp_path: Path) -> None:
    f = tmp_path / "findings.sarif"
    _write_sarif(f, _FINDINGS_SARIF)
    rc = main([str(f), "--severity", "warning"])
    assert rc == 0


def test_emits_error_per_finding(tmp_path: Path) -> None:
    f = tmp_path / "findings.sarif"
    _write_sarif(f, _FINDINGS_SARIF)
    with (
        patch(f"{_MOD}.emit_error") as mock_err,
        patch(f"{_MOD}.write_summary"),
    ):
        main([str(f)])
    mock_err.assert_called_once()
    assert "VULN-1" in mock_err.call_args[0][0]


def test_writes_summary_on_findings(tmp_path: Path) -> None:
    f = tmp_path / "findings.sarif"
    _write_sarif(f, _FINDINGS_SARIF)
    with (
        patch(f"{_MOD}.emit_error"),
        patch(f"{_MOD}.write_summary") as mock_sum,
    ):
        main([str(f)])
    mock_sum.assert_called_once()
    assert "Security Scan" in mock_sum.call_args[0][0]
