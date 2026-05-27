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
    assert "build_cmd:" in captured.out
    assert "publish_cmd:" in captured.out
    assert "credential_required: True" in captured.out


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
    assert "build_cmd=" in content
    assert "publish_cmd=" in content
    assert "credential_secret_name=" in content


def test_go_ecosystem_no_credential(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("vergil_tooling.bin.vrg_ecosystem_resolve.is_ci", return_value=False):
        rc = main(["go"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "publish_cmd:" in captured.out
    assert "credential_required: False" in captured.out


def test_unknown_language_fails() -> None:
    with patch("vergil_tooling.bin.vrg_ecosystem_resolve.is_ci", return_value=False):
        rc = main(["unknown"])
    assert rc == 1


def test_no_args_fails() -> None:
    import pytest

    with pytest.raises(SystemExit):
        main([])
