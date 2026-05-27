"""Tests for vergil_tooling.bin.vrg_trivy_scan CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_trivy_scan import main
from vergil_tooling.lib.trivy import ScanResult

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_trivy_scan"

_CLEAN_SARIF = {
    "version": "2.1.0",
    "runs": [{"tool": {"driver": {"name": "trivy"}}, "results": []}],
}

_FINDINGS_SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "trivy"}},
            "results": [
                {
                    "ruleId": "CVE-2024-1234",
                    "level": "error",
                    "message": {"text": "vulnerable package"},
                    "locations": [],
                }
            ],
        }
    ],
}


def _make_scan_result(
    tmp_path: Path,
    sarif_data: dict | None = None,
) -> ScanResult:
    sarif_path = str(tmp_path / "trivy-results.sarif")
    table_path = str(tmp_path / "trivy-results.table")
    if sarif_data:
        (tmp_path / "trivy-results.sarif").write_text(json.dumps(sarif_data))
    return ScanResult(returncode=0, sarif_path=sarif_path, table_path=table_path)


def test_clean_scan(tmp_path: Path) -> None:
    result = _make_scan_result(tmp_path, _CLEAN_SARIF)
    with (
        patch(f"{_MOD}.run_scan", return_value=result),
        patch(f"{_MOD}.write_output"),
    ):
        rc = main(
            [
                "--type",
                "filesystem",
                "--target",
                "/project",
                "--output-dir",
                str(tmp_path),
            ]
        )
    assert rc == 0


def test_findings_scan(tmp_path: Path) -> None:
    result = _make_scan_result(tmp_path, _FINDINGS_SARIF)
    with (
        patch(f"{_MOD}.run_scan", return_value=result),
        patch(f"{_MOD}.emit_error"),
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}.write_summary"),
    ):
        rc = main(
            [
                "--type",
                "filesystem",
                "--target",
                "/project",
                "--output-dir",
                str(tmp_path),
            ]
        )
    assert rc == 1


def test_scan_failure(tmp_path: Path) -> None:
    result = ScanResult(
        returncode=2,
        sarif_path=str(tmp_path / "trivy-results.sarif"),
        table_path=str(tmp_path / "trivy-results.table"),
    )
    with (
        patch(f"{_MOD}.run_scan", return_value=result),
        patch(f"{_MOD}.emit_error") as mock_err,
    ):
        rc = main(
            [
                "--type",
                "image",
                "--target",
                "myimage:latest",
                "--output-dir",
                str(tmp_path),
            ]
        )
    assert rc == 2
    mock_err.assert_called_once()


def test_no_sarif_file(tmp_path: Path) -> None:
    result = _make_scan_result(tmp_path)
    with (
        patch(f"{_MOD}.run_scan", return_value=result),
        patch(f"{_MOD}.write_output") as mock_out,
    ):
        rc = main(
            [
                "--type",
                "filesystem",
                "--target",
                "/project",
                "--output-dir",
                str(tmp_path),
            ]
        )
    assert rc == 0
    calls = {c[0][0]: c[0][1] for c in mock_out.call_args_list}
    assert calls["finding_count"] == "0"


def test_sbom_generation(tmp_path: Path) -> None:
    result = _make_scan_result(tmp_path, _CLEAN_SARIF)
    sbom_path = tmp_path / "sbom.json"
    with (
        patch(f"{_MOD}.run_scan", return_value=result),
        patch(f"{_MOD}.generate_sbom", return_value=0) as mock_sbom,
        patch(f"{_MOD}.write_output"),
    ):
        rc = main(
            [
                "--type",
                "filesystem",
                "--target",
                "/project",
                "--output-dir",
                str(tmp_path),
                "--sbom",
                str(sbom_path),
            ]
        )
    assert rc == 0
    mock_sbom.assert_called_once()


def test_sbom_on_findings(tmp_path: Path) -> None:
    result = _make_scan_result(tmp_path, _FINDINGS_SARIF)
    sbom_path = tmp_path / "sbom.json"
    with (
        patch(f"{_MOD}.run_scan", return_value=result),
        patch(f"{_MOD}.generate_sbom", return_value=0) as mock_sbom,
        patch(f"{_MOD}.emit_error"),
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}.write_summary"),
    ):
        rc = main(
            [
                "--type",
                "filesystem",
                "--target",
                "/project",
                "--output-dir",
                str(tmp_path),
                "--sbom",
                str(sbom_path),
            ]
        )
    assert rc == 1
    mock_sbom.assert_called_once()


def test_outputs_finding_count(tmp_path: Path) -> None:
    result = _make_scan_result(tmp_path, _FINDINGS_SARIF)
    with (
        patch(f"{_MOD}.run_scan", return_value=result),
        patch(f"{_MOD}.emit_error"),
        patch(f"{_MOD}.write_output") as mock_out,
        patch(f"{_MOD}.write_summary"),
    ):
        main(
            [
                "--type",
                "filesystem",
                "--target",
                "/project",
                "--output-dir",
                str(tmp_path),
            ]
        )
    calls = {c[0][0]: c[0][1] for c in mock_out.call_args_list}
    assert calls["finding_count"] == "1"
