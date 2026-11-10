"""Tests for vergil_tooling.bin.vrg_semgrep_scan CLI."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.bin.vrg_semgrep_scan import main
from vergil_tooling.lib.semgrep import ScanResult

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

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


def _fake_semgrep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str) -> None:
    """Put a ``semgrep`` running *script* (sh) first on PATH, outside CI."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "semgrep"
    fake.write_text(f"#!/bin/sh\n{script}")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def test_scan_failure_prints_semgrep_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_semgrep(
        tmp_path,
        monkeypatch,
        "printf 'Scanning 12 files\\n'\n"
        "printf 'Fatal error at line 1\\nsemgrep-core exited with -11!\\n\\n' >&2\n"
        "exit 2\n",
    )

    rc = main(["--language", "python", "--output", str(tmp_path / "out.sarif")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "Scanning 12 files" in err
    assert "Fatal error at line 1" in err
    assert "ERROR: semgrep scan failed with exit code 2: semgrep-core exited with -11!" in err
    # The full output comes before the error line.
    assert err.index("Fatal error at line 1") < err.index("ERROR: semgrep scan failed")


def test_scan_failure_falls_back_to_stdout_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_semgrep(tmp_path, monkeypatch, "printf 'invalid config p/python\\n'\nexit 7\n")

    rc = main(["--language", "python", "--output", str(tmp_path / "out.sarif")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "ERROR: semgrep scan failed with exit code 7: invalid config p/python" in err


def test_scan_failure_with_no_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_semgrep(tmp_path, monkeypatch, "exit 2\n")

    rc = main(["--language", "python", "--output", str(tmp_path / "out.sarif")])

    assert rc == 2
    err = capsys.readouterr().err
    assert "--- semgrep" not in err
    assert err.rstrip("\n").endswith("ERROR: semgrep scan failed with exit code 2")


def test_findings_scan_prints_no_semgrep_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "results.sarif"

    def _fake_scan(rulesets: list, target: object, out: object, **_kwargs: object) -> ScanResult:
        output.write_text(json.dumps(_FINDINGS_SARIF))
        return ScanResult(returncode=1, sarif_produced=True, stderr="noisy progress\n")

    with (
        patch(f"{_MOD}.run_scan", side_effect=_fake_scan),
        patch(f"{_MOD}.emit_error"),
        patch(f"{_MOD}.write_output"),
        patch(f"{_MOD}.write_summary"),
    ):
        rc = main(["--language", "python", "--output", str(output)])
    assert rc == 1
    assert "noisy progress" not in capsys.readouterr().err


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


_SUPPRESSED_SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "semgrep"}},
            "results": [
                {
                    "ruleId": "yaml.github-actions.security.secrets-inherit.secrets-inherit",
                    "level": "warning",
                    "message": {"text": "secrets: inherit"},
                    "locations": [],
                    "suppressions": [{"kind": "inSource"}],
                }
            ],
        }
    ],
}


def test_nosemgrep_suppressed_finding_passes(tmp_path: Path) -> None:
    output = tmp_path / "results.sarif"

    def _fake_scan(rulesets: list, target: object, out: object, **_kwargs: object) -> ScanResult:
        output.write_text(json.dumps(_SUPPRESSED_SARIF))
        return ScanResult(returncode=0, sarif_produced=True)

    with (
        patch(f"{_MOD}.run_scan", side_effect=_fake_scan),
        patch(f"{_MOD}.emit_error") as mock_err,
        patch(f"{_MOD}.write_output") as mock_out,
        patch(f"{_MOD}.write_summary") as mock_sum,
    ):
        rc = main(["--language", "python", "--output", str(output)])
    assert rc == 0
    mock_err.assert_not_called()
    calls = {c[0][0]: c[0][1] for c in mock_out.call_args_list}
    assert calls["finding_count"] == "0"
    assert calls["suppressed_count"] == "1"
    mock_sum.assert_called_once()
    assert "suppressed" in mock_sum.call_args[0][0]
