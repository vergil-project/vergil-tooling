"""Tests for vrg-promote CLI."""

from __future__ import annotations

import subprocess as _sp
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from vergil_tooling.bin.vrg_promote import main


def _write_toml(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(
        '[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        '\n[dependencies]\nvergil = "v2.0"\n'
        '\n[ci]\nversions = ["3.14"]\n'
    )


def test_promote_explicit_version(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-promote", "2.0.34"]),
        patch("vergil_tooling.lib.promote.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        result = main()
    assert result == 0
    out = capsys.readouterr().out
    assert "Promoted" in out


def test_promote_from_version_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("3.1.0\n")
    with (
        patch("vergil_tooling.bin.vrg_promote.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-promote"]),
        patch("vergil_tooling.lib.promote.subprocess.run") as mock_run,
    ):
        mock_run.return_value = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        result = main()
    assert result == 0
    out = capsys.readouterr().out
    assert "v3.1" in out


def test_promote_dry_run(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("sys.argv", ["vrg-promote", "2.0.34", "--dry-run"]),
        patch("vergil_tooling.lib.promote.subprocess.run") as mock_run,
    ):
        result = main()
    assert result == 0
    mock_run.assert_not_called()
    out = capsys.readouterr().out
    assert "Would" in out


def test_promote_no_version_no_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch("vergil_tooling.bin.vrg_promote.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["vrg-promote"]),
    ):
        result = main()
    assert result == 1
    err = capsys.readouterr().err
    assert "VERSION" in err


def test_promote_invalid_version(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("sys.argv", ["vrg-promote", "invalid"]):
        result = main()
    assert result == 1
    err = capsys.readouterr().err
    assert "Error" in err
