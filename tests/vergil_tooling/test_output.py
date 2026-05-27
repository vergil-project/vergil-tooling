"""Tests for vergil_tooling.lib.output."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

from vergil_tooling.lib.output import (
    emit_error,
    emit_warning,
    is_ci,
    write_output,
    write_summary,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_is_ci_returns_true_when_not_a_tty() -> None:
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = False
        assert is_ci() is True


def test_is_ci_returns_false_when_tty() -> None:
    with patch("sys.stdout") as mock_stdout:
        mock_stdout.isatty.return_value = True
        assert is_ci() is False


def test_emit_error_ci_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=True):
        emit_error("something broke")
    captured = capsys.readouterr()
    assert captured.err == "::error ::something broke\n"


def test_emit_error_ci_mode_with_file_and_line(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=True):
        emit_error("bad value", file="src/main.py", line=42)
    captured = capsys.readouterr()
    assert captured.err == "::error file=src/main.py,line=42::bad value\n"


def test_emit_error_interactive_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        emit_error("something broke")
    captured = capsys.readouterr()
    assert "something broke" in captured.err
    assert "::" not in captured.err


def test_emit_warning_ci_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=True):
        emit_warning("heads up")
    captured = capsys.readouterr()
    assert captured.err == "::warning ::heads up\n"


def test_emit_warning_ci_mode_with_file(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=True):
        emit_warning("check this", file="action.yml")
    captured = capsys.readouterr()
    assert captured.err == "::warning file=action.yml::check this\n"


def test_emit_warning_interactive_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        emit_warning("heads up")
    captured = capsys.readouterr()
    assert "heads up" in captured.err
    assert "::" not in captured.err


def test_write_output_ci_mode(tmp_path: Path) -> None:
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        write_output("version", "1.2.3")
    assert output_file.read_text() == "version=1.2.3\n"


def test_write_output_ci_mode_appends(tmp_path: Path) -> None:
    output_file = tmp_path / "github_output"
    output_file.write_text("existing=value\n")
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        write_output("new_key", "new_value")
    assert output_file.read_text() == "existing=value\nnew_key=new_value\n"


def test_write_output_ci_mode_missing_env_var(capsys: pytest.CaptureFixture[str]) -> None:
    env = os.environ.copy()
    env.pop("GITHUB_OUTPUT", None)
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, env, clear=True),
    ):
        write_output("version", "1.2.3")
    captured = capsys.readouterr()
    assert "version: 1.2.3" in captured.out


def test_write_output_interactive_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        write_output("version", "1.2.3")
    captured = capsys.readouterr()
    assert "version: 1.2.3" in captured.out


def test_write_summary_ci_mode(tmp_path: Path) -> None:
    summary_file = tmp_path / "step_summary"
    summary_file.write_text("")
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_file)}),
    ):
        write_summary("## Results\n\nAll clean.")
    assert summary_file.read_text() == "## Results\n\nAll clean.\n"


def test_write_summary_ci_mode_missing_env_var(capsys: pytest.CaptureFixture[str]) -> None:
    env = os.environ.copy()
    env.pop("GITHUB_STEP_SUMMARY", None)
    with (
        patch("vergil_tooling.lib.output.is_ci", return_value=True),
        patch.dict(os.environ, env, clear=True),
    ):
        write_summary("## Results")
    captured = capsys.readouterr()
    assert "## Results" in captured.out


def test_write_summary_interactive_mode(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.lib.output.is_ci", return_value=False):
        write_summary("## Results")
    captured = capsys.readouterr()
    assert "## Results" in captured.out
