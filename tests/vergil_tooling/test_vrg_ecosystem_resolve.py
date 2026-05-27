"""Tests for vrg-ecosystem-resolve CLI."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

from vergil_tooling.bin.vrg_ecosystem_resolve import main


def test_python_ecosystem_interactive(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_ecosystem_resolve.is_ci", return_value=False):
        rc = main(["python"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "build: uv build\n" in captured.out
    assert "publish: uv publish\n" in captured.out
    assert "credential-secret: PYPI_TOKEN\n" in captured.out


def test_python_ecosystem_ci_mode(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    with (
        patch("vergil_tooling.bin.vrg_ecosystem_resolve.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        rc = main(["python"])
    assert rc == 0
    content = output_file.read_text()
    assert "build=uv build\n" in content
    assert "publish=uv publish\n" in content
    assert "credential-secret=PYPI_TOKEN\n" in content


def test_go_ecosystem_ci_mode(tmp_path: Path) -> None:
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    with (
        patch("vergil_tooling.bin.vrg_ecosystem_resolve.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        rc = main(["go"])
    assert rc == 0
    content = output_file.read_text()
    assert "build=go build ./...\n" in content
    assert "publish=\n" in content
    assert "credential-secret=\n" in content


def test_go_ecosystem_no_credential(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_ecosystem_resolve.is_ci", return_value=False):
        rc = main(["go"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "build: go build ./...\n" in captured.out
    assert "credential-secret: \n" in captured.out


def test_rust_ecosystem_ci_mode(tmp_path: Path) -> None:
    output_file = tmp_path / "github_output"
    output_file.write_text("")
    with (
        patch("vergil_tooling.bin.vrg_ecosystem_resolve.is_ci", return_value=True),
        patch.dict(os.environ, {"GITHUB_OUTPUT": str(output_file)}),
    ):
        rc = main(["rust"])
    assert rc == 0
    content = output_file.read_text()
    assert "build=cargo build --release\n" in content
    assert "publish=cargo publish\n" in content
    assert "credential-secret=CRATES_IO_TOKEN\n" in content


def test_unknown_language_fails() -> None:
    with patch("vergil_tooling.bin.vrg_ecosystem_resolve.is_ci", return_value=False):
        rc = main(["unknown"])
    assert rc == 1


def test_no_args_fails() -> None:
    import pytest

    with pytest.raises(SystemExit):
        main([])
