"""Tests for vergil_tooling.bin.vrg_npm_license_gate (epic vergil-project/.github#1339)."""

from __future__ import annotations

import datetime
import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_npm_license_gate
from vergil_tooling.bin.vrg_npm_license_gate import main

if TYPE_CHECKING:
    from pathlib import Path

_REAL_TODAY = vrg_npm_license_gate._today


def test_today_is_the_utc_date() -> None:
    assert _REAL_TODAY() == datetime.datetime.now(datetime.UTC).date()


@pytest.fixture(autouse=True)
def _fixed_day(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vrg_npm_license_gate, "_today", lambda: datetime.date(2026, 11, 18))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


def _lockfile(tmp_path: Path, licenses: dict[str, Any]) -> Path:
    packages: dict[str, Any] = {"": {"name": "app", "version": "0.1.0"}}
    for name, value in licenses.items():
        entry: dict[str, Any] = {"version": "1.0.0", "dev": True}
        if value is not None:
            entry["license"] = value
        packages[f"node_modules/{name}"] = entry
    path = tmp_path / "package-lock.json"
    path.write_text(json.dumps({"lockfileVersion": 3, "packages": packages}))
    return path


def _run(tmp_path: Path, lockfile: Path, exceptions: str | None = None) -> int:
    if exceptions is not None:
        (tmp_path / ".npm-license-exceptions").write_text(exceptions)
    return main(["--lockfile", str(lockfile), "--output", str(tmp_path / "licenses.json")])


def _report(tmp_path: Path) -> list[dict[str, Any]]:
    return json.loads((tmp_path / "licenses.json").read_text())


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "vrg-npm-license-gate" in capsys.readouterr().out


def test_defaults_read_and_write_in_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _lockfile(tmp_path, {"a": "MIT"})
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    assert [row["name"] for row in _report(tmp_path)] == ["a"]
    assert (
        "npm license gate: 1 package(s) checked, 0 failing, 0 covered by an exception, "
        "0 unmatched exception(s); report written to licenses.json"
    ) in capsys.readouterr().out


def test_all_allowed_passes(tmp_path: Path) -> None:
    lockfile = _lockfile(tmp_path, {"a": "MIT", "b": "(MIT OR GPL-3.0-only)", "c": "isc"})
    assert _run(tmp_path, lockfile) == 0
    assert [row["verdict"] for row in _report(tmp_path)] == ["allowed"] * 3


def test_gate_writes_report_on_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lockfile = _lockfile(tmp_path, {"a": "MIT", "b": "(MIT AND GPL-3.0-only)", "c": None})
    assert _run(tmp_path, lockfile) == 1
    rows = {row["name"]: row for row in _report(tmp_path)}
    assert rows["a"]["verdict"] == "allowed"
    assert rows["b"]["verdict"] == "failing"
    assert rows["b"]["license"] == "(MIT AND GPL-3.0-only)"
    assert rows["c"] == {
        "name": "c",
        "version": "1.0.0",
        "license": None,
        "license_canonical": None,
        "dev": True,
        "verdict": "failing",
    }
    captured = capsys.readouterr()
    assert (
        'ERROR: b@1.0.0 (license "(MIT AND GPL-3.0-only)"): its license is not allowed '
        "(GPL-3.0-only not on the allowlist)"
    ) in captured.err
    assert "ERROR: c@1.0.0 (no license field): it records no license" in captured.err
    assert ".npm-license-exceptions" in captured.out
    assert "_TYPESCRIPT_LICENSES_ALLOWLIST" in captured.out
    assert "2 failing" in captured.out


def test_exception_applied_and_listed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lockfile = _lockfile(tmp_path, {"vendor": "SEE LICENSE IN LICENSE"})
    rc = _run(
        tmp_path,
        lockfile,
        'vendor "SEE LICENSE IN LICENSE" exp:2027-06-30 owner/repo#3 reviewed vendor terms\n',
    )
    assert rc == 0
    (row,) = _report(tmp_path)
    assert row["verdict"] == "excepted"
    assert row["exception"] == {"tracking": "owner/repo#3", "expires": "2027-06-30"}
    out = capsys.readouterr().out
    assert (
        'npm license exception applied: vendor@1.0.0 (license "SEE LICENSE IN LICENSE") '
        "until 2027-06-30, tracked in owner/repo#3: reviewed vendor terms"
    ) in out
    assert "1 covered by an exception" in out


