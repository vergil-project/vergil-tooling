"""Tests for vergil_tooling.bin.vrg_npm_audit_gate CLI (issue #4682)."""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_npm_audit_gate
from vergil_tooling.bin.vrg_npm_audit_gate import main

if TYPE_CHECKING:
    from pathlib import Path

PICOMATCH = "GHSA-fc5q-39xc-vjpf"
OTHER = "GHSA-2222-3333-4444"


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vrg_npm_audit_gate, "_today", lambda: datetime.date(2026, 11, 18))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _report(tmp_path: Path, *advisories: tuple[str, str]) -> Path:
    vulnerabilities: dict[str, Any] = {}
    for advisory_id, severity in advisories:
        name = f"pkg-{advisory_id[-4:]}"
        vulnerabilities[name] = {
            "severity": severity,
            "via": [
                {
                    "name": name,
                    "title": f"title of {advisory_id}",
                    "url": f"https://github.com/advisories/{advisory_id}",
                    "severity": severity,
                }
            ],
        }
    path = tmp_path / "npm-audit.json"
    path.write_text(json.dumps({"auditReportVersion": 2, "vulnerabilities": vulnerabilities}))
    return path


def _run(tmp_path: Path, report: Path, npm_exit: int, exceptions: str | None) -> int:
    path = tmp_path / ".npm-audit-exceptions"
    if exceptions is not None:
        path.write_text(exceptions)
    return main([str(report), "--npm-exit", str(npm_exit), "--exceptions", str(path)])


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "vrg-npm-audit-gate" in capsys.readouterr().out


def test_live_exception_passes_and_is_listed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report(tmp_path, (PICOMATCH, "high"))
    rc = _run(tmp_path, report, 1, f"{PICOMATCH} exp:2026-12-31 owner/repo#11 no patched release\n")
    out = capsys.readouterr().out
    assert rc == 0
    assert (
        f"npm audit exception applied: {PICOMATCH} (high, pkg-vjpf) until 2026-12-31, "
        "tracked in owner/repo#11: no patched release"
    ) in out
    assert "0 advisory(ies) at high or critical, 1 covered by an exception" in out


def test_expired_exception_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = _report(tmp_path, (PICOMATCH, "high"))
    rc = _run(tmp_path, report, 1, f"{PICOMATCH} exp:2026-11-01 #11 r\n")
    err = capsys.readouterr().err
    assert rc == 1
    assert f"the exception for {PICOMATCH} expired on 2026-11-01" in err


def test_malformed_exception_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = _report(tmp_path)
    rc = _run(tmp_path, report, 0, f"{PICOMATCH} #11 no expiry here\n")
    err = capsys.readouterr().err
    assert rc == 1
    assert "'#11' is not an expiry (exp:YYYY-MM-DD)" in err


def test_unmatched_exception_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report(tmp_path)
    rc = _run(tmp_path, report, 0, f"{PICOMATCH} exp:2026-12-31 #11 r\n")
    captured = capsys.readouterr()
    assert rc == 0
    assert (
        f"WARNING: {tmp_path / '.npm-audit-exceptions'}: line 1: the exception for "
        f"{PICOMATCH} matches no advisory in the report: remove it"
    ) in captured.err
    assert "1 unmatched exception(s)" in captured.out


def test_finding_with_no_exception_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = _report(tmp_path, (PICOMATCH, "high"), (OTHER, "critical"))
    rc = _run(tmp_path, report, 1, f"{PICOMATCH} exp:2026-12-31 #11 r\n")
    captured = capsys.readouterr()
    assert rc == 1
    assert (
        f"ERROR: {OTHER} (critical, pkg-4444): title of {OTHER} "
        f"<https://github.com/advisories/{OTHER}>"
    ) in captured.err
    assert "1 advisory(ies) at high or critical, 1 covered by an exception" in captured.out


def test_no_exceptions_file_means_none(tmp_path: Path) -> None:
    assert _run(tmp_path, _report(tmp_path), 0, None) == 0
    assert _run(tmp_path, _report(tmp_path, (PICOMATCH, "high")), 1, None) == 1


def test_moderate_finding_passes(tmp_path: Path) -> None:
    assert _run(tmp_path, _report(tmp_path, (OTHER, "moderate")), 0, None) == 0


def test_npm_status_must_match_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, _report(tmp_path), 1, None) == 1
    assert "npm audit exited 1, but its report" in capsys.readouterr().err
    assert _run(tmp_path, _report(tmp_path, (OTHER, "high")), 0, None) == 1
    assert "has advisory at high or critical" in capsys.readouterr().err


def test_unreadable_report_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "nope.json"
    assert _run(tmp_path, missing, 0, None) == 1
    assert f"cannot read the npm audit report {missing}" in capsys.readouterr().err
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _run(tmp_path, bad, 0, None) == 1
    error = tmp_path / "error.json"
    error.write_text(json.dumps({"error": {"summary": "request to registry failed"}}))
    assert _run(tmp_path, error, 1, None) == 1
    assert "npm audit reported an error: request to registry failed" in capsys.readouterr().err


def test_unreadable_exceptions_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".npm-audit-exceptions").mkdir()
    assert _run(tmp_path, _report(tmp_path), 0, None) == 1
    assert "cannot read" in capsys.readouterr().err
