"""Tests for vergil_tooling.bin.vrg_sarif_filter CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_sarif_filter import main

_MOD = "vergil_tooling.bin.vrg_sarif_filter"

# Shape of semgrep's SARIF for a finding silenced by ``# nosemgrep: <rule-id>``.
_SEMGREP_SARIF: dict[str, Any] = {
    "$schema": "https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/schemas/sarif-schema-2.1.0.json",
    "version": "2.1.0",
    "runs": [
        {
            "tool": {
                "driver": {
                    "name": "Semgrep OSS",
                    "rules": [{"id": "secrets-inherit"}, {"id": "shell-true"}],
                }
            },
            "invocations": [{"executionSuccessful": True, "toolExecutionNotifications": []}],
            "results": [
                {
                    "ruleId": "secrets-inherit",
                    "level": "warning",
                    "message": {"text": "secrets: inherit"},
                    "suppressions": [{"kind": "inSource"}],
                },
                {
                    "ruleId": "shell-true",
                    "level": "error",
                    "message": {"text": "shell=True"},
                },
            ],
        }
    ],
}


def _write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    assert "vrg-sarif-filter" in capsys.readouterr().out


def test_filters_to_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "in.sarif"
    dst = tmp_path / "out.sarif"
    _write(src, _SEMGREP_SARIF)
    rc = main([str(src), str(dst)])
    assert rc == 0
    out = json.loads(dst.read_text(encoding="utf-8"))
    assert [r["ruleId"] for r in out["runs"][0]["results"]] == ["shell-true"]
    assert out["runs"][0]["tool"] == _SEMGREP_SARIF["runs"][0]["tool"]
    assert out["runs"][0]["invocations"] == _SEMGREP_SARIF["runs"][0]["invocations"]
    assert out["$schema"] == _SEMGREP_SARIF["$schema"]
    # The input is left as-is.
    assert json.loads(src.read_text(encoding="utf-8")) == _SEMGREP_SARIF
    assert capsys.readouterr().out == (
        f"vrg-sarif-filter: removed 1 suppressed result(s) from {src} -> {dst}\n"
    )


def test_in_place(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "scan.sarif"
    _write(path, _SEMGREP_SARIF)
    rc = main([str(path), str(path)])
    assert rc == 0
    out = json.loads(path.read_text(encoding="utf-8"))
    assert [r["ruleId"] for r in out["runs"][0]["results"]] == ["shell-true"]
    assert "removed 1 suppressed result(s)" in capsys.readouterr().out
    assert sorted(p.name for p in tmp_path.iterdir()) == ["scan.sarif"]


def test_nothing_suppressed_reports_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = tmp_path / "in.sarif"
    dst = tmp_path / "out.sarif"
    clean = {"version": "2.1.0", "runs": [{"tool": {"driver": {"name": "t"}}, "results": []}]}
    _write(src, clean)
    assert main([str(src), str(dst)]) == 0
    assert json.loads(dst.read_text(encoding="utf-8")) == clean
    assert "removed 0 suppressed result(s)" in capsys.readouterr().out


def test_missing_input(tmp_path: Path) -> None:
    dst = tmp_path / "out.sarif"
    with patch(f"{_MOD}.emit_error") as err:
        rc = main([str(tmp_path / "nope.sarif"), str(dst)])
    assert rc == 1
    err.assert_called_once()
    assert "nope.sarif" in err.call_args.args[0]
    assert not dst.exists()


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({"version": "2.1.0"}), json.dumps(["runs"]), json.dumps({"runs": 1})],
)
def test_invalid_input(tmp_path: Path, content: str) -> None:
    src = tmp_path / "in.sarif"
    dst = tmp_path / "out.sarif"
    src.write_text(content, encoding="utf-8")
    with patch(f"{_MOD}.emit_error") as err:
        rc = main([str(src), str(dst)])
    assert rc == 1
    err.assert_called_once()
    assert not dst.exists()


def test_unwritable_output_cleans_up(tmp_path: Path) -> None:
    src = tmp_path / "in.sarif"
    _write(src, _SEMGREP_SARIF)
    dst = tmp_path / "missing-dir" / "out.sarif"
    with patch(f"{_MOD}.emit_error") as err:
        rc = main([str(src), str(dst)])
    assert rc == 1
    err.assert_called_once()
    assert "out.sarif" in err.call_args.args[0]


def test_replace_failure_removes_temp_file(tmp_path: Path) -> None:
    src = tmp_path / "in.sarif"
    dst = tmp_path / "out.sarif"
    _write(src, _SEMGREP_SARIF)
    with (
        patch.object(Path, "replace", side_effect=OSError("boom")),
        patch(f"{_MOD}.emit_error") as err,
    ):
        rc = main([str(src), str(dst)])
    assert rc == 1
    assert "boom" in err.call_args.args[0]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.sarif"]
