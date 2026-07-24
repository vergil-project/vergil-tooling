"""Tests for vergil_tooling.bin.vrg_semgrep_scan CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_semgrep_scan import main
from vergil_tooling.lib.semgrep import ScanResult

if TYPE_CHECKING:
    from pathlib import Path

_MOD = "vergil_tooling.bin.vrg_semgrep_scan"

_CLEAN_SARIF = {
    "version": "2.1.0",
    "runs": [{"tool": {"driver": {"name": "semgrep"}}, "results": []}],
}

_FINDINGS_SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "semgrep"}},
            "results": [
                {
                    "ruleId": "python.security.audit",
                    "level": "warning",
                    "message": {"text": "potential issue"},
                    "locations": [],
                }
            ],
        }
    ],
}


def test_clean_scan(tmp_path: Path) -> None:
    output = tmp_path / "results.sarif"

    def _fake_scan(rulesets: list, target: object, out: object, **_kwargs: object) -> ScanResult:
        output.write_text(json.dumps(_CLEAN_SARIF))
        return ScanResult(returncode=0, sarif_produced=True)

    with (
        patch(f"{_MOD}.run_scan", side_effect=_fake_scan),
        patch(f"{_MOD}.write_output"),
    ):
        rc = main(["--language", "python", "--output", str(output)])
    assert rc == 0


def test_findings_scan(tmp_path: Path) -> None:
    output = tmp_path / "results.sarif"

    def _fake_scan(rulesets: list, target: object, out: object, **_kwargs: object) -> ScanResult:
        output.write_text(json.dumps(_FINDINGS_SARIF))
        return ScanResult(returncode=1, sarif_produced=True)

    with (
        patch(f"{_MOD}.run_scan", side_effect=_fake_scan),
        patch(f"{_MOD}.emit_error"),
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}.write_summary"),
    ):
        rc = main(["--language", "python", "--output", str(output)])
    assert rc == 1


def test_scan_failure(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.run_scan", return_value=ScanResult(returncode=2, sarif_produced=False)),
        patch(f"{_MOD}.emit_error") as mock_err,
        patch(f"{_MOD}.write_output"),
    ):
        rc = main(
            [
                "--language",
                "python",
                "--output",
                str(tmp_path / "out.sarif"),
            ]
        )
    assert rc == 2
    mock_err.assert_called_once()


def test_no_sarif_produced(tmp_path: Path) -> None:
    with (
        patch(f"{_MOD}.run_scan", return_value=ScanResult(returncode=0, sarif_produced=False)),
        patch(f"{_MOD}.emit_warning") as mock_warn,
        patch(f"{_MOD}.write_output"),
    ):
        rc = main(
            [
                "--language",
                "python",
                "--output",
                str(tmp_path / "out.sarif"),
            ]
        )
    assert rc == 0
    mock_warn.assert_called_once()


def test_unknown_language(tmp_path: Path) -> None:
    with patch(f"{_MOD}.emit_warning") as mock_warn:
        rc = main(
            [
                "--language",
                "cobol",
                "--output",
                str(tmp_path / "out.sarif"),
            ]
        )
    assert rc == 0
    mock_warn.assert_called_once()


def test_extra_config(tmp_path: Path) -> None:
    output = tmp_path / "results.sarif"

    def _fake_scan(rulesets: list, target: object, out: object, **_kwargs: object) -> ScanResult:
        output.write_text(json.dumps(_CLEAN_SARIF))
        return ScanResult(returncode=0, sarif_produced=True)

    with (
        patch(f"{_MOD}.run_scan", side_effect=_fake_scan) as mock_scan,
        patch(f"{_MOD}.write_output"),
    ):
        main(
            [
                "--language",
                "python",
                "--output",
                str(output),
                "--extra-config",
                "p/custom",
            ]
        )

    rulesets = mock_scan.call_args[0][0]
    assert "p/custom" in rulesets


def test_exclude_rule_threads_through(tmp_path: Path) -> None:
    output = tmp_path / "results.sarif"

    def _fake_scan(rulesets: list, target: object, out: object, **_kwargs: object) -> ScanResult:
        output.write_text(json.dumps(_CLEAN_SARIF))
        return ScanResult(returncode=0, sarif_produced=True)

    with (
        patch(f"{_MOD}.run_scan", side_effect=_fake_scan) as mock_scan,
        patch(f"{_MOD}.write_output"),
    ):
        main(
            [
                "--language",
                "python",
                "--output",
                str(output),
                "--exclude-rule",
                "custom.rule.a",
                "--exclude-rule",
                "custom.rule.b",
            ]
        )

    exclude_rules = mock_scan.call_args.kwargs["exclude_rules"]
    assert "custom.rule.a" in exclude_rules
    assert "custom.rule.b" in exclude_rules


def test_dockerfile_and_workflow_flags(tmp_path: Path) -> None:
    output = tmp_path / "results.sarif"

    def _fake_scan(rulesets: list, target: object, out: object, **_kwargs: object) -> ScanResult:
        output.write_text(json.dumps(_CLEAN_SARIF))
        return ScanResult(returncode=0, sarif_produced=True)

    with (
        patch(f"{_MOD}.run_scan", side_effect=_fake_scan) as mock_scan,
        patch(f"{_MOD}.write_output"),
    ):
        main(
            [
                "--language",
                "python",
                "--output",
                str(output),
                "--has-dockerfiles",
                "--has-workflows",
            ]
        )

    rulesets = mock_scan.call_args[0][0]
    assert "p/dockerfile" in rulesets
    assert "p/github-actions" in rulesets


def test_outputs_finding_count(tmp_path: Path) -> None:
    output = tmp_path / "results.sarif"

    def _fake_scan(rulesets: list, target: object, out: object, **_kwargs: object) -> ScanResult:
        output.write_text(json.dumps(_FINDINGS_SARIF))
        return ScanResult(returncode=1, sarif_produced=True)

    with (
        patch(f"{_MOD}.run_scan", side_effect=_fake_scan),
        patch(f"{_MOD}.emit_error"),
        patch(f"{_MOD}.write_output") as mock_out,
        patch(f"{_MOD}.write_summary"),
    ):
        main(["--language", "python", "--output", str(output)])

    calls = {c[0][0]: c[0][1] for c in mock_out.call_args_list}
    assert calls["finding_count"] == "1"