def test_unmatched_exceptions_warn(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lockfile = _lockfile(tmp_path, {"a": "MIT"})
    rc = _run(
        tmp_path,
        lockfile,
        'a "MIT" exp:2027-01-01 #1 r\nghost - exp:2027-01-01 #2 r\n',
    )
    assert rc == 0
    captured = capsys.readouterr()
    path = tmp_path / ".npm-license-exceptions"
    assert (
        f'WARNING: {path}: line 1: the exception for a "MIT" matches only packages that '
        "pass without it: remove it"
    ) in captured.err
    assert (
        f"WARNING: {path}: line 2: the exception for ghost - matches no package in the "
        "lockfile: remove it"
    ) in captured.err
    assert "2 unmatched exception(s)" in captured.out


def test_gate_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "licenses.json"
    lockfile = tmp_path / "package-lock.json"

    # A missing lockfile: exit 1, naming the file and the fix, and no report,
    # not even a stale one from an earlier run.
    report.write_text("stale")
    assert _run(tmp_path, lockfile) == 1
    err = capsys.readouterr().err
    assert f"cannot read {lockfile}" in err
    assert "npm install --package-lock-only" in err
    assert not report.exists()

    # An unreadable one.
    lockfile.write_text("{not json")
    assert _run(tmp_path, lockfile) == 1
    assert "npm install --package-lock-only" in capsys.readouterr().err
    assert not report.exists()

    # A pre-v2 one.
    lockfile.write_text(json.dumps({"lockfileVersion": 1, "dependencies": {}}))
    assert _run(tmp_path, lockfile) == 1
    assert "not an npm v2 or v3 lockfile" in capsys.readouterr().err
    assert not report.exists()

    # A malformed exceptions file: exit 1 naming every bad line, and the report
    # is written with no exception applied, so it shows each real standing.
    _lockfile(tmp_path, {"vendor": "SEE LICENSE IN LICENSE", "a": "MIT"})
    rc = _run(
        tmp_path,
        lockfile,
        'vendor "SEE LICENSE IN LICENSE" exp:2027-01-01 #1 r\nbad\nworse MIT exp:2027-01-01 #1 r\n',
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "line 2: an entry is" in err
    assert "line 3: the license value must be a JSON string literal" in err
    assert {row["name"]: row["verdict"] for row in _report(tmp_path)} == {
        "a": "allowed",
        "vendor": "failing",
    }

    # A malformed exceptions file fails even when every package passes.
    _lockfile(tmp_path, {"a": "MIT"})
    assert _run(tmp_path, lockfile, "bad\n") == 1
    assert [row["verdict"] for row in _report(tmp_path)] == ["allowed"]

    # No exceptions file means none.
    (tmp_path / ".npm-license-exceptions").unlink()
    assert _run(tmp_path, lockfile) == 0


def test_unreadable_exceptions_file_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lockfile = _lockfile(tmp_path, {"a": "MIT"})
    (tmp_path / ".npm-license-exceptions").mkdir()
    assert _run(tmp_path, lockfile) == 1
    assert "cannot read" in capsys.readouterr().err
    assert [row["verdict"] for row in _report(tmp_path)] == ["allowed"]


def test_unwritable_report_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lockfile = _lockfile(tmp_path, {"a": "MIT"})
    missing_dir = tmp_path / "no-such-dir" / "licenses.json"
    assert main(["--lockfile", str(lockfile), "--output", str(missing_dir)]) == 1
    assert f"cannot write the licenses report {missing_dir}" in capsys.readouterr().err


def test_exceptions_read_beside_the_lockfile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lockfile = _lockfile(repo, {"vendor": "SEE LICENSE IN LICENSE"})
    (repo / ".npm-license-exceptions").write_text(
        'vendor "SEE LICENSE IN LICENSE" exp:2027-01-01 #1 r\n'
    )
    assert main(["--lockfile", str(lockfile), "--output", str(tmp_path / "licenses.json")]) == 0
