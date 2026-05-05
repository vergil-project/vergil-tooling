"""Tests for st-version CLI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

if TYPE_CHECKING:
    import pytest

from standard_tooling.bin.version import main

if TYPE_CHECKING:
    from pathlib import Path


def _write_toml(tmp_path: Path, language: str = "shell") -> None:
    (tmp_path / "standard-tooling.toml").write_text(
        f'[project]\nrepository-type = "library"\nversioning-scheme = "semver"\n'
        f'branching-model = "library-release"\nrelease-model = "tagged-release"\n'
        f'primary-language = "{language}"\n\n[dependencies]\nstandard-tooling = "v1.4"\n'
    )


def test_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("standard_tooling.bin.version.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["st-version", "show"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.2.3"


def test_show_major_minor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("standard_tooling.bin.version.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["st-version", "show", "--major-minor"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.2"


def test_show_ref(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("standard_tooling.bin.version.Path.cwd", return_value=tmp_path),
        patch(
            "standard_tooling.lib.version._read_version_from_ref",
            return_value="1.1.0",
        ),
        patch("sys.argv", ["st-version", "show", "--ref", "origin/main"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.1.0"


def test_bump(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_toml(tmp_path)
    (tmp_path / "VERSION").write_text("1.2.3\n")
    with (
        patch("standard_tooling.bin.version.Path.cwd", return_value=tmp_path),
        patch("sys.argv", ["st-version", "bump"]),
    ):
        main()
    assert capsys.readouterr().out.strip() == "1.2.4"
    assert (tmp_path / "VERSION").read_text().strip() == "1.2.4"
